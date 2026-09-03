"""
KG 知识图谱构建（对应 TS GraphBuildService）

图模型（简化）：
    (KnowledgeDocument)-[:HAS_CHUNK]->(DocumentChunk)-[:MENTIONS]->(KnowledgeEntity)
    (KnowledgeEntity)-[:RELATED_TO]->(KnowledgeEntity)

单篇构建步骤：
1. 删除该文档旧图数据（clear before build）
2. MERGE 文档节点
3. ChunkingService 分块 → 每块建 DocumentChunk + HAS_CHUNK
4. ExtractionService 抽实体关系 → MERGE 实体 / RELATED_TO / MENTIONS

Neo4j 不可用时跳过写入（不抛错阻断发布消费）；
生命周期经 main.py lifespan 调 start()/close()（对应 TS OnModuleInit/OnModuleDestroy）。

唯一性约束：
    start() 幂等创建三个约束（KnowledgeDocument.id / DocumentChunk.chunkId /
    KnowledgeEntity.name）。**没有约束时 MERGE 不是原子操作**，并发建图会产生重复
    节点（曾观测到同一 documentId 出现 4 份 KnowledgeDocument）。加了约束后并发
    MERGE 同一 key 会拿到 transient error，由 _run_write 自动重试。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncSession,
    NotificationMinimumSeverity,
)
from neo4j.exceptions import TransientError

from app.core.config import settings
from app.pipeline.types import ExtractionResult, PipelineDocument, to_iso_date
from app.services.chunker import chunking_service
from app.services.kg_extraction import extraction_service

logger = logging.getLogger("GraphBuildService")

# 唯一性约束 DDL（IF NOT EXISTS 幂等，重复启动安全）
CONSTRAINT_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT knowledge_document_id IF NOT EXISTS "
    "FOR (d:KnowledgeDocument) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT document_chunk_chunk_id IF NOT EXISTS "
    "FOR (c:DocumentChunk) REQUIRE c.chunkId IS UNIQUE",
    "CREATE CONSTRAINT knowledge_entity_name IF NOT EXISTS "
    "FOR (e:KnowledgeEntity) REQUIRE e.name IS UNIQUE",
)

# transient 错误（并发 MERGE 撞唯一约束 / 死锁）最大尝试次数
WRITE_MAX_ATTEMPTS = 3


def _now_iso() -> str:
    """当前时间 ISO-8601 字符串（对应 TS new Date().toISOString()）"""
    return datetime.now(timezone.utc).isoformat()


class GraphBuildService:
    """KG 知识图谱构建（Neo4j）"""

    def __init__(self) -> None:
        self._enabled = settings.NEO4J_ENABLED
        self._driver: AsyncDriver | None = None

    async def start(self) -> None:
        """连接 Neo4j 并验证连通性（不可用只降级，不阻断启动）"""
        if not self._enabled:
            logger.warning("Neo4j 已禁用（NEO4J_ENABLED=false）")
            return
        self._driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        try:
            await self._driver.verify_connectivity()
            logger.info("Neo4j 已连接：%s", settings.NEO4J_URI)
        except Exception as err:
            logger.warning("Neo4j 不可用，KG 写入将跳过：%s", err)
            await self._driver.close()
            self._driver = None
            return
        await self._ensure_constraints()

    async def close(self) -> None:
        """关闭驱动（对应 TS onModuleDestroy）"""
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    async def _ensure_constraints(self) -> None:
        """幂等创建唯一性约束；失败只降级 WARN，不阻断启动

        缺失约束时 MERGE 在并发下不原子，会写出重复节点；
        但约束创建失败（如库里有历史重复数据）也不该拖垮整个发布链路。
        """
        if self._driver is None:
            return
        try:
            # 约束已存在时 Neo4j 会回 INFORMATION 级 SCHEMA 通知，默认被驱动打到
            # INFO 日志，每次启动刷三屏。这里把该 session 的通知阈值抬到 WARNING：
            # 只挡「已存在」这类提示，真正的告警仍会打出来。
            async with self._driver.session(
                notifications_min_severity=NotificationMinimumSeverity.WARNING
            ) as session:
                for statement in CONSTRAINT_STATEMENTS:
                    # consume() 不可省：不消费则服务端报错不会被观察到
                    await (await session.run(statement)).consume()
            logger.info(
                "Neo4j 唯一性约束已就绪：count=%d", len(CONSTRAINT_STATEMENTS)
            )
        except Exception as err:
            logger.warning(
                "Neo4j 约束创建失败（KG 写入继续，但并发下可能产生重复节点）：%s",
                err,
            )

    async def _run_write(
        self, session: AsyncSession, cypher: str, **params: Any
    ) -> None:
        """执行写语句并消费结果；transient 错误自动重试

        - consume()：不消费则服务端报错（如约束冲突）不会被观察到，错误会被静默吞掉。
        - 重试：加了唯一约束后，并发 MERGE 同一 key 时其中一个事务会拿到
          transient error（Neo4j 标准可重试错误），重试即可成功。
        """
        for attempt in range(1, WRITE_MAX_ATTEMPTS + 1):
            try:
                result = await session.run(cypher, **params)
                await result.consume()
                return
            except TransientError as err:
                if attempt == WRITE_MAX_ATTEMPTS:
                    logger.error("KG 写入重试耗尽：attempts=%d, %s", attempt, err)
                    raise
                logger.warning(
                    "KG 写入遇到 transient 错误，准备重试：attempt=%d/%d, %s",
                    attempt,
                    WRITE_MAX_ATTEMPTS,
                    err,
                )
                # 线性退避，避开并发窗口
                await asyncio.sleep(0.1 * attempt)

    async def build_for_document(self, doc: PipelineDocument) -> int:
        """为单篇文档全量重建图谱，返回写入的实体数量（近似）"""
        if self._driver is None:
            logger.warning("跳过 KG 构建（Neo4j 不可用）：documentId=%s", doc.id)
            return 0
        if not doc.content or not doc.content.strip():
            logger.info("文档内容为空，跳过 KG：documentId=%s", doc.id)
            return 0

        # 先清再建，避免重复发布导致边/节点翻倍
        await self.delete_for_document(doc.id)

        async with self._driver.session() as session:
            # ① 文档节点：按 id 幂等 upsert，保留首次 createdAt
            await self._run_write(session,
                """
                // 以文档业务 id 为唯一键：存在则命中，不存在则创建
                MERGE (d:KnowledgeDocument {id: $id})
                // 每次重建都刷新可变元数据；createdAt 仅首次写入
                SET d.title = $title, d.summary = $summary, d.categoryId = $categoryId,
                    d.authorId = $authorId, d.status = $status, d.updatedAt = $now,
                    d.createdAt = coalesce(d.createdAt, $now)
                """,
                id=doc.id,
                title=doc.title,
                summary=doc.summary or "",
                categoryId=doc.category_id,
                authorId=doc.author_id,
                status=doc.status,
                now=_now_iso(),
            )

            # ② 复用 RAG 同款分块，保证图谱粒度与向量块一致
            chunks = await chunking_service.chunk(
                content=doc.content,
                document_id=doc.id,
                document_title=doc.title,
                category_id=doc.category_id,
                author_id=doc.author_id,
                team_id=doc.team_id,
                doc_status=doc.status,
                publish_time=to_iso_date(doc.publish_time),
            )

            total_entities = 0
            for chunk in chunks:
                # ③ chunk 节点 + 文档→块边：Document -[HAS_CHUNK]-> Chunk
                await self._run_write(session,
                    """
                    // 以全局唯一 chunkId 幂等创建/更新块节点
                    MERGE (c:DocumentChunk {chunkId: $chunkId})
                    SET c.documentId = $documentId, c.content = $content, c.heading = $heading,
                        c.chunkIndex = $chunkIndex, c.totalChunks = $totalChunks, c.updatedAt = $now
                    // 携带 c 进入下一子句，避免丢失当前块上下文
                    WITH c
                    // 找到所属文档（① 已保证存在）
                    MATCH (d:KnowledgeDocument {id: $documentId})
                    // 文档→块 一对多边；边属性记序号便于按序遍历
                    MERGE (d)-[r:HAS_CHUNK]->(c)
                    SET r.chunkIndex = $chunkIndex
                    """,
                    chunkId=chunk.chunk_id,
                    documentId=doc.id,
                    content=chunk.content,
                    heading=chunk.heading,
                    chunkIndex=chunk.chunk_index,
                    totalChunks=chunk.total_chunks,
                    now=_now_iso(),
                )

                # ④ 抽实体关系并落图；单块失败不阻断其余块（图已先清过）
                try:
                    extracted = await extraction_service.extract(
                        chunk.content, chunk.heading, doc.title
                    )
                except Exception as err:
                    logger.error(
                        "KG 抽取失败，跳过该块：documentId=%s, chunk=%d, %s",
                        doc.id,
                        chunk.chunk_index,
                        err,
                    )
                    extracted = ExtractionResult()
                # 绑定当前块，write_extraction 才能建 MENTIONS
                extracted.chunk_id = chunk.chunk_id
                # 写入 Neo4j：实体节点 / MENTIONS / RELATED_TO
                written = await self._write_extraction(session, extracted)
                # 累加本块实体数，仅用于日志；图数据已在上一行入库
                total_entities += written

            logger.info(
                "KG 图谱构建完成：documentId=%s, chunks=%d, entities=%d",
                doc.id,
                len(chunks),
                total_entities,
            )
            return total_entities

    async def build_batch(self, docs: list[PipelineDocument]) -> None:
        """批量建图：单篇失败只记日志"""
        for doc in docs:
            try:
                await self.build_for_document(doc)
            except Exception as err:
                logger.error("KG 构建失败：documentId=%s, %s", doc.id, err)

    async def delete_for_document(self, document_id: str) -> None:
        """删除文档及其 chunk；再清理「已无人提及」的孤儿实体，避免图膨胀"""
        if self._driver is None:
            return
        async with self._driver.session() as session:
            # 删除文档节点及其所有 chunk（DETACH 会一并拆掉相连关系边）
            await self._run_write(session,
                """
                // 定位待删文档
                MATCH (d:KnowledgeDocument {id: $id})
                // 可选匹配下属块：无 chunk 时仍可删文档
                OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:DocumentChunk)
                // DETACH DELETE：先删节点上的所有关系，再删节点本身
                // 会清掉 HAS_CHUNK 等与 c/d 相连的边
                DETACH DELETE c, d
                """,
                id=document_id,
            )
            # 孤儿实体清理：没有任何 chunk MENTIONS 的实体视为无引用，整节点删除
            await self._run_write(session,
                """
                MATCH (e:KnowledgeEntity)
                // 入边 MENTIONS 为空 ⇒ 已无任何文档块引用该实体
                WHERE NOT (e)<-[:MENTIONS]-()
                // DETACH 同时清掉 RELATED_TO 等残留关系，避免悬空边
                DETACH DELETE e
                """
            )
        logger.info("KG 图谱已删除：documentId=%s", document_id)

    async def _write_extraction(
        self, session: AsyncSession, result: ExtractionResult
    ) -> int:
        """把抽取结果写入 Neo4j：

        - KnowledgeEntity（按 name MERGE，跨文档可复用同名实体）
        - DocumentChunk -[:MENTIONS]-> Entity
        - Entity -[:RELATED_TO]-> Entity
        """
        now = _now_iso()
        count = 0

        for entity in result.entities:
            await self._run_write(session,
                """
                MERGE (e:KnowledgeEntity {name: $name})
                ON CREATE SET e.type = $type, e.description = $description,
                              e.aliases = $aliases, e.createdAt = $now, e.updatedAt = $now
                ON MATCH SET e.type = coalesce($type, e.type),
                             e.description = CASE WHEN $description <> '' THEN $description ELSE e.description END,
                             e.updatedAt = $now
                """,
                name=entity.name,
                type=entity.type,
                description=entity.description or "",
                aliases=entity.aliases,
                now=now,
            )
            count += 1

            if result.chunk_id:
                await self._run_write(session,
                    """
                    MATCH (c:DocumentChunk {chunkId: $chunkId})
                    MATCH (e:KnowledgeEntity {name: $name})
                    MERGE (c)-[:MENTIONS]->(e)
                    """,
                    chunkId=result.chunk_id,
                    name=entity.name,
                )

        for rel in result.relations:
            await self._run_write(session,
                """
                MATCH (a:KnowledgeEntity {name: $source})
                MATCH (b:KnowledgeEntity {name: $target})
                MERGE (a)-[r:RELATED_TO]->(b)
                ON CREATE SET r.relation = $relType, r.weight = $weight, r.createdAt = datetime()
                ON MATCH SET r.weight = coalesce($weight, r.weight)
                """,
                source=rel.source,
                target=rel.target,
                relType=rel.relation,
                weight=rel.weight,
            )

        return count

    async def list_nodes(
        self, type: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """查询知识实体节点（对应 TS listNodes）。Neo4j 不可用时返回 []。"""
        if self._driver is None:
            logger.warning("跳过图谱节点查询（Neo4j 不可用）")
            return []
        cap = min(max(limit, 1), 500)
        async with self._driver.session() as session:
            try:
                result = await session.run(
                    """
                    MATCH (e:KnowledgeEntity)
                    WHERE $type IS NULL OR $type = '' OR e.type = $type
                    RETURN e.name AS id, e.name AS name, e.type AS type,
                           e.description AS description
                    LIMIT $limit
                    """,
                    type=type,
                    limit=cap,
                )
                records = await result.data()
            except Exception as err:
                logger.warning("图谱节点查询失败：%s", err)
                return []
        return [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "type": row.get("type"),
                "description": row.get("description"),
            }
            for row in records
        ]

    async def list_edges(self, limit: int = 500) -> list[dict[str, Any]]:
        """查询实体间 RELATED_TO 边（对应 TS listEdges）。Neo4j 不可用时返回 []。"""
        if self._driver is None:
            logger.warning("跳过图谱边查询（Neo4j 不可用）")
            return []
        cap = min(max(limit, 1), 1000)
        async with self._driver.session() as session:
            try:
                result = await session.run(
                    """
                    MATCH (a:KnowledgeEntity)-[r:RELATED_TO]->(b:KnowledgeEntity)
                    RETURN a.name AS source, b.name AS target,
                           r.relation AS relation, r.weight AS weight
                    LIMIT $limit
                    """,
                    limit=cap,
                )
                records = await result.data()
            except Exception as err:
                logger.warning("图谱边查询失败：%s", err)
                return []
        return [
            {
                "source": row.get("source"),
                "target": row.get("target"),
                "relation": row.get("relation") or "RELATED_TO",
                "weight": _to_number(row.get("weight"), 0.5),
            }
            for row in records
        ]

    async def search_graph(self, keyword: str, limit: int = 50) -> list[dict[str, Any]]:
        """图谱关键词检索：匹配实体名/描述、文档标题/摘要、块标题/正文。

        对应 TS searchGraph。Neo4j 不可用或关键词为空时返回 []。
        """
        if self._driver is None:
            logger.warning("跳过图谱检索（Neo4j 不可用）")
            return []
        kw = keyword.strip()
        if not kw:
            return []
        cap = min(max(limit, 1), 200)
        async with self._driver.session() as session:
            try:
                result = await session.run(
                    """
                    MATCH (n)
                    WHERE toLower(coalesce(n.name, '')) CONTAINS toLower($kw)
                       OR toLower(coalesce(n.title, '')) CONTAINS toLower($kw)
                       OR toLower(coalesce(n.heading, '')) CONTAINS toLower($kw)
                       OR toLower(coalesce(n.description, '')) CONTAINS toLower($kw)
                       OR toLower(coalesce(n.summary, '')) CONTAINS toLower($kw)
                       OR toLower(coalesce(n.content, '')) CONTAINS toLower($kw)
                    RETURN labels(n)[0] AS label,
                           coalesce(n.name, n.title, n.heading, n.id, n.chunkId) AS name,
                           coalesce(n.id, n.chunkId, n.name) AS id,
                           n.type AS type,
                           n.title AS title,
                           n.description AS description,
                           n.heading AS heading,
                           n.documentId AS documentId,
                           n.summary AS summary,
                           CASE
                             WHEN n.content IS NULL THEN null
                             ELSE substring(n.content, 0, 160)
                           END AS snippet
                    ORDER BY label, name
                    LIMIT $limit
                    """,
                    kw=kw,
                    limit=cap,
                )
                records = await result.data()
            except Exception as err:
                logger.warning("图谱检索失败：%s", err)
                return []
        return [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "label": row.get("label"),
                "type": row.get("type"),
                "title": row.get("title"),
                "description": row.get("description"),
                "heading": row.get("heading"),
                "documentId": row.get("documentId"),
                "summary": row.get("summary"),
                "snippet": row.get("snippet"),
            }
            for row in records
        ]


def _to_number(value: Any, default: float) -> float:
    """Neo4j 数值字段统一转 float（对齐 TS toNumber）。

    None/非数字回退 default；int/float 直接转 float。
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 全局单例
graph_build_service = GraphBuildService()
