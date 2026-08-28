"""
管线消息定义（对应 TS messages/pipeline.messages）
"""
from typing import Literal, TypedDict


class ReindexMessage(TypedDict):
    """RAG 重建索引消息"""

    # 任务 ID（uuid4）
    taskId: str
    # 消息类型：按文档 ID 重建
    type: Literal["BY_DOC_IDS"]
    # 待重建的文档 ID 列表
    documentIds: list[str]
