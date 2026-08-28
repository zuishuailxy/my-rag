"""
文档分块服务（对应 TS ChunkingService，基于 LangChain RecursiveCharacterTextSplitter）

分块策略：
1. Markdown 感知分隔符（标题 / 代码块 / 水平线 / 段落…）递归切分；
2. chunkSize / chunkOverlap 控制块大小与重叠（token → 字符 ×2 换算）；
3. 从块内标题行推断 heading，跨块继承上一标题并前缀补全，检索命中带上下文。

配置：RAG_CHUNK_SIZE（默认 512 token）、RAG_CHUNK_OVERLAP（默认 64 token）。
"""
import hashlib
import logging
import re
from dataclasses import dataclass

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from app.core.config import settings

logger = logging.getLogger(__name__)

# token → 字符粗略换算（中英混合保守值：约 1 token ≈ 2 字符）
CHARS_PER_TOKEN = 2.0

# 匹配块内 Markdown ATX 标题行（对应 TS HEADING_LINE）
HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)

# TS 在 markdown 内置分隔符前补 '\n# '（一级标题），保持同样顺序
SEPARATORS = [
    "\n# ",
    *RecursiveCharacterTextSplitter.get_separators_for_language(Language.MARKDOWN),
]


@dataclass
class DocumentChunk:
    """分块结果（对应 TS types/pipeline.types 的 DocumentChunk）"""

    chunk_id: str
    document_id: str
    document_title: str
    content: str
    heading: str | None
    chunk_index: int
    total_chunks: int
    category_id: str | None = None
    author_id: str | None = None
    team_id: str | None = None
    doc_status: int | None = None
    publish_time: str | None = None
    # 嵌入向量（由调用方在 embedding 后填充，写入时使用）
    embedding: list[float] | None = None


class ChunkingService:
    """文档分块服务"""

    def __init__(self) -> None:
        chunk_size = int(settings.RAG_CHUNK_SIZE * CHARS_PER_TOKEN)
        chunk_overlap = int(settings.RAG_CHUNK_OVERLAP * CHARS_PER_TOKEN)
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            keep_separator=True,
            separators=SEPARATORS,
        )

    async def chunk(
        self,
        *,
        content: str,
        document_id: str,
        document_title: str,
        category_id: str | None = None,
        author_id: str | None = None,
        team_id: str | None = None,
        doc_status: int | None = None,
        publish_time: str | None = None,
    ) -> list[DocumentChunk]:
        """将一篇文档正文切成可索引的 DocumentChunk 列表；内容为空返回 []"""
        if not content or not content.strip():
            logger.warning("文档内容为空，跳过分块：documentId=%s", document_id)
            return []

        texts = self._splitter.split_text(content)
        chunks: list[DocumentChunk] = []
        current_heading: str | None = None

        for text in texts:
            trimmed = text.strip()
            if not trimmed:
                continue

            heading_in_chunk = extract_heading(trimmed)
            if heading_in_chunk:
                current_heading = heading_in_chunk

            # 同章节后续块通常不含标题行：前缀补上，便于检索命中时带上下文
            chunk_content = trimmed
            if current_heading and not HEADING_LINE.search(trimmed):
                chunk_content = f"{current_heading}\n\n{trimmed}"

            chunks.append(
                DocumentChunk(
                    chunk_id=hashlib.sha256(
                        f"{document_id}:{len(chunks)}".encode("utf-8")
                    ).hexdigest(),
                    document_id=document_id,
                    document_title=document_title,
                    content=chunk_content,
                    heading=current_heading,
                    chunk_index=len(chunks),
                    total_chunks=0,
                    category_id=category_id,
                    author_id=author_id,
                    team_id=team_id,
                    doc_status=doc_status,
                    publish_time=publish_time,
                )
            )

        total = len(chunks)
        for c in chunks:
            c.total_chunks = total

        logger.debug("文档分块完成：documentId=%s, totalChunks=%d", document_id, total)
        return chunks


def extract_heading(text: str) -> str | None:
    """取块内第一个 ATX 标题文案（不含 #）"""
    m = HEADING_LINE.search(text)
    return m.group(2).strip() if m else None


# 全局单例
chunking_service = ChunkingService()
