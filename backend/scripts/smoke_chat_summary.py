"""会话滚动摘要冒烟测试（真实 Redis + Postgres + LLM）

验证点（对应 services/chat_summary.py + ai_stream 接入）：
1. 前提断言：`enabled` 必须与「是否配置 LLM Key」一致（否则折叠断言会假绿）
2. 未折叠时 try_load 返回 None（没有摘要 ≠ 去重算）
3. 折叠水位：24 条消息、窗口 20 → 只折叠滑出的前 4 条，covered = 第 4 条 id
4. 摘要内容：非空、≤ CHAT_SUMMARY_MAX_CHARS（+省略号）、抓住了窗口外的早期事实
   —— 这条是核心：早期消息在热窗口之外，**只能**通过摘要进入上下文
5. 双写：Redis key 与 kh_ai_session_summary 行都在，且一致
6. 兜底：删掉 Redis key 后 try_load 仍能从 Postgres 拿回同一份摘要
7. 幂等：同一会话重复 fold，不产生新的折叠（covered 不前进、摘要不变）
8. 增量：再追加一轮，下一轮 fold 只折叠新滑出的消息，covered 前进到新水位
9. 降级：`_llm=None` 时 fold 静默跳过、不抛
10. clear：Redis 与 DB 都清干净

⚠️ 本模块读写失败都只 WARN，所以第 0 步必须显式 ping 确认 Redis 真通，否则断言会假绿。
第 3/8 步是真实 LLM 调用（fold），会消耗少量额度。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_chat_summary.py

不污染持久层：临时 user/session，结束前删会话 + 摘要。
"""

import asyncio
import json
import sys

from app.common.snowflake import next_snowflake_id
from app.core.config import settings
from app.db.redis import redis_service
from app.db.session import async_session_factory, engine
from app.models.ai_session import (
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    AiMessage,
)
from app.models.ai_session_summary import AiSessionSummary
from app.schemas.ai import CreateSessionRequest
from app.services.chat_session import chat_session_service
from app.services.chat_short_memory import chat_short_memory_service
from app.services.chat_summary import chat_session_summary_service

USER_ID = "990000000000000921"
WINDOW = chat_short_memory_service.window_size
# 早期（会滑出窗口）里埋一个只可能靠摘要带出来的事实
EARLY_FACT = "工号 EMP-7788"


def summary_key(session_id: str) -> str:
    return f"{settings.CHAT_SHORT_MEMORY_KEY_PREFIX}:{USER_ID}:{session_id}:summary"


async def insert_messages(session_id: int, count: int, start: int = 0) -> list[int]:
    """直接插 count 条消息（user/assistant 交替），返回按插入顺序的 id 列表

    用 next_snowflake_id 手工生成，保证 id 单调递增（等价 append_turn 的落库顺序）。
    """
    ids: list[int] = []
    async with async_session_factory() as db:
        for i in range(start, start + count):
            mid = next_snowflake_id()
            role = MESSAGE_ROLE_USER if i % 2 == 0 else MESSAGE_ROLE_ASSISTANT
            if i == 0:
                content = f"请记住：我的{EARLY_FACT}。"
            elif i == 1:
                content = f"好的，已记住{EARLY_FACT}。"
            else:
                content = f"第{i}轮：关于差旅制度的第{i}个细节问题与回答。"
            db.add(AiMessage(id=mid, session_id=session_id, role=role, content=content))
            ids.append(mid)
        await db.commit()
    return ids


async def cleanup(session_id: str | None) -> None:
    """清会话（连带消息）+ 摘要（幂等；失败不阻塞收尾）"""
    try:
        if session_id:
            async with async_session_factory() as db:
                await chat_session_service.remove(db, USER_ID, session_id)
    except Exception as err: # noqa: BLE001
        print(f" (cleanup 会话跳过：{err})", file=sys.stderr)
    try:
        if session_id:
            await chat_session_summary_service.clear(USER_ID, session_id)
    except Exception as err: # noqa: BLE001
        print(f" (cleanup 摘要跳过：{err})", file=sys.stderr)


async def main() -> None:
    has_key = bool(
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    print(
        f" · 配置：LLM Key={'已配置' if has_key else '未配置'}、"
        f"窗口={WINDOW} 条、摘要上限={settings.CHAT_SUMMARY_MAX_CHARS} 字"
    )

    # 0) Redis 必须真通（save 吞异常，否则断言假绿）
    await redis_service.start()
    client = redis_service.client
    assert client is not None, "Redis 未连接"
    await client.ping()
    print(" 0) Redis 连接 ✓ 已确认可用")

    # 1) 前提断言
    assert chat_session_summary_service.enabled is has_key, (
        "enabled 必须与「是否配置 LLM Key」一致，否则折叠断言会假绿"
    )
    print(f" 1) 前提断言 ✓ enabled={chat_session_summary_service.enabled}")

    session_id: str | None = None
    try:
        async with async_session_factory() as db:
            sess = await chat_session_service.create(
                db, USER_ID, CreateSessionRequest(title="摘要冒烟")
            )
            sid = sess.id
        session_id = str(sid)
        print(f" 2) 建临时会话 ✓ sessionId={session_id}")

        # 2) 未折叠 → None
        assert await chat_session_summary_service.try_load(USER_ID, session_id) is None
        print(" 3) 未折叠 try_load ✓ 返回 None")

        # 3) 24 条 > 窗口 20 → fold 只折叠滑出的前 4 条
        ids = await insert_messages(sid, WINDOW + 4)
        await chat_session_summary_service.fold(USER_ID, session_id, WINDOW)
        state = await chat_session_summary_service.try_load(USER_ID, session_id)
        if not has_key:
            assert state is None, "未配 LLM Key 时 fold 应跳过，不留摘要"
            print(" 4) SKIP —— 未配置 LLM Key，折叠未验证（SKIP 不算通过）")
        else:
            assert state is not None and state.summary.strip(), "fold 后应有摘要"
            assert state.covered_until_message_id == ids[3], (
                f"水位应停在第 4 条（滑出前 4 条），实际 {state.covered_until_message_id} vs {ids[3]}"
            )
            assert len(state.summary) <= settings.CHAT_SUMMARY_MAX_CHARS + 1, "摘要不应超限"
            assert "EMP-7788" in state.summary, (
                f"窗口外的早期事实应被摘要抓住，实际摘要：{state.summary!r}"
            )
            print(f" 4) 折叠 ✓ covered={state.covered_until_message_id}（=第4条）")
            print(f" 摘要({len(state.summary)}字)：{state.summary[:100]}...")

            # 4) 双写：Redis 与 DB 都在且一致
            raw = await redis_service.get(summary_key(session_id))
            assert raw is not None, "Redis 摘要 key 应存在"
            redis_state = json.loads(raw)
            async with async_session_factory() as db:
                row = await db.get(AiSessionSummary, sid)
            assert row is not None, "Postgres 摘要行应存在"
            assert redis_state["summary"] == row.summary == state.summary, "Redis/DB 摘要应一致"
            assert redis_state["covered_until_message_id"] == row.covered_until_message_id
            print(" 5) 双写 ✓ Redis + kh_ai_session_summary 一致")

            # 5) 删 Redis → DB 兜底
            await redis_service.delete(summary_key(session_id))
            restored = await chat_session_summary_service.try_load(USER_ID, session_id)
            assert restored is not None and restored.summary == state.summary, (
                "删 Redis 后应从 Postgres 拿回同一份摘要"
            )
            assert restored.covered_until_message_id == state.covered_until_message_id
            print(" 6) DB 兜底 ✓ 删 Redis 后仍能读回摘要")

            # 6) 幂等：重复 fold 不产生新折叠
            await chat_session_summary_service.fold(USER_ID, session_id, WINDOW)
            again = await chat_session_summary_service.try_load(USER_ID, session_id)
            assert again is not None
            assert again.covered_until_message_id == state.covered_until_message_id, (
                "无新滑出消息时水位不应前进"
            )
            assert again.summary == state.summary, "幂等 fold 不应改动摘要"
            print(" 7) 幂等 ✓ 重复 fold 水位/摘要不变")

            # 7) 增量：再追加一轮 → 只折叠新滑出的 2 条，水位前进
            await insert_messages(sid, 2, start=WINDOW + 4)
            await chat_session_summary_service.fold(USER_ID, session_id, WINDOW)
            advanced = await chat_session_summary_service.try_load(USER_ID, session_id)
            assert advanced is not None
            assert advanced.covered_until_message_id > state.covered_until_message_id, (
                "追加消息后水位应前进"
            )
            print(
                f" 8) 增量 ✓ 水位 {state.covered_until_message_id} → "
                f"{advanced.covered_until_message_id}"
            )

            # 8) 摘要 SystemMessage 形态
            msg = chat_session_summary_service.build_system_message(advanced)
            assert msg is not None and "【早前对话摘要】" in msg.content
            assert chat_session_summary_service.build_system_message(None) is None
            print(" 9) build_system_message ✓ 格式正确，空摘要返回 None")

        # 9) 降级：无 LLM 时 fold 静默跳过
        saved_llm = chat_session_summary_service._llm
        chat_session_summary_service._llm = None
        try:
            await chat_session_summary_service.fold(USER_ID, session_id, WINDOW)
            print(" 10) 降级 ✓ 无 LLM 时 fold 静默返回不抛")
        finally:
            chat_session_summary_service._llm = saved_llm

        # 10) clear：Redis + DB 都清
        await chat_session_summary_service.clear(USER_ID, session_id)
        assert await redis_service.get(summary_key(session_id)) is None, "clear 后 Redis 应无 key"
        async with async_session_factory() as db:
            assert await db.get(AiSessionSummary, sid) is None, "clear 后 DB 行应删除"
        print(" 11) clear ✓ Redis 与 Postgres 均已清")
    finally:
        await cleanup(session_id)
        await redis_service.close()
        await engine.dispose()

    print("\n会话滚动摘要冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
