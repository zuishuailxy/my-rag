"""流式链路连接占用冒烟（真 Postgres + 真 Redis + fake LLM）

验证 docs/specs/2026-10-29-流式链路资源与延迟治理.md 的 G1：
单请求在 classify / Mem0 / 流式期间**不占用 PG 连接**。

为什么单独写这个脚本，而不是加进 pytest：
  - 它需要真库与真 Redis，pytest 套件是纯逻辑、CI 里没有依赖；
  - 它要断言的 `engine.pool.checkedout()` 是连接池的真实状态，
    fake 不出来 —— mock 掉就变成「测我自己写的 mock」。

三步：
  1. 单请求：在 classify 睡眠期间采样 `checkedout()`，必须恒为 0
     （修复前这里恒为 1，且状态是 idle in transaction）
  2. 并发 40：全部成功、无 pool_timeout，峰值连接数不随并发线性增长
  3. 落库块仍工作：本轮问答落库成功（2 条消息），结束后连接归零

用法：PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_stream_pool.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import traceback
from typing import Any

from langchain_core.messages import AIMessageChunk
from sqlalchemy import event, text

from app.core.config import settings
from app.db.redis import redis_service
from app.db.session import async_session_factory, engine
from app.schemas.ai import ChatStreamMessage, ChatStreamMessagePart, ChatStreamRequest
from app.schemas.auth import AuthUser
from app.services.ai_stream import ai_stream_service
from app.services.chat_long_memory import chat_long_memory_service
from app.services.chat_query_rewrite import chat_query_rewrite_service

MARKER = "连接占用冒烟"
# 模拟 classify 的真实耗时（实测 p50 1653ms，这里缩短以免冒烟太慢）。
# 必须**明显大于**持有时间阈值：否则「占着连接跨过 classify」与「事件循环排队抖动」
# 在数值上分不开 —— 前者约等于本值，后者是一两百毫秒。
CLASSIFY_SLEEP = 0.80
CONCURRENCY = 40
HOLD_LIMIT_MS = 400


# ------------------------------------------------------------------ fake 依赖


class _FakeAgent:
    """只推一个 text delta 的 agent stub：不联网、不建模型"""

    async def astream(self, inputs: Any, stream_mode: Any = None) -> Any:
        yield "messages", (AIMessageChunk(content="冒烟回答"), {"tags": []})


def _plan(question: str) -> dict[str, Any]:
    return {
        "intent": "kb",
        "label": "知识库",
        "query": question,
        "graph_queries": [],
        "allow_retrieve": False,
        "allow_graph": False,
        "allow_web": False,
    }


async def _fake_classify(question: str, history: Any) -> dict[str, Any]:
    """睡眠期间反复采样池占用：这段本该完全不碰库"""
    deadline = time.perf_counter() + CLASSIFY_SLEEP
    while time.perf_counter() < deadline:
        _classify_samples.append(engine.pool.checkedout())
        await asyncio.sleep(0.005)
    return _plan(question)


async def _fake_search(user_id: str, session_id: str | None, query: str) -> Any:
    await asyncio.sleep(0.05)  # 模拟外部 HTTP
    return {"user": [], "session": []}


async def _fake_remember(*args: Any, **kwargs: Any) -> None:
    """后台写入不测，避免真打 Mem0"""


_classify_samples: list[int] = []
_peak_samples: list[int] = []


# ------------------------------------------------------------------ 持有时间

# 池事件：记录每条连接的「借出 → 归还」时长。
# 这是本 spec 的 G1 度量 —— 峰值连接数分不清「同时进来」和「占着不放」，
# 持有时间才能。修复前同口径是 ~4.6s（连接陪着 classify + Mem0 + 流式）。
_hold_started: dict[int, float] = {}
_holds_ms: list[float] = []


@event.listens_for(engine.sync_engine, "checkout")
def _on_checkout(dbapi_connection: Any, record: Any, proxy: Any) -> None:
    _hold_started[id(record)] = time.perf_counter()


@event.listens_for(engine.sync_engine, "checkin")
def _on_checkin(dbapi_connection: Any, record: Any) -> None:
    started = _hold_started.pop(id(record), None)
    if started is not None:
        _holds_ms.append((time.perf_counter() - started) * 1000)


def _reset_hold() -> None:
    _hold_started.clear()
    _holds_ms.clear()


def _pct(values: list[float], p: float) -> float:
    """最近秩法（与 load_test._pct 同口径）"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1)
    return ordered[idx]


# ------------------------------------------------------------------ 采样


async def _sampler(stop: asyncio.Event) -> None:
    """全程采样池占用峰值（含 DB 块本身，所以峰值必然 >0）"""
    while not stop.is_set():
        _peak_samples.append(engine.pool.checkedout())
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ 用例


def _request(question: str, session_id: str | None = None) -> ChatStreamRequest:
    return ChatStreamRequest(
        messages=[
            ChatStreamMessage(
                role="user", parts=[ChatStreamMessagePart(type="text", text=question)]
            )
        ],
        sessionId=session_id,
        topK=3,
    )


async def _drive(req: ChatStreamRequest, user: AuthUser) -> list[str]:
    """消费整个 SSE 生成器，返回事件帧"""
    return [frame async for frame in ai_stream_service.stream_chat(req, user)]


def _session_id_of(frames: list[str]) -> str:
    """从 session 帧里取 sessionId（帧是 `data: {json}`，json 默认带空格）"""
    for frame in frames:
        found = re.search(r'"sessionId"\s*:\s*"?(\d+)"?', frame)
        if found:
            return found.group(1)
    raise AssertionError(f"应发出 session 帧，实际帧首条：{frames[:1]!r}")


async def _count_messages(session_id: str) -> int:
    async with async_session_factory() as db:
        row = await db.execute(
            text("SELECT count(*) FROM kh_ai_message WHERE session_id = :s"),
            {"s": int(session_id)},
        )
        return int(row.scalar() or 0)


async def _pick_user() -> AuthUser:
    async with async_session_factory() as db:
        row = await db.execute(text("SELECT id, username FROM kh_user LIMIT 1"))
        found = row.first()
    if found is None:
        print("  ✗ kh_user 里没有用户，无法冒烟（先注册或灌数据）")
        sys.exit(1)
    return AuthUser(user_id=str(found[0]), username=str(found[1]))


async def _cleanup() -> int:
    async with async_session_factory() as db:
        ids = [
            row[0]
            for row in (
                await db.execute(
                    text("SELECT id FROM kh_ai_session WHERE title LIKE :m"),
                    {"m": f"%{MARKER}%"},
                )
            ).all()
        ]
        if not ids:
            return 0
        await db.execute(
            text("DELETE FROM kh_ai_message WHERE session_id = ANY(:ids)"), {"ids": ids}
        )
        await db.execute(
            text("DELETE FROM kh_ai_session WHERE id = ANY(:ids)"), {"ids": ids}
        )
        await db.commit()
    return len(ids)


# ------------------------------------------------------------------ main


async def main() -> int:
    await redis_service.start()
    user = await _pick_user()

    # fake 掉三个外部调用：只留 DB 行为是真的
    saved = (
        chat_query_rewrite_service.classify,
        chat_long_memory_service.search,
        chat_long_memory_service.remember_turn,
        ai_stream_service.create_turn_agent,
    )
    chat_query_rewrite_service.classify = _fake_classify  # type: ignore[method-assign]
    chat_long_memory_service.search = _fake_search  # type: ignore[method-assign]
    chat_long_memory_service.remember_turn = _fake_remember  # type: ignore[method-assign]
    ai_stream_service.create_turn_agent = lambda *a, **k: _FakeAgent()  # type: ignore[method-assign]

    stop = asyncio.Event()
    sampler = asyncio.create_task(_sampler(stop))
    try:
        print("1) 单请求：classify 期间必须 0 连接")
        _reset_hold()
        frames = await _drive(_request(f"{MARKER}-单请求"), user)
        sid = _session_id_of(frames)
        assert _classify_samples, "采样器没跑到"
        peak_classify = max(_classify_samples)
        print(
            f"   classify 期间采样 {len(_classify_samples)} 次，"
            f"checkedout 峰值 = {peak_classify}"
        )
        assert peak_classify == 0, (
            f"classify 期间不应占用连接，实际峰值 {peak_classify}"
            "（修复前恒为 1，状态 idle in transaction）"
        )
        hold = max(_holds_ms) if _holds_ms else 0.0
        print(f"   单次连接最长持有 {hold:.0f}ms（G1 要求 < 150ms；修复前约 4600ms）")
        assert hold < 150, f"单请求连接持有 {hold:.0f}ms 超过 G1 的 150ms 上限"
        print("   ✓ 外部调用阶段不占连接，且连接出块即归还")

        print("2) 落库块仍然工作")
        n = await _count_messages(sid)
        assert n == 2, f"本轮问答应落库 2 条消息，实际 {n}"
        print(f"   ✓ 会话 {sid} 落库 {n} 条消息")

        print(f"3) 并发 {CONCURRENCY}：全部成功、连接不随并发线性增长")
        _peak_samples.clear()
        _reset_hold()
        started = time.perf_counter()
        results = await asyncio.gather(
            *(_drive(_request(f"{MARKER}-并发{i}"), user) for i in range(CONCURRENCY)),
            return_exceptions=True,
        )
        elapsed = time.perf_counter() - started
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            print(f"   ✗ {len(errors)} 个请求失败，首个：")
            traceback.print_exception(errors[0])
            return 1
        peak_total = max(_peak_samples) if _peak_samples else 0
        hold_p50 = _pct(_holds_ms, 50)
        hold_p95 = _pct(_holds_ms, 95)
        hold_max = max(_holds_ms) if _holds_ms else 0.0
        print(
            f"   {CONCURRENCY} 个请求全部完成，耗时 {elapsed:.2f}s，"
            f"池占用峰值 = {peak_total} / 上限 "
            f"{settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW}"
        )
        print(
            f"   连接持有 p50={hold_p50:.0f}ms p95={hold_p95:.0f}ms max={hold_max:.0f}ms"
            f"（{len(_holds_ms)} 次借还）"
        )
        print(
            "   峰值贴近池上限是正常的：40 个请求在 t=0 同时进前置块（雷群），"
            "块内只有几十毫秒，排队即可消化；max 含事件循环抖动，不作判据"
        )
        assert peak_total < CONCURRENCY, (
            f"连接占用随并发线性增长（峰值 {peak_total}），说明外部调用仍在占连接"
        )
        assert hold_p95 < HOLD_LIMIT_MS, (
            f"并发下连接持有 p95={hold_p95:.0f}ms ≥ {HOLD_LIMIT_MS}ms："
            f"classify 睡了 {CLASSIFY_SLEEP * 1000:.0f}ms，"
            "若持有时间与之同量级，说明连接仍跨外部调用"
        )
        print("   ✓ 无 pool_timeout，连接占用未随并发线性增长")

        print("4) 结束后连接归还")
        await asyncio.sleep(0.3)  # 让 fire-and-forget 的 fold/save 收尾
        leaked = engine.pool.checkedout()
        assert leaked == 0, f"应有连接已全部归还，实际仍占用 {leaked}"
        print("   ✓ checkedout 已归零")
    finally:
        stop.set()
        await sampler
        await asyncio.sleep(0.1)
        (
            chat_query_rewrite_service.classify,
            chat_long_memory_service.search,
            chat_long_memory_service.remember_turn,
            ai_stream_service.create_turn_agent,
        ) = saved
        removed = await _cleanup()
        print(f"清理冒烟会话：{removed} 条")
        engine_pool = engine.pool
        await engine.dispose()
        await redis_service.close()
        print(f"（池最终状态：{engine_pool.status()}）")

    print("\n✅ 流式链路连接占用冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
