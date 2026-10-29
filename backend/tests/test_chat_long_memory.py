"""长期记忆（Mem0）：两层并行召回，整体预算 1500ms，超时/配额耗尽降级为空，不阻断对话。

这里只测**并发、超时、降级与配额熔断**这一层；记忆的召回质量由评测集覆盖，不在单测范围。
"""

import asyncio
import logging
import time
from typing import Any

import pytest
from mem0.exceptions import RateLimitError, ValidationError

from app.services.chat_long_memory import (
    ChatLongMemoryService,
    _is_quota_error,
    _search_timeout_s,
)

_EMPTY = {"user": [], "session": []}


class _Client:
    """假 Mem0 客户端：按给定耗时返回结果、让指定层抛错、或整体回 429"""

    def __init__(
        self,
        delay: float = 0.0,
        payload: dict[str, Any] | None = None,
        fail_layer: str | None = None,
        quota: bool = False,
        error: BaseException | None = None,
    ) -> None:
        self.delay = delay
        self.payload = payload or {"results": []}
        self.fail_layer = fail_layer
        self.quota = quota
        self.error = error
        self.calls = 0
        self.filters: list[dict[str, Any]] = []

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        filters = kwargs.get("filters") or {}
        self.filters.append(filters)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.quota:
            # 真实形态：RateLimitError(message, error_code)，无 status_code 属性
            raise RateLimitError(
                '{"error":"Monthly retrieval limit reached (1000 of 1000)."}',
                "HTTP_429",
            )
        # 用有没有 run_id 区分层，与真实调用一致（session 层才叠加 run_id）
        if self.fail_layer == "session" and "run_id" in filters:
            raise RuntimeError("session 层炸了")
        if self.fail_layer == "user" and "run_id" not in filters:
            raise RuntimeError("user 层炸了")
        return self.payload


def _service(client: _Client, timeout_s: float | None) -> ChatLongMemoryService:
    """只装 search 需要的属性

    刻意绕开 `__init__`：它要构造 Mem0 客户端（约 1.3s），而本文件的被测对象
    是「并发、超时与降级」这段逻辑，不是客户端构造。构造与换算由
    `test_timeout_config_mapping` 单独覆盖。
    """
    service = object.__new__(ChatLongMemoryService)
    service._client = client  # type: ignore[assignment]
    service._top_k = 5
    service._timeout_s = timeout_s
    service._quota_blocked_until = 0.0
    service._quota_suppressed = 0
    service._inflight = set()
    return service


async def _drain(service: ChatLongMemoryService) -> None:
    """取消残留的在途请求：超时轮次故意不取消它们，测试结束前得收掉"""
    for task in list(service._inflight):
        task.cancel()
    await asyncio.sleep(0)


async def test_layers_run_concurrently() -> None:
    """两层必须同时发出：各睡 0.4s，串行会是 0.8s

    变异测试锚点：把 `search` 改回顺序 await，这条必须变红。
    """
    client = _Client(delay=0.4)
    service = _service(client, timeout_s=2.0)

    started = time.perf_counter()
    hits = await service.search("1", "2", "问题")
    cost = time.perf_counter() - started

    assert client.calls == 2, "有 sessionId 时两层都要发"
    assert hits == _EMPTY
    assert cost < 0.65, f"实际 {cost:.2f}s ≈ 两次 0.4s 串行，说明两层没有并行"


async def test_one_layer_failure_keeps_other() -> None:
    """一层挂了不能拖空另一层：return_exceptions 让两层各自降级"""
    client = _Client(
        payload={"results": [{"memory": "用户偏好简洁回答"}]},
        fail_layer="user",
    )
    service = _service(client, timeout_s=1.0)

    hits = await service.search("1", "2", "问题")

    assert hits["user"] == [], "失败的那层降级为空"
    assert hits["session"], "另一层的结果必须留下"


async def test_no_session_id_sends_one_request() -> None:
    """无 sessionId 时不为会话层白跑一次"""
    client = _Client()
    service = _service(client, timeout_s=1.0)

    hits = await service.search("1", None, "问题")

    assert client.calls == 1
    assert hits == _EMPTY


async def test_timeout_returns_empty_within_budget() -> None:
    """慢 Mem0 必须在预算内放弃，而不是把 TTFT 拖到 60 秒"""
    client = _Client(delay=5.0)
    service = _service(client, timeout_s=0.05)

    started = time.perf_counter()
    hits = await service.search("1", "2", "问题")
    cost = time.perf_counter() - started

    assert hits == _EMPTY, "超时应按无记忆处理"
    assert cost < 0.5, f"实际耗时 {cost:.2f}s，把预算当摆设了"
    assert len(service._inflight) == 2, "超时轮次的请求不取消，留给迟到判定"
    await _drain(service)


async def test_budget_is_one_deadline_for_both_layers() -> None:
    """两层共用一个 deadline：整体墙钟不超过预算，不是每层各一份

    行为变化（规格 §4）：并行后无法在发出前预知超时，所以超时轮次两层请求
    都已经发出（calls=2）；串行时是 1。这是并行的代价，不是回归。
    """
    client = _Client(delay=0.6)  # 比预算长
    service = _service(client, timeout_s=0.5)

    started = time.perf_counter()
    hits = await service.search("1", "2", "问题")
    cost = time.perf_counter() - started

    assert hits == _EMPTY
    assert cost < 0.7, f"实际 {cost:.2f}s ≈ 两层各 0.6s，说明超时是按层而不是整体"
    assert client.calls == 2, "并行后两层都已发出，无法像串行那样省掉第二次"
    await _drain(service)


async def test_fast_client_returns_hits() -> None:
    payload = {"results": [{"memory": "用户偏好简洁回答"}]}
    client = _Client(payload=payload)
    service = _service(client, timeout_s=1.5)

    hits = await service.search("1", "2", "问题")

    assert hits["user"], "快路径应解析出记忆"
    assert client.calls == 2, "有 sessionId 时应查 user + session 两层"

    hits_no_session = await service.search("1", None, "问题")
    assert hits_no_session["session"] == [], "无 sessionId 不查会话层"


async def test_no_budget_means_no_timeout() -> None:
    """预算配 0 视为不限：否则记忆功能会静默全失效，排查成本高"""
    client = _Client(delay=0.01)
    service = _service(client, timeout_s=None)

    hits = await service.search("1", "2", "问题")
    assert hits == _EMPTY  # 空结果而非超时
    assert client.calls == 2


def test_timeout_config_mapping() -> None:
    """毫秒 → 秒的换算；≤0 视为不限（纯函数，不构造服务）"""
    assert _search_timeout_s(1500) == 1.5
    assert _search_timeout_s(0) is None, "0 表示不限，不是「立刻全部超时」"
    assert _search_timeout_s(-1) is None


# ---------- 配额熔断（429） ----------

_LOG = "ChatLongMemoryService"


def _quota_warns(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "配额耗尽" in r.getMessage()]


async def test_quota_error_returns_empty_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """429 是「没额度」不是「检索故障」：返回空 + 一条可操作的 WARN"""
    client = _Client(quota=True)
    service = _service(client, timeout_s=1.0)

    with caplog.at_level(logging.WARNING, logger=_LOG):
        hits = await service.search("1", "2", "问题")

    assert hits == _EMPTY, "配额耗尽按无记忆处理"
    assert client.calls == 2, "窗口还没打开，本轮两层请求都已发出"
    warns = _quota_warns(caplog)
    assert len(warns) == 1, f"两层都 429 也只能有一条 WARN，实际 {len(warns)} 条"
    assert "MEM0_API_KEY" in warns[0], "WARN 必须给出可操作的动作，不能只说失败"
    layer_fails = [r for r in caplog.records if "层检索失败" in r.getMessage()]
    assert layer_fails == [], "429 不能被写成「某层检索失败」，那会把真因藏起来"


async def test_breaker_skips_network_for_later_turns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G1：窗口内一次网络请求都不发 —— 止损点就在这，不是「失败了也无所谓」"""
    client = _Client(quota=True)
    service = _service(client, timeout_s=1.0)

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await service.search("1", "2", "问题")  # 撞 429，开窗
        caplog.clear()

        started = time.perf_counter()
        hits = await service.search("1", "2", "问题")
        cost = time.perf_counter() - started

    assert hits == _EMPTY
    assert client.calls == 2, "窗口内的检索不能再碰网络"
    assert cost < 0.01, f"实际 {cost * 1000:.1f}ms，熔断后应几乎为 0"
    assert _quota_warns(caplog) == [], "窗口内不许刷日志，否则每轮一条"


async def test_breaker_reopens_after_window() -> None:
    """G3：窗口过期必须再探一次 —— 换 key / 配额重置后不重启进程也能恢复"""
    client = _Client(quota=True)
    service = _service(client, timeout_s=1.0)

    await service.search("1", "2", "问题")
    assert client.calls == 2

    service._quota_blocked_until = time.monotonic() - 1  # 模拟 10 分钟已过
    await service.search("1", "2", "问题")
    assert client.calls == 4, "窗口过期后应重新发请求（探测）"


async def test_other_failure_does_not_open_breaker() -> None:
    """R5：一次网络抖动不能关掉记忆 10 分钟"""
    client = _Client(payload={"results": [{"memory": "用户偏好简洁回答"}]}, fail_layer="user")
    service = _service(client, timeout_s=1.0)

    first = await service.search("1", "2", "问题")
    second = await service.search("1", "2", "问题")

    assert first["session"], "非配额异常仍按层降级，另一层要留下"
    assert second["session"], "第二轮仍要真的去查"
    assert client.calls == 4, "没熔断就该每轮都发请求"


async def test_suppressed_count_reported_on_next_trip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R4：窗口内跳过多少次，下次开窗一并报出 —— 这个数才是熔断省下的量"""
    client = _Client(quota=True)
    service = _service(client, timeout_s=1.0)

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await service.search("1", "2", "问题")  # 开窗
        for _ in range(3):
            await service.search("1", "2", "问题")  # 全被跳过
        caplog.clear()

        service._quota_blocked_until = time.monotonic() - 1
        await service.search("1", "2", "问题")  # 再撞一次

    warns = _quota_warns(caplog)
    assert len(warns) == 1, f"实际 {len(warns)} 条"
    assert "跳过 3 次" in warns[0], f"应上报上一窗口的跳过次数：{warns[0]}"


async def test_text_mentioning_429_is_not_quota() -> None:
    """R1：按**类型**判，不按文本判

    `ValidationError("filters.user_id='429' 非法")` 文本里有 429，但它不是配额错误 ——
    若改成 message 子串匹配，这条会误熔断，把记忆静默关掉 10 分钟。
    """
    client = _Client(error=ValidationError("filters.user_id='429' 非法", "HTTP_400"))
    service = _service(client, timeout_s=1.0)

    await service.search("1", "2", "问题")
    await service.search("1", "2", "问题")

    assert client.calls == 4, "非配额错误不许熔断，每轮都得真的去查"


def test_quota_detection_covers_error_code_fallback() -> None:
    """SDK 若重命名异常类，兜底认 error_code；两者都没有就一律不算配额错误"""

    class _Renamed(Exception):
        error_code = "HTTP_429"

    assert _is_quota_error(RateLimitError("x", "HTTP_429")) is True
    assert _is_quota_error(_Renamed()) is True, "类名变了也要认得出来"
    assert _is_quota_error(ValidationError("x", "HTTP_400")) is False
    assert _is_quota_error(RuntimeError("429 Too Many Requests")) is False


async def test_late_429_still_opens_breaker() -> None:
    """429 比预算慢也必须被看见

    实测 429 往返 300~1500ms、冷启动更久，经常比 1500ms 预算还慢。若超时就取消请求，
    熔断能否生效取决于「429 是否刚好比预算快」（实测 4 次只撞上 1 次）——
    这条用例锚住「迟到的 429 记给下一轮」。
    """
    client = _Client(delay=0.2, quota=True)  # 429 比 0.05 的预算慢
    service = _service(client, timeout_s=0.05)

    first = await service.search("1", "2", "问题")
    assert first == _EMPTY, "本轮按超时放弃"
    assert client.calls == 2
    assert service._quota_blocked_until == 0.0, "此刻还没看到 429"

    await asyncio.sleep(0.3)  # 等在途请求跑完
    assert service._quota_blocked_until > time.monotonic(), "迟到的 429 也要开窗"

    second = await service.search("1", "2", "问题")
    assert second == _EMPTY
    assert client.calls == 2, "窗口已开，下一轮不能再发请求"


