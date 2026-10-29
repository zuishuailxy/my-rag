"""长期记忆（Mem0）：检索整体预算 1500ms，超时降级为空，不阻断对话。

这里只测**超时与降级**这一层；记忆的召回质量由评测集覆盖，不在单测范围。
"""

import asyncio
import time
from typing import Any

from app.services.chat_long_memory import ChatLongMemoryService, _search_timeout_s

_EMPTY = {"user": [], "session": []}


class _Client:
    """假 Mem0 客户端：按给定次数返回结果，或一直挂着"""

    def __init__(self, delay: float = 0.0, payload: dict[str, Any] | None = None) -> None:
        self.delay = delay
        self.payload = payload or {"results": []}
        self.calls = 0
        self.filters: list[dict[str, Any]] = []

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.filters.append(kwargs.get("filters") or {})
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload


def _service(client: _Client, timeout_s: float | None) -> ChatLongMemoryService:
    """只装 search 需要的三个属性

    刻意绕开 `__init__`：它要构造 Mem0 客户端（约 1.3s），而本文件的被测对象
    是「超时与降级」这段逻辑，不是客户端构造。构造与换算由
    `test_timeout_config_mapping` 单独覆盖。
    """
    service = object.__new__(ChatLongMemoryService)
    service._client = client  # type: ignore[assignment]
    service._top_k = 5
    service._timeout_s = timeout_s
    return service


async def test_timeout_returns_empty_within_budget() -> None:
    """慢 Mem0 必须在预算内放弃，而不是把 TTFT 拖到 60 秒"""
    client = _Client(delay=5.0)
    service = _service(client, timeout_s=0.05)

    started = time.perf_counter()
    hits = await service.search("1", "2", "问题")
    cost = time.perf_counter() - started

    assert hits == _EMPTY, "超时应按无记忆处理"
    assert cost < 0.5, f"实际耗时 {cost:.2f}s，把预算当摆设了"


async def test_budget_covers_both_layers_not_each() -> None:
    """两层共用一个 deadline：单层就超预算时，不该有机会再发第二个请求"""
    client = _Client(delay=0.6)  # 比预算长，第一层就吃掉
    service = _service(client, timeout_s=0.5)

    started = time.perf_counter()
    hits = await service.search("1", "2", "问题")
    cost = time.perf_counter() - started

    assert hits == _EMPTY
    assert cost < 0.7, f"实际 {cost:.2f}s ≈ 两层各 0.6s，说明超时是按层而不是整体"
    assert client.calls == 1, "第一层已超预算，不应再发第二个请求"


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
