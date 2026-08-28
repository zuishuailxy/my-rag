"""
Markdown 分块（对应架构「自动切片生成 chunk」）

简单启发式：按段落（空行分隔）聚合到目标长度，超长段落硬切并保留重叠。
真实场景可替换为结构感知分块（按标题层级优先切分）。
"""
import re

DEFAULT_CHUNK_SIZE = 500
DEFAULT_OVERLAP = 50


def chunk_markdown(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """将 Markdown 文本切分为分块列表（确定性）"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for p in paragraphs:
        if not current or len(current) + len(p) + 2 <= chunk_size:
            current = f"{current}\n\n{p}" if current else p
            continue
        if current:
            chunks.append(current)
            current = ""
        # 超长段落硬切（带重叠）
        while len(p) > chunk_size:
            chunks.append(p[:chunk_size])
            p = p[chunk_size - overlap :]
        current = p

    if current:
        chunks.append(current)
    return chunks
