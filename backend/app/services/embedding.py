"""
文本向量服务

把 chunk 文本变成固定维度浮点向量，供语义检索。

- LocalDeterministicEmbedding：确定性伪向量（开发/测试，无外部依赖，
  基于 3-gram 激活维度，相似文本余弦更高，可离线跑通链路）
- OpenAICompatibleEmbedding：OpenAI 兼容 /embeddings（DashScope 百炼等），
  基于 LangChain OpenAIEmbeddings；
  batch 钳制 ≤10（DashScope 上限）、正文保留换行

配置见 config.py（EMBEDDING_PROVIDER / BASE_URL / MODEL / DIMENSION / BATCH_SIZE）。
"""
import hashlib
import logging
import math
import random
from typing import Protocol

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.core.config import settings

logger = logging.getLogger("EmbeddingService")

# DashScope text-embedding-v3 单次最多条数上限
_DASHSCOPE_BATCH_LIMIT = 10


class EmbeddingProvider(Protocol):
    """向量服务协议"""

    dim: int

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class LocalDeterministicEmbedding:
    """确定性伪向量：同文本同向量，重叠 3-gram 越多余弦相似度越高"""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = [text[i : i + 3] for i in range(max(0, len(text) - 2))] or ["<e>"]
        vec = [0.0] * self.dim
        for token in tokens:
            rng = random.Random(hashlib.md5(token.encode("utf-8")).digest())
            for _ in range(8): # 每个 token 激活 8 个维度（带符号）
                d = rng.randrange(self.dim)
                vec[d] += 1.0 if rng.random() < 0.5 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAICompatibleEmbedding:
    """OpenAI 兼容 /embeddings 接口（DashScope 百炼等）
    - API Key fallback：EMBEDDING_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
    - batchSize 钳制到 10（DashScope 单次上限，超出 400 InvalidParameter）
    - 正文原样发送（保留换行，对齐 stripNewLines=false）

    注意：langchain-openai 1.x 中 batch_size / strip_new_lines 已不是参数
    （传入会被塞进 model_kwargs 并告警），分批由本类自行处理。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int = 1024,
        batch_size: int = 10,
    ) -> None:
        self.dim = dim
        # 钳制到 DashScope 上限
        self.batch_size = max(1, min(batch_size, _DASHSCOPE_BATCH_LIMIT))
        if batch_size > _DASHSCOPE_BATCH_LIMIT:
            logger.warning(
                "EMBEDDING_BATCH_SIZE=%d 超过 DashScope 上限，已钳制为 %d",
                batch_size,
                _DASHSCOPE_BATCH_LIMIT,
            )
        self._embeddings = OpenAIEmbeddings(
            api_key=SecretStr(api_key),
            model=model,
            dimensions=dim,
            base_url=base_url,
            # 不做本地 token 长度检查
            check_embedding_ctx_length=False,
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for batch in _split_batches(texts, self.batch_size):
            vectors.extend(await self._embeddings.aembed_documents(batch))
        logger.info("嵌入完成：count=%d", len(vectors))
        return vectors


def _split_batches(texts: list[str], size: int) -> list[list[str]]:
    """按 batch 大小切片（内部分批，避免超 DashScope 单次上限）"""
    return [texts[i : i + size] for i in range(0, len(texts), size)]


def _resolve_api_key() -> str | None:
    """API Key fallback 链"""
    return (
        settings.EMBEDDING_API_KEY
        or settings.DASHSCOPE_API_KEY
        or settings.OPENAI_API_KEY
    )


def _build_provider() -> EmbeddingProvider:
    if settings.EMBEDDING_PROVIDER == "openai":
        api_key = _resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "未配置 EMBEDDING_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY"
            )
        return OpenAICompatibleEmbedding(
            settings.EMBEDDING_BASE_URL,
            api_key,
            settings.EMBEDDING_MODEL,
            settings.EMBEDDING_DIMENSION,
            settings.EMBEDDING_BATCH_SIZE,
        )
    logger.info("Embedding 使用本地确定性伪向量（开发/测试）")
    return LocalDeterministicEmbedding(settings.EMBEDDING_DIMENSION)


# 全局单例
embedding_provider = _build_provider()
