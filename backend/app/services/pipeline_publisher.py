"""
文档发布管线投递（对应 TS DocumentPipelinePublisher）

真实实现基于 RabbitMQ：发布后向 rag.reindex.exchange 投递 RAG 重建消息
（见 app/mq/publisher.py / app/mq/rabbitmq.py）。
"""
from app.mq.publisher import DocumentPipelinePublisher

# 全局单例（RabbitMQ 投递；MQ 不可用时由 publish 内部降级为跳过并记日志）
pipeline_publisher = DocumentPipelinePublisher()
