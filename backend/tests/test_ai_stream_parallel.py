"""流式链路：classify 与 Mem0 并行、异常时不泄漏后台 task。

**不连数据库**：`async_session_factory()` 只创建 Session 对象，第一次执行 SQL 才借连接。
这里把会话创建 / 历史加载 / 摘要读取全部换成 fake，整条链路一次 SQL 都不打，
所以能在没有 PG 的 CI 里跑。
"""

import asyncio
import time
from typing import Any

import pytest

from app.schemas.ai import ChatStreamMessage, ChatStreamMessagePart, ChatStreamRequest
from app.schemas.auth import AuthUser
from app.services import ai_stream as ai_stream_module
from app.services.ai_stream import ai_stream_service
from app.services.chat_long_memory import chat_long_memory_service
from app.services.chat_query_rewrite import chat_query_rewrite_service
from app.services.chat_session import chat_session_service
from app.services.chat_summary import chat_session_summary_service
from app.services.turn_timing import TurnTiming

USER = AuthUser(user_id="1", username="tester")
PLAN: dict[str, Any] = {
    "intent": "kb",
    "label": "知识库",
    "query": "问题",
    "graph_queries": [],
    "allow_retrieve": False,
    "allow_graph": False,
    "allow_web": False,
}


class _FakeSession:
    id = 123


def _request() -> ChatStreamRequest:
    return ChatStreamRequest(
        messages=[
            ChatStreamMessage(
                role="user", parts=[ChatStreamMessagePart(type="text", text="问题")]
            )
        ]
    )


def _patch_db_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """把三个碰库的调用换成 fake，让 stream_chat 不依赖 PG"""

    async def fake_create(db: Any, user_id: str, dto: Any) -> _FakeSession:
        return _FakeSession()

    async def fake_history(db: Any, user_id: str, session_id: str) -> list[Any]:
        return []

    async def fake_try_load(user_id: str, session_id: str) -> None:
        return None

    monkeypatch.setattr(chat_session_service, "create", fake_create)
    monkeypatch.setattr(ai_stream_service, "load_working_history", fake_history)
    monkeypatch.setattr(chat_session_summary_service, "try_load", fake_try_load)
    # agent 为 None 走「未配置 LLM Key」的早退分支：本文件只测前段时序
    monkeypatch.setattr(ai_stream_service, "create_turn_agent", lambda *a, **k: None)


async def test_intent_frame_does_not_wait_for_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """意图卡必须只等 classify；等 Mem0 是纯等待，用户能看出来"""
    _patch_db_side(monkeypatch)
    classify_sleep, memory_sleep = 0.05, 0.30

    async def fake_classify(question: str, history: list[Any]) -> dict[str, Any]:
        await asyncio.sleep(classify_sleep)
        return PLAN

    async def fake_search(
        user_id: str, session_id: str | None, query: str
    ) -> dict[str, Any]:
        await asyncio.sleep(memory_sleep)
        return {"user": [], "session": []}

    monkeypatch.setattr(chat_query_rewrite_service, "classify", fake_classify)
    monkeypatch.setattr(chat_long_memory_service, "search", fake_search)

    started = time.perf_counter()
    intent_at: float | None = None
    async for frame in ai_stream_service.stream_chat(_request(), USER):
        # ⚠️ 用 data-intent 而不是 "intent"：status 帧里也有 stage=intent
        if intent_at is None and '"data-intent"' in frame:
            intent_at = time.perf_counter() - started
    finished = time.perf_counter() - started

    assert intent_at is not None, "应发出 intent 帧"
    assert intent_at < classify_sleep + 0.06, (
        f"intent 帧在 {intent_at:.3f}s 才发出，说明被 Mem0 拖住了"
    )
    assert finished >= memory_sleep, "本轮仍要等 Mem0 回来才能拼 messages"
    assert finished < classify_sleep + memory_sleep, (
        f"总耗时 {finished:.3f}s ≈ 两段串行之和，说明根本没并行"
    )


async def test_classify_error_cancels_memory_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classify 抛错时，后台的 Mem0 task 不能悬着（会占连接 + 事件循环关不掉）"""
    _patch_db_side(monkeypatch)
    cancelled = asyncio.Event()

    async def slow_search(
        user_id: str, session_id: str | None, query: str
    ) -> dict[str, Any]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"user": [], "session": []}

    async def boom(question: str, history: list[Any]) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        raise RuntimeError("classify 挂了")

    monkeypatch.setattr(chat_long_memory_service, "search", slow_search)
    monkeypatch.setattr(chat_query_rewrite_service, "classify", boom)

    with pytest.raises(RuntimeError, match="classify 挂了"):
        async for _ in ai_stream_service.stream_chat(_request(), USER):
            pass

    await asyncio.wait_for(cancelled.wait(), timeout=1.0)


async def test_memory_failure_does_not_break_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mem0 炸了也只能降级，不能影响意图帧与本轮继续"""
    _patch_db_side(monkeypatch)

    async def fake_classify(question: str, history: list[Any]) -> dict[str, Any]:
        return PLAN

    async def broken_search(
        user_id: str, session_id: str | None, query: str
    ) -> dict[str, Any]:
        return {"user": [], "session": []}  # 真实实现：异常内部吞掉后返回空

    monkeypatch.setattr(chat_query_rewrite_service, "classify", fake_classify)
    monkeypatch.setattr(chat_long_memory_service, "search", broken_search)

    frames = [f async for f in ai_stream_service.stream_chat(_request(), USER)]
    assert any('"data-intent"' in f for f in frames), "意图帧仍要发出"
    assert any('"type": "error"' in f for f in frames), "无 LLM Key 走早退的错误帧"


async def test_memory_span_excludes_classify_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`spans.memory` 必须是记忆**自己**的耗时，不能把 classify 的墙钟算进去

    回归锚点：把 `span_start()` 挪回 `create_task` 之前，这个 span 会横跨
    classify，值涨到 ≈ 200ms（本用例阈值 100ms）→ 必红。

    真实踩过：75 条压测里 `spans.memory` 与 `spans.classify` 的 p50 只差 157ms、
    p95 只差 0.1ms —— 指标成了 classify 的复读机，还被当成「记忆本身要 1.3 秒」
    写进了文档。
    """
    _patch_db_side(monkeypatch)
    captured: dict[str, TurnTiming] = {}

    class _RecordingTiming(TurnTiming):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured["timing"] = self

    monkeypatch.setattr(ai_stream_module, "TurnTiming", _RecordingTiming)

    async def slow_classify(question: str, history: list[Any]) -> dict[str, Any]:
        await asyncio.sleep(0.20)
        return PLAN

    async def quick_search(
        user_id: str, session_id: str | None, query: str
    ) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return {"user": [], "session": []}

    monkeypatch.setattr(chat_query_rewrite_service, "classify", slow_classify)
    monkeypatch.setattr(chat_long_memory_service, "search", quick_search)

    async for _ in ai_stream_service.stream_chat(_request(), USER):
        pass

    spans = captured["timing"].spans
    assert spans["classify"] >= 200, f"classify 应测到 200ms 以上，实得 {spans['classify']}"
    assert spans["memory"] < 100, (
        f"memory span={spans['memory']}ms ≈ classify 的墙钟，"
        "说明计时起点放到了 task 外面"
    )
