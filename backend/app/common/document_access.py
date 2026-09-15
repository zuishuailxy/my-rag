"""
文档 / 知识块可见性判定（对应 TS src/document/document-access.ts）

可见性模型（三条并列，命中任一即可读）：
  1. 公开（is_public = true）
  2. 自己写的（author_id = 当前用户）
  3. 所在团队（team_id ∈ 当前用户团队，含其担任负责人的团队）

ADMIN / REVIEWER 视为不受限（unrestricted），不加任何过滤。

本模块只做「纯函数」判定与查询条件构造，不查库、不依赖 ORM 会话 / Neo4j driver：
  - access_from_user：AuthUser → DocumentAccessScope（检索链路入口）
  - can_read_document：单篇文档判权（详情接口等复用）
  - es_visibility_filter：ES filter 子句（只筛不打分，kh_chunk / kh_document 共用）
  - wrap_es_query：把相关性查询放 must、可见性放 filter
  - neo4j_access_params / neo4j_document_access_where：KG（Neo4j）查询侧可见性
    参数与 Cypher 片段，供 services/graph_build.py 的四个查询方法拼接

两种 ES 索引的字段名不同，由调用方选常量：
  - kh_chunk（块级，snake_case）：CHUNK_VISIBILITY_FIELDS —— services/search.py 关键词通道
  - kh_document（文档级，camelCase）：DOC_VISIBILITY_FIELDS —— services/search_index.py 全文检索

Neo4j 侧字段名与 TS 一致（文档节点属性是 camelCase：authorId / isPublic / teamId），
由 neo4j_document_access_where 内的字符串字面量固定，不走 ES 那套常量表。

刻意差异（相对 TS）：
  - 本项目向量在 PG doc_chunk（TS 在 ES dense_vector），故向量通道的可见性用
    JOIN kh_document 在 SQL 侧过滤，见 services/search.py 的 _vector_recall；
    本模块负责 ES 侧的 filter（kh_chunk 关键词通道 + kh_document 全文检索）。
  - KG 侧的可见性（TS neo4jAccessParams / neo4jDocumentAccessWhere）已补齐：
    写入侧文档节点落 authorId / teamId / isPublic（services/graph_build.py），
    查询侧四个方法逐条 Cypher 都带 neo4j_document_access_where，本模块提供两个纯函数。
"""

from dataclasses import dataclass, field
from typing import Any

from app.common.roles import RoleCode
from app.models.document_status import DocumentStatus
from app.schemas.auth import AuthUser

__all__ = [
    "CHUNK_VISIBILITY_FIELDS",
    "DOC_VISIBILITY_FIELDS",
    "DocumentAccessScope",
    "access_from_user",
    "can_read_document",
    "empty_scope",
    "es_visibility_filter",
    "neo4j_access_params",
    "neo4j_document_access_where",
    "wrap_es_query",
]


@dataclass
class DocumentAccessScope:
    """当前用户的可读范围（检索 / 判权共用）

    unrestricted=True 表示管理员或审核员：调用方不加任何过滤条件。
    """

    # 不受限（ADMIN / REVIEWER）时跳过全部可见性过滤
    unrestricted: bool
    # 当前用户 ID（字符串，雪花 ID 转字符串与全项目 bigint 约定一致）
    user_id: str
    # 所在团队 ID（含担任负责人的团队）；无团队为空列表
    team_ids: list[str] = field(default_factory=list)


def access_from_user(user: AuthUser) -> DocumentAccessScope:
    """AuthUser → DocumentAccessScope（对应 TS accessFromUser）

    角色码取 ADMIN / REVIEWER 之一即不受限。team_ids 由鉴权层在构建 AuthUser 时
    查好注入（见 services/user.py build_auth_user），此处只做透传，不查库。
    """
    roles = user.roles or []
    return DocumentAccessScope(
        unrestricted=RoleCode.ADMIN in roles or RoleCode.REVIEWER in roles,
        user_id=user.user_id,
        team_ids=list(user.team_ids or []),
    )


def empty_scope() -> DocumentAccessScope:
    """无登录用户时的兜底 scope（对应 TS `{ unrestricted: false, userId: '', teamIds: [] }`）

    语义是「只能看到公开内容」：user_id 空字符串匹配不到任何作者，
    team_ids 为空表示没有团队可见范围。
    """
    return DocumentAccessScope(unrestricted=False, user_id="", team_ids=[])


def can_read_document(
    doc: object,
    scope: DocumentAccessScope,
) -> bool:
    """单篇文档是否可读（对应 TS canReadDocument）

    入参 doc 需具备 author_id / team_id / is_public / status 四个属性
    （kh_document 实体或等价的 dict-like 快照）。判定顺序与 TS 一致：
    不受限 → 作者本人 → 未发布一律不可读 → 公开 → 团队。
    """
    if scope.unrestricted:
        return True
    author_id = getattr(doc, "author_id", None)
    if author_id is not None and str(author_id) == scope.user_id:
        return True
    status = getattr(doc, "status", None)
    if status is not None and int(status) != int(DocumentStatus.PUBLISHED):
        return False
    if bool(getattr(doc, "is_public", False)):
        return True
    team_id = getattr(doc, "team_id", None)
    return team_id is not None and str(team_id) in scope.team_ids


# kh_chunk 的可见性字段名（snake_case；TS ES_CHUNK_VISIBILITY_FIELDS）
CHUNK_VISIBILITY_FIELDS: dict[str, str] = {
    "is_public": "is_public",
    "author_id": "author_id",
    "team_id": "team_id",
}

# kh_document 的可见性字段名（camelCase；TS ES_DOC_VISIBILITY_FIELDS）
#
# 与 kh_chunk 的区别：文档级索引整篇一条记录，字段名沿用 TS
# DocumentPipelinePublisher.buildSearchIndexData 的 camelCase 风格
# （见 services/search_index.py 的 DOCUMENT_MAPPINGS），故这里 value 是 camelCase。
# dict 的 key 保持 snake_case 语义名，便于 es_visibility_filter 两种索引共用同一实现。
DOC_VISIBILITY_FIELDS: dict[str, str] = {
    "is_public": "isPublic",
    "author_id": "authorId",
    "team_id": "teamId",
}


def es_visibility_filter(
    scope: DocumentAccessScope,
    fields: dict[str, str],
) -> dict | None:
    """Elasticsearch 可见性 filter（只筛不打分，对应 TS esVisibilityFilter）

    返回 bool.should + minimum_should_match=1 的子句；不受限时返回 None，
    调用方据此不加过滤。不判断 status —— 假定未发布文档不会进索引。
    """
    if scope.unrestricted:
        return None
    should: list[dict] = [
        {"term": {fields["is_public"]: True}},
        {"term": {fields["author_id"]: scope.user_id}},
    ]
    if scope.team_ids:
        should.append({"terms": {fields["team_id"]: scope.team_ids}})
    # filter 上下文中 should = 或：至少命中一条才可见
    return {"bool": {"should": should, "minimum_should_match": 1}}


def wrap_es_query(
    query: dict,
    visibility_filter: dict | None,
) -> dict:
    """相关性查询放 must、可见性放 filter（对应 TS wrapEsQuery）

    可见性条件不参与打分，避免权限过滤影响 BM25 排序；filter 为 None 时原样返回。
    """
    if not visibility_filter:
        return query
    return {"bool": {"must": [query], "filter": [visibility_filter]}}


# ------------------------------------------------------------------ Neo4j（KG）


def neo4j_access_params(scope: DocumentAccessScope | None) -> dict[str, Any]:
    """Neo4j 查询参数（对应 TS neo4jAccessParams）

    返回 Cypher 里要用的三个参数（名字与 TS 完全一致，Cypher 片段里直接引用）：
      - $unrestricted：True 时可见性条件短路（管理员 / 审核员）
      - $accessUserId：当前用户 ID（空串匹配不到任何 authorId）
      - $accessTeamIds：所在团队 ID 列表（空列表时 IN 恒不命中）

    ⚠️ scope 为 None 时 unrestricted=True —— 即「**不传 scope = 不过滤**」，
    这是刻意对齐 TS 的 `unrestricted: !scope || scope.unrestricted`，
    与 es_visibility_filter / search_documents(scope=None) 的 None 语义保持一致。
    因此生产链路（api/v1/graph.py 四个端点）**必须**传 access_from_user(user)，
    否则等于放开全部可见性；脚本 / 内部调用若确需全库视图才可省略。
    """
    if scope is None:
        return {"unrestricted": True, "accessUserId": "", "accessTeamIds": []}
    return {
        "unrestricted": scope.unrestricted,
        "accessUserId": scope.user_id,
        "accessTeamIds": list(scope.team_ids),
    }


def neo4j_document_access_where(alias: str = "d") -> str:
    """Cypher 片段：别名 alias 的文档节点是否对当前用户可见（对应 TS neo4jDocumentAccessWhere）

    四个条件与 ES 侧 es_visibility_filter 同口径（作者 / 公开 / 团队 + 不受限短路）：
        ($unrestricted OR {alias}.authorId = $accessUserId
         OR {alias}.isPublic = true OR {alias}.teamId IN $accessTeamIds)

    调用方必须把 neo4j_access_params(scope) 一并作为查询参数传入，否则 $unrestricted
    等占位符无值会直接报错（Neo4j 缺参会抛，不会静默放行）。
    文档节点属性名沿用 TS 的 camelCase（authorId / isPublic / teamId）。
    """
    return (
        f"($unrestricted OR {alias}.authorId = $accessUserId "
        f"OR {alias}.isPublic = true OR {alias}.teamId IN $accessTeamIds)"
    )
