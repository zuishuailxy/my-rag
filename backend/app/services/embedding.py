"""
文本向量服务（对应架构「嵌入」：sentence-transformers / DashScope API，1024 维）

- EmbeddingProvider：协议（async embed_texts）
- LocalDeterministicEmbedding：确定性伪向量（开发/测试，无外部依赖；
  基于 3-gram 激活维度，相似文本余弦相似度更高，可离线跑通链路）
- OpenAICompatibleEmbedding：任意 OpenAI 兼容 /embeddings 接口（DashScope 等）

配置：EMBEDDING_PROVIDER=local|openai，其余见 config.py。
"""
import hashlib
import logging
import math
import random
from typing import Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


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
            for _ in range(8):  # 每个 token 激活 8 个维度（带符号）
                d = rng.randrange(self.dim)
                vec[d] += 1.0 if rng.random() < 0.5 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAICompatibleEmbedding:
    """OpenAI 兼容 /embeddings 接口（DashScope 等）"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int = 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]


def _build_provider() -> EmbeddingProvider:
    if settings.EMBEDDING_PROVIDER == "openai":
        if not settings.EMBEDDING_BASE_URL or not settings.EMBEDDING_API_KEY:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai 但未配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY"
            )
        return OpenAICompatibleEmbedding(
            settings.EMBEDDING_BASE_URL,
            settings.EMBEDDING_API_KEY,
            settings.EMBEDDING_MODEL,
            settings.EMBEDDING_DIM,
        )
    logger.info("Embedding 使用本地确定性伪向量（开发/测试）")
    return LocalDeterministicEmbedding(settings.EMBEDDING_DIM)


# 全局单例
embedding_provider = _build_provider()
