"""对 dev 库执行 init.sql 中指定段的 DDL（幂等），并校验表与索引。

直接取 init.sql 文本执行，保证库结构与真相源逐字一致（而非 ORM create_all）。

用法（marker 为段注释的开头文字，用于定位从哪一行开始执行）：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/apply_ddl_section.py "AI 会话"
    cd backend && PYTHONPATH=. .venv/bin/python scripts/apply_ddl_section.py "团队"

前置：docker 的 postgres 已就绪（连接读 backend/.env）。
"""
import argparse
import asyncio
import pathlib
import sys

import asyncpg

from app.core.config import settings

INIT_SQL = (
    pathlib.Path(__file__).resolve().parents[2]
    / "init-scripts"
    / "postgresql"
    / "init.sql"
)


async def main(marker: str) -> None:
    sql = INIT_SQL.read_text(encoding="utf-8")
    if marker not in sql:
        raise SystemExit(f"init.sql 中找不到段标记：{marker}")
    ddl = sql[sql.index(f"-- {marker}"):]
    print(f"执行 DDL（{len(ddl.splitlines())} 行，段标记『{marker}』）")

    conn = await asyncpg.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        database=settings.POSTGRES_DB,
    )
    try:
        await conn.execute(ddl)
        # 校验：列出本段 DDL 建出的表（取 CREATE TABLE IF NOT EXISTS 后的表名）
        tables = [
            line.split("CREATE TABLE IF NOT EXISTS")[1].strip().split()[0].strip("(")
            for line in ddl.splitlines()
            if "CREATE TABLE IF NOT EXISTS" in line
        ]
        if not tables:
            print("本段无 CREATE TABLE，跳过校验")
            return
        rows = await conn.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = any($1::text[])
            ORDER BY table_name
            """,
            tables,
        )
        found = [r["table_name"] for r in rows]
        idx = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = any($1::text[]) ORDER BY indexname
            """,
            tables,
        )
        print("已建表:", found)
        print("索引:", [r["indexname"] for r in idx])
        missing = set(tables) - set(found)
        if missing:
            raise SystemExit(f"表未全部创建：{missing}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="执行 init.sql 中指定段的 DDL")
    parser.add_argument("marker", help="段注释开头文字，如「AI 会话」「团队」")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.marker)))
