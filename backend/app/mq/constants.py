"""
RabbitMQ 拓扑常量（对应 TS mq.constants）

交换机：rag.reindex；队列名带 `kh.` 前缀，避免和本机同时跑的其他项目冲突。
"""

# RAG 重建索引交换机（topic）
RAG_REINDEX_EXCHANGE = "rag.reindex.exchange"

# 本服务消费的队列
RAG_REINDEX_QUEUE = "kh.rag.reindex.queue"

# 路由键：按文档 ID 重建
RAG_RK_BY_IDS = "rag.reindex.by_ids"