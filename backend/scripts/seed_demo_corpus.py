"""导入合成演示语料（`eval/corpus/*.md`）

**为什么要合成语料**
    评测集要拉开区分度，前提是知识库里存在**强干扰项** —— 同领域、词汇重叠、
    数字不同的文档。例如「会议与培训费用管理办法」与「差旅与费用报销管理办法」
    都讲住宿费上限、都要求发票抬头、都要 Leader 审批，只是数字不同。
    没有干扰项时，任何检索参数都能把正确文档排第一，指标必然饱和
    （实测：仅 5 篇文档时，hybrid_top_k / rrf_c 怎么扫都是 1.00）。

⚠️ `eval/corpus/` 是**演示数据**，不是真实制度，不要据此执行任何公司流程。

**流程**（复用线上同一套服务，只是绕过 MQ 直接索引）：
    1. `DocumentService.create`        → 写 Mongo 正文 + PG 元数据
    2. `DocumentService.direct_publish` → 置为 Published（免审）
    3. `reindex_document`              → 分块 → 嵌入 → PG 向量 + ES kh_chunk
    4. `search_index_service`          → ES kh_document（文档级全文检索）

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/seed_demo_corpus.py          # 导入（幂等：按标题跳过）
        .venv/bin/python scripts/seed_demo_corpus.py --wipe   # 回滚（删除这批语料）
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.db.es import es_service  # noqa: E402
from app.db.mongo import get_document_content_collection  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.models.document import DocumentEntity  # noqa: E402
from app.pipeline.types import to_search_index_doc  # noqa: E402
from app.schemas.auth import AuthUser  # noqa: E402
from app.schemas.document import DocumentCreate  # noqa: E402
from app.services.document import DocumentService  # noqa: E402
from app.services.indexing import (  # noqa: E402
    VectorIndexService,
    load_pipeline_document,
    reindex_document,
)
from app.services.search_index import search_index_service  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

CORPUS_DIR = Path(__file__).resolve().parent.parent / "eval" / "corpus"
# 与既有演示文档同一作者（admin），保证可见性/权限字段一致
SEED_AUTHOR = "1000000000000000001"

_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _actor() -> AuthUser:
    """构造导入用的操作者（admin；服务层用其 userId 写 authorId/createBy）"""
    return AuthUser(
        user_id=SEED_AUTHOR,
        username="admin",
        real_name="系统管理员",
        roles=["admin"],
        permissions=["document:edit", "document:publish"],
    )


def load_corpus() -> list[tuple[str, str]]:
    """读 `eval/corpus/*.md` → [(标题, 正文)]；标题取首个一级标题"""
    if not CORPUS_DIR.is_dir():
        raise SystemExit(f"语料目录不存在：{CORPUS_DIR}")

    items: list[tuple[str, str]] = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = _TITLE_RE.search(text)
        if match is None:
            raise SystemExit(f"{path.name} 缺少 `# 标题` 一级标题")
        items.append((match.group(1).strip(), text))
    if not items:
        raise SystemExit(f"语料目录为空：{CORPUS_DIR}")
    return items


async def _index(session, doc_id: int) -> None:
    """重建单篇索引：PG 向量 + ES kh_chunk + ES kh_document"""
    doc = await load_pipeline_document(session, doc_id)
    if doc is None:
        print(f"        ⚠️ 文档 {doc_id} 加载失败，跳过索引")
        return
    chunks = await reindex_document(session, doc)
    await search_index_service.index_document(to_search_index_doc(doc))
    print(f"        索引完成：{chunks} 块 chunk + 1 条文档级检索")


async def _seed(reindex: bool) -> int:
    corpus = load_corpus()
    service = DocumentService()
    actor = _actor()
    created = 0

    async with async_session_factory() as session:
        for title, content in corpus:
            existing = (
                await session.execute(
                    select(DocumentEntity).where(
                        DocumentEntity.title == title,
                        DocumentEntity.deleted.is_(False),
                    )
                )
            ).scalars().first()

            if existing is not None:
                # 已存在就**不重建索引**：重建要重新嵌入，白烧额度；
                # 需要强制重建时显式传 --reindex
                if not reindex:
                    print(f"  跳过（已存在）：{title}")
                    continue
                doc_id = int(existing.id)
                print(f"  重建索引：{title}  id={doc_id}")
            else:
                vo = await service.create(
                    session,
                    DocumentCreate(title=title, content=content, is_public=True),
                    actor,
                )
                doc_id = int(vo["id"])
                await service.direct_publish(session, str(doc_id), actor)
                created += 1
                print(f"  导入：{title}  id={doc_id}")

            await _index(session, doc_id)

    print(f"\n完成：新增 {created} 篇；语料目录共 {len(corpus)} 篇")
    return 0


async def _wipe() -> int:
    """回滚：删除本批语料（PG 元数据 + Mongo 正文 + PG/ES 块 + ES 文档级索引）"""
    titles = [title for title, _ in load_corpus()]

    async with async_session_factory() as session:
        entities = (
            await session.execute(
                select(DocumentEntity).where(DocumentEntity.title.in_(titles))
            )
        ).scalars().all()

        vector_service = VectorIndexService()
        for entity in entities:
            doc_id = int(entity.id)
            await vector_service.delete_by_doc_id(session, doc_id)  # PG + ES kh_chunk
            await search_index_service.delete_document(str(doc_id))  # ES kh_document
            await get_document_content_collection().delete_one(
                {"documentId": str(doc_id)}
            )
            await session.delete(entity)
            print(f"  已删除：{entity.title}")
        await session.commit()

    print(f"\n完成：删除 {len(entities)} 篇")
    return 0


async def main(wipe: bool, reindex: bool) -> int:
    await es_service.start()
    try:
        return await _wipe() if wipe else await _seed(reindex)
    finally:
        await es_service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入 / 回滚合成演示语料")
    parser.add_argument("--wipe", action="store_true", help="删除这批语料（回滚）")
    parser.add_argument(
        "--reindex", action="store_true", help="对已存在的文档也强制重建索引"
    )
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed.wipe, parsed.reindex)))
