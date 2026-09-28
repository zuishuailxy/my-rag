"""文档分块纯逻辑：块序号 / 稳定 ID / heading 继承。"""
import hashlib

from app.services.chunker import chunking_service, extract_heading


async def test_empty_content_returns_empty() -> None:
    assert await chunking_service.chunk(content="   ", document_id="d1", document_title="T") == []


async def test_chunk_index_and_total() -> None:
    content = "# 第一章\n\n内容一。\n\n# 第二章\n\n内容二。"
    chunks = await chunking_service.chunk(
        content=content, document_id="d1", document_title="标题"
    )
    assert chunks, "应有分块"
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.total_chunks == len(chunks) for c in chunks)


async def test_chunk_id_stable() -> None:
    chunks = await chunking_service.chunk(
        content="# 标题\n\n正文", document_id="d1", document_title="T"
    )
    # 稳定 ID：sha256(documentId:index)
    assert chunks[0].chunk_id == hashlib.sha256(b"d1:0").hexdigest()
    # 同样入参产出同样 ID（重建可覆盖）
    again = await chunking_service.chunk(
        content="# 标题\n\n正文", document_id="d1", document_title="T"
    )
    assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]


def test_extract_heading() -> None:
    assert extract_heading("## 子标题\n正文") == "子标题"
    assert extract_heading("无标题正文") is None


async def test_configure_changes_chunk_count() -> None:
    content = "# 标题\n\n" + ("这是一句用于分块的正文内容。" * 200)
    try:
        chunking_service.configure(128, 16)
        small = await chunking_service.chunk(
            content=content, document_id="d1", document_title="T"
        )
    finally:
        chunking_service.configure()  # 恢复默认（读 settings）
    default = await chunking_service.chunk(
        content=content, document_id="d1", document_title="T"
    )
    assert len(small) > len(default), "更小的 chunk size 应切出更多块"
