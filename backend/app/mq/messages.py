"""
管线消息定义（对应 TS messages/pipeline.messages）
"""
from typing import Literal, TypedDict

from app.pipeline.types import SearchDocumentData


class ReindexMessage(TypedDict):
    """RAG 重建 / 删除消息（rag.reindex.exchange）

    复用一个结构：type 区分重建（BY_DOC_IDS）与删除向量块（DELETE_BY_DOC_IDS），
    与 TS DocumentPipelinePublisher 一致。
    """

    # 任务 ID（uuid4）
    taskId: str
    # 消息类型：按文档 ID 重建，或按文档 ID 删除向量块
    type: Literal["BY_DOC_IDS", "DELETE_BY_DOC_IDS"]
    # 待处理（重建 / 删除）的文档 ID 列表
    documentIds: list[str]


class SearchIndexMessage(TypedDict):
    """Search 全文索引消息（search.index.exchange / search.index.document）

    消息内直接带文档快照（document），消费者无需再查库即可写索引；
    type 区分写入（INDEX）与删除（DELETE），与 TS 一致。
    """

    # 任务 ID（uuid4）
    taskId: str
    # 动作：整篇快照 upsert 到 kh_document
    type: Literal["INDEX"]
    # 文档 ID（顶层冗余，便于路由 / 日志）
    documentId: str
    # 文档快照（字段约定见 DocumentPipelinePublisher.build_search_index_data）
    document: SearchDocumentData


class SearchDeleteMessage(TypedDict):
    """Search 索引清理消息（search.index.exchange / search.index.delete）"""

    # 任务 ID（uuid4）
    taskId: str
    # 动作：从 kh_document 删除
    type: Literal["DELETE"]
    documentId: str


class KgBuildMessage(TypedDict):
    """KG 知识图谱构建 / 删除消息（kg.graph.exchange）

    type 区分建图（BUILD_BY_DOC_IDS / BUILD_ALL）与删除（DELETE_BY_DOC_IDS），
    与 TS 一致。
    """

    # 任务 ID（uuid4）
    taskId: str
    # 消息类型：按文档 ID 建图、全量建图，或按文档 ID 删除图谱
    type: Literal["BUILD_BY_DOC_IDS", "BUILD_ALL", "DELETE_BY_DOC_IDS"]
    # 待处理（建图 / 删除）的文档 ID 列表
    documentIds: list[str]
