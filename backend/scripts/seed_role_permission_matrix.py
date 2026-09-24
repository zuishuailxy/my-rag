"""
将 init.sql 的 kh_role_permission 矩阵同步到运行中的数据库（幂等）。

- 仅插入，不删除：已存在的 id 由 ON CONFLICT DO NOTHING 跳过。
- 用途：本地 / 容器库在 init.sql 增补权限行后，把缺失的 role_permission 行补齐，
  使文档端点的 @RequirePermission 在种子用户（USER / REVIEWER / ADMIN）上行为正确。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/seed_role_permission_matrix.py
"""
from sqlalchemy import text

from app.db.session import async_session_factory

# (id, role_id, permission_id) — 与 init-scripts/postgresql/init.sql 完全一致
ROLE_PERMISSION_ROWS = [
    (4100000000000000001, 2000000000000000002, 4000000000000000011), # REVIEWER, document:list
    (4100000000000000002, 2000000000000000002, 4000000000000000015), # REVIEWER, document:review
    (4100000000000000003, 2000000000000000003, 4000000000000000001), # USER, dashboard(menu)
    (4100000000000000004, 2000000000000000003, 4000000000000000002), # USER, document(menu)
    (4100000000000000005, 2000000000000000003, 4000000000000000011), # USER, document:list
    (4100000000000000006, 2000000000000000003, 4000000000000000012), # USER, document:create
    (4100000000000000007, 2000000000000000003, 4000000000000000003), # USER, search(menu)
    (4100000000000000008, 2000000000000000003, 4000000000000000004), # USER, profile(menu)
    (4100000000000000009, 2000000000000000003, 4000000000000000013), # USER, document:edit
    (4100000000000000010, 2000000000000000003, 4000000000000000014), # USER, document:delete
    (4100000000000000011, 2000000000000000002, 4000000000000000003), # REVIEWER, search(menu)
]


async def main() -> None:
    async with async_session_factory() as session:
        before = await session.scalar(text("SELECT COUNT(*) FROM kh_role_permission"))
        for row_id, role_id, perm_id in ROLE_PERMISSION_ROWS:
            await session.execute(
                text(
                    "INSERT INTO kh_role_permission (id, role_id, permission_id) "
                    "VALUES (:id, :role, :perm) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": row_id, "role": role_id, "perm": perm_id},
            )
        await session.commit()
        after = await session.scalar(text("SELECT COUNT(*) FROM kh_role_permission"))
        print(f"kh_role_permission: {before} -> {after} 行（新增 {after - before} 行）")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
