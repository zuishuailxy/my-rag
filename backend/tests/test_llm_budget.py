"""LLM 用量统计与限流（纯逻辑，mock 掉 Redis 与 LLMResult）

验证：
  - 成本估算（真实 token 数 → 元）
  - token 提取兼容两种 LLMResult 形态；取不到返回 None（**不按字数猜**）
  - usage handler 只在 track_turn 上下文内累加
  - 配额：放行 / 超限 / Redis 不可用 fail-open / 0 表示不限制 / 会话维度
"""
import app.services.llm_budget as budget
from app.services.llm_budget import (
    LlmUsageHandler,
    QuotaService,
    TurnUsage,
    current_usage,
    end_turn,
    extract_tokens,
    start_turn,
    track_turn,
)


# ---------- 成本估算 ----------


def test_cost_uses_separate_input_output_prices() -> None:
    usage = TurnUsage()
    usage.add(1_000_000, 0)
    assert usage.cost == 2.0  # 输入单价
    usage2 = TurnUsage()
    usage2.add(0, 1_000_000)
    assert usage2.cost == 8.0  # 输出单价（更贵）


def test_usage_accumulates_and_reports() -> None:
    usage = TurnUsage()
    usage.add(1000, 500)
    usage.add(2000, 200)
    assert usage.llm_calls == 2
    assert usage.total_tokens == 3700
    assert usage.as_log_fields()["estimatedCostYuan"] == round(
        (3000 * 2.0 + 700 * 8.0) / 1_000_000, 6
    )


# ---------- token 提取 ----------


class _FakeMessage:
    def __init__(self, usage_metadata: dict | None) -> None:
        self.usage_metadata = usage_metadata


class _FakeGeneration:
    def __init__(self, usage_metadata: dict | None = None) -> None:
        self.message = _FakeMessage(usage_metadata)


class _FakeResult:
    def __init__(self, generations=None, llm_output=None) -> None:  # noqa: ANN001
        self.generations = generations or []
        self.llm_output = llm_output


def test_extract_tokens_new_style() -> None:
    result = _FakeResult(
        generations=[[_FakeGeneration({"input_tokens": 120, "output_tokens": 30})]]
    )
    assert extract_tokens(result) == (120, 30)


def test_extract_tokens_legacy_style() -> None:
    result = _FakeResult(
        llm_output={"token_usage": {"prompt_tokens": 88, "completion_tokens": 12}}
    )
    assert extract_tokens(result) == (88, 12)


def test_extract_tokens_missing_returns_none() -> None:
    """取不到就返回 None：不按字数估算，避免污染成本口径"""
    assert extract_tokens(_FakeResult()) is None
    assert extract_tokens(_FakeResult(generations=[[_FakeGeneration(None)]])) is None


# ---------- usage handler ----------


def test_handler_ignores_calls_outside_turn() -> None:
    """不在 track_turn 上下文内（如离线脚本）→ 静默跳过，不报错"""
    assert current_usage() is None
    LlmUsageHandler().on_llm_end(
        _FakeResult(generations=[[_FakeGeneration({"input_tokens": 1, "output_tokens": 1})]])
    )


def test_handler_accumulates_inside_turn() -> None:
    handler = LlmUsageHandler()
    with track_turn() as usage:
        handler.on_llm_end(
            _FakeResult(
                generations=[[_FakeGeneration({"input_tokens": 10, "output_tokens": 5})]]
            )
        )
        handler.on_llm_end(_FakeResult())  # 拿不到 usage 也要计一次调用
        handler.on_llm_error(RuntimeError("boom"))
    assert usage.llm_calls == 2
    assert usage.failed_calls == 1
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5


def test_start_end_turn_pairs_and_resets() -> None:
    usage, token = start_turn()
    assert current_usage() is usage
    end_turn(token)
    assert current_usage() is None


# ---------- 限流 ----------


class _FakeRedis:
    """假 Redis：只需 client 属性 + incr"""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.client = object()  # 非 None 即视为可用

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int:  # noqa: ARG002
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


class _BrokenRedis(_FakeRedis):
    async def incr(self, key: str, ttl_seconds: int | None = None):  # noqa: ANN201, ARG002
        raise ConnectionError("redis down")


class _RedisDown:
    client = None


async def test_quota_allows_until_limit(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(budget, "redis_service", fake)
    service = QuotaService(per_user=2, per_session=0)

    assert (await service.consume("u1")).allowed
    assert (await service.consume("u1")).allowed
    blocked = await service.consume("u1")
    assert blocked.allowed is False
    assert blocked.scope == "user"
    assert blocked.used == 3 and blocked.limit == 2


async def test_quota_is_per_user(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(budget, "redis_service", fake)
    service = QuotaService(per_user=1, per_session=0)
    assert (await service.consume("u1")).allowed
    assert (await service.consume("u2")).allowed  # 换个用户不受影响


async def test_quota_session_scope(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(budget, "redis_service", fake)
    service = QuotaService(per_user=0, per_session=1)
    assert (await service.consume("u1", "s1")).allowed
    blocked = await service.consume("u1", "s1")
    assert blocked.allowed is False and blocked.scope == "session"


async def test_quota_disabled_when_zero(monkeypatch) -> None:
    """上限为 0 = 不限制，且不应碰 Redis"""
    fake = _FakeRedis()
    monkeypatch.setattr(budget, "redis_service", fake)
    service = QuotaService(per_user=0, per_session=0)
    for _ in range(5):
        assert (await service.consume("u1")).allowed
    assert fake.counters == {}


async def test_quota_fail_open_when_redis_unavailable(monkeypatch) -> None:
    """Redis 不可用时放行：限流不该成为新的单点故障"""
    monkeypatch.setattr(budget, "redis_service", _RedisDown())
    service = QuotaService(per_user=1, per_session=1)
    assert (await service.consume("u1", "s1")).allowed


async def test_quota_fail_open_on_redis_error(monkeypatch) -> None:
    monkeypatch.setattr(budget, "redis_service", _BrokenRedis())
    service = QuotaService(per_user=1, per_session=1)
    assert (await service.consume("u1", "s1")).allowed
