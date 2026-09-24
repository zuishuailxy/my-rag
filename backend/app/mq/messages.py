"""
管线消息定义
"""
from typing import Literal, TypedDict


class ReindexMessage(TypedDict):
    """RAG 重建 / 删除消息（rag.reindex.exchange）

    复用一个结构：type 区分重建（BY_DOC_IDS）与删除向量块（DELETE_BY_DOC_IDS）。
    """

    # 任务 ID（uuid4）
    taskId: str
    # 消息类型：按文档 ID 重建，或按文档 ID 删除向量块
    type: Literal["BY_DOC_IDS", "DELETE_BY_DOC_IDS"]
    # 待处理（重建 / 删除）的文档 ID 列表
    documentIds: list[str]


class SearchIndexMessage(TypedDict):
    """Search 全文索引消息（search.index.exchange）

    只传 documentId：消费者按 ID 从 Postgres + Mongo 拉取文档全文后再写
    ES kh_document。早期版本曾在消息内携带 1000 字正文快照，因队列字符
    限制改为按 ID 拉库。
    type 区分写入（INDEX）与删除（DELETE）。
    """

    # 任务 ID（uuid4）
    taskId: str
    # 动作：按 documentId 拉库写索引（INDEX）/ 从 kh_document 删除（DELETE）
    type: Literal["INDEX", "DELETE"]
    # 文档 ID
    documentId: str


class KgBuildMessage(TypedDict):
    """KG 知识图谱构建 / 删除消息（kg.graph.exchange）

    type 区分建图（BUILD_BY_DOC_IDS / BUILD_ALL）与删除（DELETE_BY_DOC_IDS）。
    """

    # 任务 ID（uuid4）
    taskId: str
    # 消息类型：按文档 ID 建图、全量建图，或按文档 ID 删除图谱
    type: Literal["BUILD_BY_DOC_IDS", "BUILD_ALL", "DELETE_BY_DOC_IDS"]
    # 待处理（建图 / 删除）的文档 ID 列表
    documentIds: list[str]
