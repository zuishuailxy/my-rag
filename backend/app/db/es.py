"""
Elasticsearch 客户端封装（对应 TS 各 service 的 new Client({ node })，amqp 同款生命周期管理）

- start：健康检查；ES 不可用时只降级（记日志、置不可用），不阻断应用启动（与 TS onModuleInit 一致）
- close：应用退出时关闭连接
- ensure_index：通用 create-if-not-exists（resource_already_exists 幂等），供后续
  kh_chunk / kh_document 等索引服务复用

注意：向量存 PostgreSQL（pgvector doc_chunk），ES 只做全文 / 关键词检索，
任何 mapping 都不要再写 dense_vector。
"""
import logging
from typing import Any

from elasticsearch import AsyncElasticsearch, BadRequestError

from app.core.config import settings

logger = logging.getLogger("ElasticsearchService")


class ElasticsearchService:
    """Elasticsearch 连接管理（单例）"""

    def __init__(self) -> None:
        self._client: AsyncElasticsearch | None = None

    @property
    def client(self) -> AsyncElasticsearch | None:
        """可用客户端；禁用 / 未连接 / 连接失败时为 None（调用方据此跳过写入）"""
        return self._client

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        """建连 + 健康检查；失败降级为不可用，不抛错（对齐 TS）"""
        if not settings.ELASTICSEARCH_ENABLED:
            logger.warning("Elasticsearch 已禁用（ELASTICSEARCH_ENABLED=false），检索索引将跳过写入")
            return

        node = settings.ELASTICSEARCH_NODE
        self._client = AsyncElasticsearch(hosts=[node])
        try:
            health = await self._client.cluster.health()
            logger.info("ES 已连接：%s, status=%s", node, health.get("status"))
        except Exception as err:
            logger.warning("Elasticsearch 不可用，检索索引将跳过写入：%s", err)
            await self.close()

    async def close(self) -> None:
        """关闭连接（应用退出时调用；未初始化时安全跳过）"""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def ensure_index(
        self,
        index: str,
        *,
        settings_body: dict[str, Any] | None = None,
        mappings: dict[str, Any],
    ) -> None:
        """create-if-not-exists（幂等，对应 TS createIndexIfNotExists）

        客户端不可用时静默跳过；创建失败（除已存在）记日志并抛出。
        """
        c = self._client
        if c is None:
            return
        if await c.indices.exists(index=index):
            return

        try:
            await c.indices.create(index=index, settings=settings_body, mappings=mappings)
            logger.info("ES 索引创建成功：index=%s", index)
        except BadRequestError as err:
            # 并发建索引兜底：已存在视为成功
            if "already_exists" in str(err):
                return
            logger.error("ES 索引创建失败：index=%s, error=%s", index, err)
            raise


# 全局单例
es_service = ElasticsearchService()
