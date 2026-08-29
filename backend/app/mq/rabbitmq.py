"""
RabbitMQ 服务（对应 TS rabbitmq.service，amqplib/amqp-connection-manager → aio-pika）

- aio-pika robust 连接：断线自动重连，重连后自动重建拓扑（交换机/队列/绑定）与消费者
- publish：JSON payload，persistent 消息，失败/不可用时返回 False 并记日志
- register_handler：注册队列消费者（ack 成功 / nack 不重新入队），连接就绪后生效
"""
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable

import aio_pika
from aio_pika import DeliveryMode, ExchangeType
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection

from app.core.config import settings
from app.mq.constants import (
    SEARCH_INDEX_EXCHANGE,
    SEARCH_INDEX_QUEUE,
    RAG_REINDEX_EXCHANGE,
    RAG_REINDEX_QUEUE,
    RAG_RK_BY_IDS,
    RAG_RK_DELETE,
    SEARCH_RK_DELETE,
    SEARCH_RK_INDEX,
)

logger = logging.getLogger("RabbitMqService")

# 队列消费者处理器：收到消息后执行，由本服务负责 ack/nack
MessageHandler = Callable[[AbstractIncomingMessage], Awaitable[None]]


def redact_amqp_url(url: str) -> str:
    """日志里隐藏 AMQP 密码：amqp://user:pass@ → amqp://user:***@"""
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url)


class RabbitMqService:
    """RabbitMQ 连接/拓扑/发布/消费封装"""

    def __init__(
        self,
        url: str = settings.RABBITMQ_URL,
        enabled: bool = settings.RABBITMQ_ENABLED,
        timeout_ms: int = settings.RABBITMQ_CONNECT_TIMEOUT_MS,
    ) -> None:
        self._url = url
        self._enabled = enabled
        self._timeout = timeout_ms / 1000
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._handlers: dict[str, MessageHandler] = {}

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_connected(self) -> bool:
        return self._channel is not None

    async def start(self) -> None:
        """连接 RabbitMQ，声明拓扑并绑定消费者；失败时关闭并抛出"""
        if not self._enabled:
            logger.warning("RabbitMQ 已禁用（RABBITMQ_ENABLED=false）")
            return
        safe_url = redact_amqp_url(self._url)
        logger.info("正在连接 RabbitMQ：%s（超时 %.0fms）", safe_url, self._timeout * 1000)
        try:
            async with asyncio.timeout(self._timeout):
                conn = await aio_pika.connect_robust(self._url)
                self._connection = conn
                channel = await conn.channel()
                self._channel = channel
                await self._assert_topology(channel)
                await self._bind_consumers(channel)
            logger.info("RabbitMQ channel 就绪：%s", safe_url)
        except Exception as err:
            logger.error("RabbitMQ 初始化失败：%s", err)
            await self.close()
            raise

    async def close(self) -> None:
        """关闭连接（应用退出时调用）"""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._channel = None

    def register_handler(self, queue: str, handler: MessageHandler) -> None:
        """注册队列消费者（连接就绪后生效）"""
        self._handlers[queue] = handler
        if self._channel is not None:
            # 连接已就绪：立即生效（声明队列 + 消费）
            asyncio.create_task(self._consume(self._channel, queue, handler))

    async def publish(
        self, exchange: str, routing_key: str, payload: object
    ) -> bool:
        """发布 JSON 消息（持久化）；不可用或失败返回 False"""
        if not self._enabled or self._channel is None:
            logger.warning(
                "跳过发消息（MQ 不可用）：exchange=%s, rk=%s", exchange, routing_key
            )
            return False
        try:
            ex = await self._channel.declare_exchange(
                exchange, ExchangeType.TOPIC, durable=True
            )
            await ex.publish(
                aio_pika.Message(
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                    delivery_mode=DeliveryMode.PERSISTENT,
                ),
                routing_key,
            )
            return True
        except Exception as err:
            logger.warning(
                "发消息失败：exchange=%s, rk=%s, error=%s", exchange, routing_key, err
            )
            return False

    async def _assert_topology(self, ch: AbstractChannel) -> None:
        # RAG：重建索引 / 删除向量块（一个队列绑两个路由键）
        ex = await ch.declare_exchange(
            RAG_REINDEX_EXCHANGE, ExchangeType.TOPIC, durable=True
        )
        q = await ch.declare_queue(RAG_REINDEX_QUEUE, durable=True)
        await q.bind(ex, RAG_RK_BY_IDS)
        await q.bind(ex, RAG_RK_DELETE)

        # Search：整篇快照索引 / 清理（一个队列绑两个路由键）
        ex_doc = await ch.declare_exchange(
            SEARCH_INDEX_EXCHANGE, ExchangeType.TOPIC, durable=True
        )
        q_search = await ch.declare_queue(SEARCH_INDEX_QUEUE, durable=True)
        await q_search.bind(ex_doc, SEARCH_RK_INDEX)
        await q_search.bind(ex_doc, SEARCH_RK_DELETE)
        logger.info("RabbitMQ 拓扑已声明（RAG + Search）")

    async def _bind_consumers(self, ch: AbstractChannel) -> None:
        for queue_name, handler in self._handlers.items():
            await self._consume(ch, queue_name, handler)

    async def _consume(
        self, ch: AbstractChannel, queue_name: str, handler: MessageHandler
    ) -> None:
        queue = await ch.declare_queue(queue_name, durable=True)

        async def on_message(msg: AbstractIncomingMessage) -> None:
            if msg is None:
                return
            try:
                await handler(msg)
                await msg.ack()
            except Exception as err:
                logger.error("消费失败 queue=%s: %s", queue_name, err)
                try:
                    await msg.nack(requeue=False)
                except Exception:
                    # 消息可能已被 handler 处理（如已 ack），忽略二次处理错误
                    pass

        await queue.consume(on_message)
        logger.info("已注册消费者：%s", queue_name)


# 全局单例
rabbitmq_service = RabbitMqService()