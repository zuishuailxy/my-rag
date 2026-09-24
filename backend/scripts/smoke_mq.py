"""
RabbitMQ 封装冒烟测试（真实连接本机 rabbitmq:5672）

覆盖：
- start() 连接 + 声明 RAG 拓扑（交换机/队列/绑定）；
- register_handler + 消费回环：发布消息 → 处理器收到并 ack（用独立测试交换机/队列，避免与真实消费者竞争）；
- publish 返回 true；禁用实例降级返回 false；
- 清理：删除测试拓扑 + 关闭连接（finally 兜底）。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_mq.py
"""
import asyncio
import json

import aio_pika
from aio_pika import ExchangeType

from app.mq.constants import RAG_REINDEX_EXCHANGE, RAG_REINDEX_QUEUE
from app.mq.rabbitmq import RabbitMqService, rabbitmq_service

TEST_EXCHANGE = "kh.test.exchange"
TEST_QUEUE = "kh.test.queue"
TEST_RK = "test.roundtrip"
URL = "amqp://guest:guest@localhost:5672"


async def main() -> None:
    test_conn: aio_pika.abc.AbstractRobustConnection | None = None
    try:
        # 1) 用独立连接声明测试拓扑（不污染 RAG 拓扑，避免与真实消费者竞争）
        test_conn = await aio_pika.connect_robust(URL)
        test_ch = await test_conn.channel()
        ex = await test_ch.declare_exchange(TEST_EXCHANGE, ExchangeType.TOPIC, durable=True)
        q = await test_ch.declare_queue(TEST_QUEUE, durable=True)
        await q.bind(ex, TEST_RK)

        # 2) 注册测试处理器，启动服务（会一并声明 RAG 拓扑）
        received = asyncio.Event()
        got: dict = {}

        async def handler(msg) -> None:
            # handler 不负责 ack
            got["body"] = json.loads(msg.body.decode("utf-8"))
            received.set()

        rabbitmq_service.register_handler(TEST_QUEUE, handler)
        await rabbitmq_service.start()
        print("1) RabbitMQ 连接 + 拓扑声明 OK")

        # 3) 发布 → 回环
        ok = await rabbitmq_service.publish(TEST_EXCHANGE, TEST_RK, {"documentIds": ["123"]})
        print("2) publish:", ok)
        assert ok
        await asyncio.wait_for(received.wait(), timeout=5)
        assert got["body"] == {"documentIds": ["123"]}, got
        print("3) 消费回环 OK:", got["body"])

        # 4) 禁用降级
        off = RabbitMqService(enabled=False)
        assert await off.publish(RAG_REINDEX_EXCHANGE, "x", {}) is False
        print("4) 禁用降级 OK")
    finally:
        await rabbitmq_service.close()
        # 清理测试拓扑（独立连接）
        if test_conn is not None:
            try:
                ch = await test_conn.channel()
                await ch.queue_delete(TEST_QUEUE)
                await ch.exchange_delete(TEST_EXCHANGE)
            except Exception:
                pass
            await test_conn.close()
    print("\nRabbitMQ 封装冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
