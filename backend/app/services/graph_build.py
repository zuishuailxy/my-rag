"""
KG 知识图谱构建

图模型（简化）：
    (KnowledgeDocument)-[:HAS_CHUNK]->(DocumentChunk)-[:MENTIONS]->(KnowledgeEntity)
    (KnowledgeEntity)-[:RELATED_TO]->(KnowledgeEntity)

单篇构建步骤：
1. 删除该文档旧图数据（clear before build）
2. MERGE 文档节点
3. ChunkingService 分块 → 每块建 DocumentChunk + HAS_CHUNK
4. ExtractionService 抽实体关系 → MERGE 实体 / RELATED_TO / MENTIONS

Neo4j 不可用时跳过写入（不抛错阻断发布消费）；
生命周期经 main.py lifespan 调 start()/close()。

唯一性约束：
    start() 幂等创建三个约束（KnowledgeDocument.id / DocumentChunk.chunkId /
    KnowledgeEntity.name）。**没有约束时 MERGE 不是原子操作**，并发建图会产生重复
    节点（曾观测到同一 documentId 出现 4 份 KnowledgeDocument）。加了约束后并发
    MERGE 同一 key 会拿到 transient error，由 _run_write 自动重试。
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, TypedDict

from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncSession,
    NotificationMinimumSeverity,
)
from neo4j.exceptions import TransientError

from app.common.document_access import (
    DocumentAccessScope,
    neo4j_access_params,
    neo4j_document_access_where,
)
from app.core.config import settings
from app.pipeline.types import ExtractionResult, PipelineDocument, to_iso_date
from app.services.chunker import chunking_service
from app.services.kg_extraction import extraction_service

logger = logging.getLogger("GraphBuildService")

# 唯一性约束 DDL
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
    """当前时间 ISO-8601 字符串（).toISOString()）"""
    return datetime.now(timezone.utc).isoformat()


def _evidence_snippet(
    content: str | None, *names: str, window: int = 180
) -> str | None:
    """从关系所在 chunk 里取「实体附近」的片段作为出处（找不到实体则取开头）。

    关系的出处就是抽取它的 chunk（写入时记在 r.chunkId）；这里只截实体附近的窗口，
    避免把整块塞进模型上下文。
    """
    if not content:
        return None
    text = re.sub(r"\s+", " ", content).strip()
    idx = -1
    for name in names:
        if not name:
            continue
        pos = text.find(name)
        if pos >= 0 and (idx < 0 or pos < idx):
            idx = pos
    if idx < 0:
        return text[:window] + ("…" if len(text) > window else "")
    start = max(0, idx - window // 2)
    end = min(len(text), start + window)
    return (
        ("…" if start > 0 else "")
        + text[start:end]
        + ("…" if end < len(text) else "")
    )


def _opt_str(value: Any) -> str | None:
    """bigint / int / str / None → 字符串 / None

    Neo4j 侧把 authorId / teamId 统一存成**字符串**：雪花 ID 超过 JS Number 安全整数
    范围，且 KG 查询参数来自 DocumentAccessScope（user_id / team_ids 本就是字符串），
    两侧类型必须一致，否则 `d.teamId IN $accessTeamIds` 会静默不命中 ——
    表现为「团队可见的文档检索不到」，且不报错。空串按 None 处理（等价于未设置）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


# ---- 对话 RAG 用的图谱命中----


class GraphChatEntity(TypedDict):
    name: str
    type: str | None
    description: str | None


class GraphChatRelation(TypedDict):
    source: str
    relation: str
    target: str
    # 关系出处：抽取该关系的 chunk 里、两个实体附近的片段
    #（None = 旧边无 chunkId，需重建图谱才有）
    evidence: str | None


class GraphChatDocument(TypedDict):
    documentId: str
    title: str


class GraphChatHit(TypedDict):
    """对话 RAG 用的图谱命中：实体 + 关系类型 + 来源文档"""

    query: str
    entities: list[GraphChatEntity]
    relations: list[GraphChatRelation]
    documents: list[GraphChatDocument]


# 书名号：抽取常把同一实体写成《X》和 X 两种形式，匹配/MERGE 前统一去掉
_BOOK_TITLE_TRANS = str.maketrans("", "", "《》")


def normalize_entity_name(name: str) -> str:
    """实体名归一：去掉书名号《》及首尾空白（《发票管理办法》→ 发票管理办法）。

    抽取对同一实体可能产出带/不带书名号两种写法，而 KnowledgeEntity 按 name 建唯一
    约束，不归一会并存两个节点（实测《发票管理办法》与发票管理办法）。归一把它们并到
    一个节点，原始写法进 aliases 保留可检索。
    """
    return (name or "").translate(_BOOK_TITLE_TRANS).strip()


def split_graph_keywords(keyword: str | list[str]) -> list[str]:
    """把关键词拆成短名。

    模型常把「发票 报销」写成一项；图匹配要拆成短名，否则对不上节点。
    按空白 / `/` 拆分，去空、去重（大小写不敏感）；并去书名号（与实体名归一乎）。
    """
    seen: set[str] = set()
    kws: list[str] = []
    parts = keyword if isinstance(keyword, list) else [keyword]
    for part in parts:
        for raw in re.split(r"[\s/]+", str(part)):
            kw = normalize_entity_name(raw)
            if not kw:
                continue
            key = kw.lower()
            if key in seen:
                continue
            seen.add(key)
            kws.append(kw)
    return kws


def format_graph_context(hit: GraphChatHit) -> str:
    """给模型看的图谱上下文：只写关系与来源，不当制度原文"""
    if not hit["entities"] and not hit["relations"]:
        return ""
    lines: list[str] = []
    if hit["relations"]:
        lines.append("关系：")
        for rel in hit["relations"]:
            line = f"- {rel['source']} → {rel['target']}（{rel['relation']}）"
            evidence = rel.get("evidence")
            if evidence:
                line += f"\n 出处：{evidence}"
            lines.append(line)
    else:
        lines.append("实体：" + "、".join(entity["name"] for entity in hit["entities"]))
    if hit["documents"]:
        titles = [doc["title"] for doc in hit["documents"] if doc["title"]]
        if titles:
            lines.append("来源文档：" + "；".join(titles))
    return "\n".join(lines)


def format_graph_system_text(hit: GraphChatHit) -> str:
    """带免责声明的图谱上下文"""
    body = format_graph_context(hit)
    if not body:
        return ""
    return (
        f"【本轮知识图谱】\n{body}\n\n"
        "以上只说明实体之间的关系，不能当制度原文；制度/流程以知识库检索资料为准。"
    )


def merge_graph_hits(base: GraphChatHit, extra: GraphChatHit) -> GraphChatHit:
    """合并两次图谱命中，按实体名 / 关系三元组 / 文档 id 去重"""
    names = {entity["name"] for entity in base["entities"]}
    entities = list(base["entities"])
    for entity in extra["entities"]:
        if entity["name"] in names:
            continue
        names.add(entity["name"])
        entities.append(entity)

    rel_keys = {
        f"{rel['source']}\t{rel['relation']}\t{rel['target']}"
        for rel in base["relations"]
    }
    relations = list(base["relations"])
    for rel in extra["relations"]:
        key = f"{rel['source']}\t{rel['relation']}\t{rel['target']}"
        if key in rel_keys:
            continue
        rel_keys.add(key)
        relations.append(rel)

    doc_ids = {doc["documentId"] for doc in base["documents"]}
    documents = list(base["documents"])
    for doc in extra["documents"]:
        if doc["documentId"] in doc_ids:
            continue
        doc_ids.add(doc["documentId"])
        documents.append(doc)

    queries = list(
        dict.fromkeys(
            q.strip()
            for q in (f"{base['query']}/{extra['query']}").split("/")
            if q.strip()
        )
    )
    return {
        "query": " / ".join(queries),
        "entities": entities,
        "relations": relations,
        "documents": documents,
    }


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
        """关闭驱动"""
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
                "Neo4j 唯一性约束已就绪：count=%d", len
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
                // authorId / teamId / isPublic 是查询侧可见性判定的依据
                //（见 common/document_access.neo4j_document_access_where）：
                // 缺任一项，该文档在 KG 里除管理员/审核员外全图不可见
                SET d.title = $title, d.summary = $summary, d.categoryId = $categoryId,
                    d.authorId = $authorId, d.teamId = $teamId, d.isPublic = $isPublic,
                    d.status = $status, d.tags = $tags, d.updatedAt = $now,
                    d.createdAt = coalesce(d.createdAt, $now)
                """,
                id=doc.id,
                title=doc.title,
                summary=doc.summary or "",
                categoryId=doc.category_id,
                authorId=_opt_str(doc.author_id),
                teamId=_opt_str(doc.team_id),

                isPublic=bool(doc.is_public),
                status=doc.status,
# ''`（overview 的文档→标签边按逗号拆）
                tags=doc.tags or "",
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
                # 可见性字段冗余进 chunk（与 indexing.reindex_document 同口径；
                # 图谱侧 chunk 节点本身不带可见性，判定一律回溯所属文档节点）
                is_public=bool(doc.is_public),
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

    async def update_visibility(
        self,
        document_id: str | int,
        is_public: bool,
        team_id: str | int | None,
        author_id: str | int | None,
    ) -> None:
        """已发布文档只改公开 / 团队 / 作者时，就地刷文档节点属性

        与 es_index / search_index 的同名方法同口径（签名一致，document.py 三处并列调用）：
        免去重新抽实体建图，只改可见性字段。

        为什么必须同步：查询侧四个方法都按 neo4j_document_access_where 过滤文档节点，
        文档由公开转私有后若不同步，未授权用户在 /graph/nodes、/graph/search、
        /graph/edges、/graph/overview 仍能看到该文档及其提及的实体。

        Neo4j 不可用或写入失败只告警，不抛错。
        """
        if self._driver is None:
            logger.warning(
                "跳过图谱可见性更新（Neo4j 不可用）：documentId=%s", document_id
            )
            return
        try:
            async with self._driver.session() as session:
                await self._run_write(session,
                    """
                    MATCH (d:KnowledgeDocument {id: $id})
                    SET d.isPublic = $isPublic, d.teamId = $teamId, d.authorId = $authorId,
                        d.updatedAt = $now
                    """,
                    # 节点 id 建图时写的是 str(entity.id)，此处同口径转字符串
                    id=str(document_id),
                    isPublic=bool(is_public),
                    teamId=_opt_str(team_id),
                    authorId=_opt_str(author_id),
                    now=_now_iso(),
                )
        except Exception as err: # noqa: BLE001
            logger.warning(
                "图谱可见性更新失败：documentId=%s, %s", document_id, err
            )
            return
        logger.info("图谱可见性已更新：documentId=%s", document_id)

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
            # 归一实体名（去书名号）：把《X》与 X 并到一个节点
            name = normalize_entity_name(entity.name)
            if not name:
                continue
            aliases = list(entity.aliases)
            if entity.name != name and entity.name not in aliases:
                aliases.append(entity.name) # 原始写法（带《）进别名
            await self._run_write(session,
                """
                MERGE (e:KnowledgeEntity {name: $name})
                ON CREATE SET e.type = $type, e.description = $description,
                              e.aliases = $aliases, e.createdAt = $now, e.updatedAt = $now
                ON MATCH SET e.type = coalesce($type, e.type),
                             e.description = CASE WHEN $description <> '' THEN $description ELSE e.description END,
                             e.aliases = coalesce(e.aliases, [])
                               + [a IN $aliases WHERE NOT a IN coalesce(e.aliases, [])],
                             e.updatedAt = $now
                """,
                name=name,
                type=entity.type,
                description=entity.description or "",
                aliases=aliases,
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
                    name=name,
                )

        for rel in result.relations:
            source = normalize_entity_name(rel.source)
            target = normalize_entity_name(rel.target)
            if not source or not target:
                continue
            await self._run_write(session,
                """
                MATCH (a:KnowledgeEntity {name: $source})
                MATCH (b:KnowledgeEntity {name: $target})
                MERGE (a)-[r:RELATED_TO]->(b)
                ON CREATE SET r.relation = $relType, r.weight = $weight,
                              r.chunkId = $chunkId, r.createdAt = datetime()
                ON MATCH SET r.weight = coalesce($weight, r.weight),
                             r.chunkId = coalesce(r.chunkId, $chunkId)
                """,
                source=source,
                target=target,
                relType=rel.relation,
                weight=rel.weight,
                # 关系出处：记下抽取它的 chunk，供对话检索回取片段
                chunkId=result.chunk_id,
            )

        return count

    async def list_nodes(
        self,
        type: str | None = None,
        limit: int = 200,
        scope: DocumentAccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """查询知识实体节点。Neo4j 不可用时返回 []。

        可见性：实体只要被「当前用户可见的某篇文档」的某个块提及过即可见；
        scope=None 表示不过滤，
        API 端点必须传 access_from_user(user)。
        """
        if self._driver is None:
            logger.warning("跳过图谱节点查询（Neo4j 不可用）")
            return []
        cap = min(max(limit, 1), 500)
        async with self._driver.session() as session:
            try:
                result = await session.run(
                    f"""
                    MATCH (e:KnowledgeEntity)
                    WHERE ($type IS NULL OR $type = '' OR e.type = $type)
                      AND (
                        $unrestricted OR EXISTS {{
                          MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                                -[:MENTIONS]->(e)
                          WHERE {neo4j_document_access_where('d')}
                        }}
                      )
                    RETURN DISTINCT e.name AS id, e.name AS name, e.type AS type,
                           e.description AS description
                    LIMIT $limit
                    """,
                    type=type,
                    limit=cap,
                    **neo4j_access_params(scope),
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

    async def list_edges(
        self, limit: int = 500, scope: DocumentAccessScope | None = None
    ) -> list[dict[str, Any]]:
        """查询实体间 RELATED_TO 边。Neo4j 不可用时返回 []。

        可见性：边的两端实体各自都要「被当前用户可见的某篇文档提及」；
        只遮挡一端会让画布出现指向不可见实体的悬空边。
        scope=None 表示不过滤。
        """
        if self._driver is None:
            logger.warning("跳过图谱边查询（Neo4j 不可用）")
            return []
        cap = min(max(limit, 1), 1000)
        async with self._driver.session() as session:
            try:
                result = await session.run(
                    f"""
                    MATCH (a:KnowledgeEntity)-[r:RELATED_TO]->(b:KnowledgeEntity)
                    WHERE $unrestricted OR (
                      EXISTS {{
                        MATCH (d1:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                              -[:MENTIONS]->(a)
                        WHERE {neo4j_document_access_where('d1')}
                      }}
                      AND EXISTS {{
                        MATCH (d2:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                              -[:MENTIONS]->(b)
                        WHERE {neo4j_document_access_where('d2')}
                      }}
                    )
                    RETURN a.name AS source, b.name AS target,
                           r.relation AS relation, r.weight AS weight
                    LIMIT $limit
                    """,
                    limit=cap,
                    **neo4j_access_params(scope),
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

    async def search_graph(
        self,
        keyword: str,
        limit: int = 50,
        scope: DocumentAccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """图谱关键词检索：匹配实体名/描述、文档标题/摘要、块标题/正文。。Neo4j 不可用或关键词为空时返回 []。

        可见性：按命中节点的 label 分别回溯所属文档 —— 文档节点自身判定；块节点回溯
        HAS_CHUNK 的文档；实体节点回溯「提及它的块所属文档」。scope=None 表示不过滤。
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
                    f"""
                    MATCH (n)
                    WHERE (
                         toLower(coalesce(n.name, '')) CONTAINS toLower($kw)
                      OR toLower(coalesce(n.title, '')) CONTAINS toLower($kw)
                      OR toLower(coalesce(n.heading, '')) CONTAINS toLower($kw)
                      OR toLower(coalesce(n.description, '')) CONTAINS toLower($kw)
                      OR toLower(coalesce(n.summary, '')) CONTAINS toLower($kw)
                      OR toLower(coalesce(n.content, '')) CONTAINS toLower($kw)
                    )
                    AND (
                      $unrestricted
                      OR (n:KnowledgeDocument AND {neo4j_document_access_where('n')})
                      OR (n:DocumentChunk AND EXISTS {{
                        MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(n)
                        WHERE {neo4j_document_access_where('d')}
                      }})
                      OR (n:KnowledgeEntity AND EXISTS {{
                        MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                              -[:MENTIONS]->(n)
                        WHERE {neo4j_document_access_where('d')}
                      }})
                    )
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
                    **neo4j_access_params(scope),
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

    async def retrieve_for_chat(
        self,
        keyword: str | list[str],
        limit: int = 8,
        scope: DocumentAccessScope | None = None,
    ) -> GraphChatHit:
        """对话 RAG 图谱检索。

        按短实体名找可见实体（相等 / 前缀 / 后缀 + 别名，**不用 CONTAINS**，避免「发票」
        命中整篇制度标题），再查这些实体之间的关系与来源文档。关系、文档用同一 session
        顺序查，避免并发共用 session 丢边。Neo4j 不可用 / 无关键词时返回空命中。
        """
        kws = split_graph_keywords(keyword)
        empty: GraphChatHit = {
            "query": " / ".join(kws),
            "entities": [],
            "relations": [],
            "documents": [],
        }
        if self._driver is None:
            logger.warning("跳过图谱对话检索（Neo4j 不可用）")
            return empty
        if not kws:
            return empty

        cap = min(max(limit, 1), 20)
        vis = neo4j_access_params(scope)
        logger.info(
            "图谱对话检索开始：kws=%s limit=%d unrestricted=%s",
            " / ".join(kws),
            cap,
            vis.get("unrestricted"),
        )
        async with self._driver.session() as session:
            try:
                # 先找实体：短词对 name/aliases 做相等、前缀、后缀；
                # 命中实体必须出现在当前用户可见文档的分块里
                ent_result = await session.run(
                    f"""
                    MATCH (e:KnowledgeEntity)
                    // 书名号归一：e.name / aliases 都去《》再比，能匹配旧节点（带《）与新节点
                    WITH e,
                         toLower(replace(replace(e.name, '《', ''), '》', '')) AS ename
                    WHERE any(kw IN $kws WHERE
                      ename = toLower(kw)
                      OR ename STARTS WITH toLower(kw)
                      OR ename ENDS WITH toLower(kw)
                      OR any(a IN coalesce(e.aliases, []) WHERE
                        toLower(replace(replace(toString(a), '《', ''), '》', '')) = toLower(kw)
                        OR toLower(replace(replace(toString(a), '《', ''), '》', ''))
                           STARTS WITH toLower(kw)
                        OR toLower(replace(replace(toString(a), '《', ''), '》', ''))
                           ENDS WITH toLower(kw)
                      )
                    )
                    AND (
                      $unrestricted OR EXISTS {{
                        MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                              -[:MENTIONS]->(e)
                        WHERE {neo4j_document_access_where('d')}
                      }}
                    )
                    RETURN e.name AS name, e.type AS type,
                           e.description AS description
                    ORDER BY
                      CASE
                        WHEN any(kw IN $kws WHERE ename = toLower(kw)) THEN 0
                        WHEN any(kw IN $kws WHERE ename STARTS WITH toLower(kw)
                          OR ename ENDS WITH toLower(kw)) THEN 1
                        ELSE 2
                      END,
                      size(e.name)
                    LIMIT $limit
                    """,
                    kws=kws,
                    limit=cap,
                    **vis,
                )
                ent_records = await ent_result.data()
                # ⚠️ Neo4j record 的值类型是 Any：name 理论非空（WHERE 就用它匹配），
                # 但仍按「取不到就跳过」处理 —— TypedDict 要求 str，而 str(None)
                # 会写出字面量 "None" 污染下游的展示与实体匹配。
                entities: list[GraphChatEntity] = []
                for row in ent_records:
                    name = row.get("name")
                    if not name:
                        continue
                    entities.append(
                        {
                            "name": str(name),
                            "type": _opt_str(row.get("type")),
                            "description": _opt_str(row.get("description")),
                        }
                    )

                names = [entity["name"] for entity in entities]
                if not names:
                    logger.info("图谱对话检索无实体：kws=%s", " / ".join(kws))
                    return empty

                # 只取本轮命中实体之间的边，不要扩到图上其它节点。
                # 关系出处：抽取该关系的 chunk（写入时记在 r.chunkId）→ 取实体附近片段。
                rel_result = await session.run(
                    """
                    MATCH (a:KnowledgeEntity)-[r:RELATED_TO]->(b:KnowledgeEntity)
                    WHERE a.name IN $names AND b.name IN $names
                    OPTIONAL MATCH (c:DocumentChunk {chunkId: r.chunkId})
                    RETURN a.name AS source,
                           coalesce(nullif(r.relation, 'RELATED_TO'), '关联') AS relation,
                           b.name AS target,
                           c.content AS evidence
                    """,
                    names=names,
                )
                seen: set[str] = set()
                relations: list[GraphChatRelation] = []
                for row in await rel_result.data():
                    # 关系两端必须存在（MATCH 已保证，这里同时做类型收窄与兜底）
                    source = row.get("source")
                    target = row.get("target")
                    if not source or not target:
                        continue
                    relation = str(row.get("relation") or "关联")
                    source, target = str(source), str(target)
                    key = f"{source}\t{relation}\t{target}"
                    if key in seen:
                        continue
                    seen.add(key)
                    relations.append(
                        {
                            "source": source,
                            "relation": relation,
                            "target": target,
                            "evidence": _evidence_snippet(
                                row.get("evidence"), source, target
                            ),
                        }
                    )

                # 这些实体来自哪些当前用户可见的文档（给前端/模型当来源）
                doc_result = await session.run(
                    f"""
                    MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                          -[:MENTIONS]->(e:KnowledgeEntity)
                    WHERE e.name IN $names
                      AND ($unrestricted OR {neo4j_document_access_where('d')})
                    RETURN DISTINCT d.id AS documentId, d.title AS title
                    LIMIT 8
                    """,
                    names=names,
                    **vis,
                )
                documents: list[GraphChatDocument] = []
                for row in await doc_result.data():
                    doc_id = row.get("documentId")
                    if not doc_id:
                        continue
                    documents.append(
                        {
                            "documentId": str(doc_id),
                            "title": str(row.get("title") or ""),
                        }
                    )
                logger.info(
                    "图谱对话检索完成：kws=%s entities=%d rels=%d docs=%d",
                    " / ".join(kws),
                    len(entities),
                    len(relations),
                    len(documents),
                )
                return {
                    "query": " / ".join(kws),
                    "entities": entities,
                    "relations": relations,
                    "documents": documents,
                }
            except Exception as err:
                logger.warning("图谱对话检索失败：%s", err)
                return empty

    async def get_overview(
        self,
        keyword: str | None = None,
        entity_type: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        doc_limit: int | None = None,
        scope: DocumentAccessScope | None = None,
    ) -> dict[str, Any]:
        """全景图：文档 + 被提及实体 + 标签 + 统计。

        不含 chunk（块太碎，不适合画布）。文档→实体为「提及」，
        实体→实体为 RELATED_TO 上的 relation，文档→标签为「标注」。
        Neo4j 不可用或查询失败时返回空结构（不抛错）。

        可见性：**全部口径都按「当前用户可见的文档」计算** —— 文档数 / 实体数 /
        RELATED_TO 边数 / 提及次数、type 分布、Top 实体、最近文档、主查询、关键词
        补充查询，逐条 Cypher 都带 neo4j_document_access_where。scope=None 表示
        不过滤，API 端点必须传 access_from_user(user)。

        刻意差异：参数名 from_time/to_time —— from 是 Python 保留字；
        HTTP 查询参数仍叫 from/to。
        """
        empty: dict[str, Any] = {
            "nodes": [],
            "edges": [],
            "stats": {
                "nodeCount": 0,
                "edgeCount": 0,
                "documentCount": 0,
                "entityCount": 0,
                "tagCount": 0,
                "mentionCount": 0,
                "relatedCount": 0,
                "entityTypes": [],
            },
            "topEntities": [],
            "recentNodes": [],
            "entityTypes": [],
        }
        if self._driver is None:
            logger.warning("跳过图谱全景（Neo4j 不可用）")
            return empty

        kw = (keyword or "").strip()
        etype = (entity_type or "").strip() or None
        date_from = (from_time or "").strip() or None
        date_to = (to_time or "").strip() or None
        # 默认 24，夹取 1..80（), 80)）
        limit = min(max(doc_limit if doc_limit is not None else 24, 1), 80)
        # 可见性参数（$unrestricted / $accessUserId / $accessTeamIds）
        vis = neo4j_access_params(scope)

        try:
            async with self._driver.session() as session:
                # 统计口径：文档数、实体数、RELATED_TO 边数、MENTIONS 提及次数
                # **均按当前用户可见的文档计算**（分段 WITH 避免笛卡尔积）
                stats_records = await (
                    await session.run(
                        f"""
                        OPTIONAL MATCH (d:KnowledgeDocument)
                        WHERE {neo4j_document_access_where('d')}
                        WITH count(d) AS documentCount
                        OPTIONAL MATCH (d2:KnowledgeDocument)-[:HAS_CHUNK]
                            ->(:DocumentChunk)-[:MENTIONS]->(e:KnowledgeEntity)
                        WHERE {neo4j_document_access_where('d2')}
                        WITH documentCount, count(DISTINCT e) AS entityCount
                        OPTIONAL MATCH (a:KnowledgeEntity)-[rel:RELATED_TO]->(b:KnowledgeEntity)
                        WHERE $unrestricted OR (
                          EXISTS {{
                            MATCH (d3:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                                  -[:MENTIONS]->(a)
                            WHERE {neo4j_document_access_where('d3')}
                          }}
                          AND EXISTS {{
                            MATCH (d4:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                                  -[:MENTIONS]->(b)
                            WHERE {neo4j_document_access_where('d4')}
                          }}
                        )
                        WITH documentCount, entityCount, count(rel) AS relatedCount
                        OPTIONAL MATCH (d5:KnowledgeDocument)-[:HAS_CHUNK]
                            ->(:DocumentChunk)-[:MENTIONS]->(e0:KnowledgeEntity)
                        WHERE {neo4j_document_access_where('d5')}
                        RETURN documentCount, entityCount, relatedCount,
                               count(e0) AS mentionCount
                        """,
                        **vis,
                    )
                ).data()
                stats_row = stats_records[0] if stats_records else {}
                document_count = int(_to_number(stats_row.get("documentCount"), 0))
                entity_count = int(_to_number(stats_row.get("entityCount"), 0))
                related_count = int(_to_number(stats_row.get("relatedCount"), 0))
                mention_count = int(_to_number(stats_row.get("mentionCount"), 0))

                # 按实体 type 分组计数，供前端筛选（只统计可见文档提及的实体）
                type_rows = await (
                    await session.run(
                        f"""
                        MATCH (d:KnowledgeDocument)-[:HAS_CHUNK]->(:DocumentChunk)
                              -[:MENTIONS]->(e:KnowledgeEntity)
                        WHERE {neo4j_document_access_where('d')}
                          AND e.type IS NOT NULL AND e.type <> ''
                        RETURN e.type AS type, count(DISTINCT e) AS count
                        ORDER BY count DESC
                        """,
                        **vis,
                    )
                ).data()
                entity_types = [
                    {
                        "type": str(row["type"]),
                        "count": int(_to_number(row.get("count"), 0)),
                    }
                    for row in type_rows
                ]

                # 被文档块 MENTIONS 最多的 5 个实体（degree = 提及次数，限可见文档）
                top_rows = await (
                    await session.run(
                        f"""
                        MATCH (e:KnowledgeEntity)<-[:MENTIONS]-(:DocumentChunk)
                              <-[:HAS_CHUNK]-(d:KnowledgeDocument)
                        WHERE {neo4j_document_access_where('d')}
                        RETURN e.name AS name, e.type AS type, count(*) AS degree
                        ORDER BY degree DESC
                        LIMIT 5
                        """,
                        **vis,
                    )
                ).data()
                top_entities = [
                    {
                        "name": str(row["name"]),
                        "type": row.get("type"),
                        "degree": int(_to_number(row.get("degree"), 0)),
                    }
                    for row in top_rows
                ]

                # 最近更新的 8 篇文档（不受 keyword / 时间 / 类型过滤，但受可见性约束）
                recent_rows = await (
                    await session.run(
                        f"""
                        MATCH (d:KnowledgeDocument)
                        WHERE {neo4j_document_access_where('d')}
                        RETURN d.id AS id, d.title AS name, d.updatedAt AS updatedAt
                        ORDER BY d.updatedAt DESC
                        LIMIT 8
                        """,
                        **vis,
                    )
                ).data()
                recent_nodes = [
                    {
                        "id": f"doc:{row['id']}",
                        "name": str(row.get("name") or ""),
                        "kind": "document",
                        "updatedAt": row.get("updatedAt"),
                    }
                    for row in recent_rows
                ]

                # 主查询：按标题/摘要/标签 + 时间筛文档，LIMIT 后挂上 MENTIONS 实体
                #（可按 entityType 再筛）；可见性条件与上面各统计口径同一条
                main_params: dict[str, Any] = {
                    "kw": kw,
                    "entityType": etype,
                    "from": date_from,
                    "to": date_to,
                    "docLimit": limit,
                    **vis,
                }
                main_records = await (
                    await session.run(
                        f"""
                        MATCH (d:KnowledgeDocument)
                        WHERE {neo4j_document_access_where('d')}
                          AND ($kw = '' OR toLower(coalesce(d.title, '')) CONTAINS toLower($kw)
                              OR toLower(coalesce(d.summary, '')) CONTAINS toLower($kw)
                              OR toLower(coalesce(d.tags, '')) CONTAINS toLower($kw))
                          AND ($from IS NULL OR d.updatedAt >= $from)
                          AND ($to IS NULL OR d.updatedAt <= $to)
                        WITH d ORDER BY d.updatedAt DESC LIMIT $docLimit
                        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(:DocumentChunk)
                            -[:MENTIONS]->(e:KnowledgeEntity)
                        WHERE $entityType IS NULL OR e.type = $entityType
                        RETURN d.id AS docId, d.title AS docTitle, d.summary AS summary,
                               d.tags AS tags, d.updatedAt AS updatedAt,
                               collect(DISTINCT CASE WHEN e IS NULL THEN NULL ELSE {{
                                 name: e.name, type: e.type, description: e.description
                               }} END) AS entities
                        """,
                        main_params,
                    )
                ).data()
                doc_records: list[dict[str, Any]] = list(main_records)

                # 关键词命中实体但标题未命中时，把提及该实体的文档补进来
                if kw:
                    extra_params: dict[str, Any] = {
                        "kw": kw,
                        "entityType": etype,
                        "from": date_from,
                        "to": date_to,
                        "docLimit": limit,
                        **vis,
                    }
                    extra_records = await (
                        await session.run(
                            f"""
                            MATCH (e:KnowledgeEntity)
                              <-[:MENTIONS]-(:DocumentChunk)
                              <-[:HAS_CHUNK]-(d:KnowledgeDocument)
                            WHERE {neo4j_document_access_where('d')}
                              AND (toLower(coalesce(e.name, '')) CONTAINS toLower($kw)
                               OR toLower(coalesce(e.description, '')) CONTAINS toLower($kw))
                            WITH DISTINCT d
                            WHERE ($from IS NULL OR d.updatedAt >= $from)
                              AND ($to IS NULL OR d.updatedAt <= $to)
                            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(:DocumentChunk)
                                -[:MENTIONS]->(e2:KnowledgeEntity)
                            WHERE $entityType IS NULL OR e2.type = $entityType
                            RETURN d.id AS docId, d.title AS docTitle,
                                   d.summary AS summary, d.tags AS tags,
                                   d.updatedAt AS updatedAt,
                                   collect(DISTINCT CASE WHEN e2 IS NULL THEN NULL ELSE {{
                                     name: e2.name, type: e2.type, description: e2.description
                                   }} END) AS entities
                            LIMIT $docLimit
                            """,
                            extra_params,
                        )
                    ).data()
                    seen = {str(r["docId"]) for r in doc_records}
                    for record in extra_records:
                        if str(record["docId"]) not in seen:
                            doc_records.append(record)

                node_map: dict[str, dict[str, Any]] = {}
                edge_map: dict[str, dict[str, Any]] = {}
                entity_names: set[str] = set()

                def add_edge(
                    source: str, target: str, relation: str, kind: str
                ) -> None:
                    key = f"{kind}|{source}|{target}|{relation}"
                    if key not in edge_map:
                        edge_map[key] = {
                            "source": source,
                            "target": target,
                            "relation": relation,
                            "kind": kind,
                        }

                def split_tags(raw: Any) -> list[str]:
                    """逗号分隔标签串拆分"""
                    return [
                        t.strip() for t in str(raw or "").replace("，", ",").split(",")
                        if t.strip()
                    ]

                for record in doc_records:
                    doc_id = str(record["docId"])
                    doc_node_id = f"doc:{doc_id}"
                    node_map[doc_node_id] = {
                        "id": doc_node_id,
                        "name": str(record.get("docTitle") or ""),
                        "kind": "document",
                        "type": "DOCUMENT",
                        "documentId": doc_id,
                        "updatedAt": record.get("updatedAt"),
                        "description": record.get("summary"),
                    }
                    for tag in split_tags(record.get("tags")):
                        tag_id = f"tag:{tag}"
                        node_map[tag_id] = {
                            "id": tag_id,
                            "name": tag,
                            "kind": "tag",
                            "type": "TAG",
                        }
                        add_edge(doc_node_id, tag_id, "标注", "tagged")
                    for entity in record.get("entities") or []:
                        if not isinstance(entity, dict) or not entity.get("name"):
                            continue
                        name = str(entity["name"])
                        entity_id = f"entity:{name}"
                        entity_names.add(name)
                        node_map[entity_id] = {
                            "id": entity_id,
                            "name": name,
                            "kind": "entity",
                            "type": entity.get("type") or "CONCEPT",
                            "description": entity.get("description"),
                        }
                        add_edge(doc_node_id, entity_id, "提及", "mentions")

                if entity_names:
                    # 只取当前画布上实体之间的 RELATED_TO，避免拉全库关系
                    related_rows = await (
                        await session.run(
                            """
                            MATCH (a:KnowledgeEntity)-[r:RELATED_TO]->(b:KnowledgeEntity)
                            WHERE a.name IN $names AND b.name IN $names
                            RETURN a.name AS source, b.name AS target,
                                   r.relation AS relation, r.weight AS weight
                            LIMIT 400
                            """,
                            names=sorted(entity_names),
                        )
                    ).data()
                    for row in related_rows:
                        source = f"entity:{row['source']}"
                        target = f"entity:{row['target']}"
                        relation = row.get("relation") or "关联"
                        add_edge(source, target, relation, "related")

                nodes = list(node_map.values())
                edges = list(edge_map.values())
                tag_count = sum(1 for n in nodes if n["kind"] == "tag")

                return {
                    "nodes": nodes,
                    "edges": edges,
                    "stats": {
                        "nodeCount": len(nodes),
                        "edgeCount": len(edges),
                        "documentCount": document_count,
                        "entityCount": entity_count,
                        "tagCount": tag_count,
                        "mentionCount": mention_count,
                        "relatedCount": related_count,
                        "entityTypes": entity_types,
                    },
                    "topEntities": top_entities,
                    "recentNodes": recent_nodes,
                    "entityTypes": [t["type"] for t in entity_types],
                }
        except Exception as err:
            logger.warning("图谱全景查询失败：%s", err)
            return empty


def _to_number(value: Any, default: float) -> float:
    """Neo4j 数值字段统一转 float。

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
