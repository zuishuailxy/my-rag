"""
LLM 成本核算与限流

背景：项目里所有 LLM 调用（agent 主循环、检索改写、切题判定、长期记忆分类、
会话摘要、KG 抽取…）此前既没有用量统计、也没有预算控制。实测中因为账号欠费被
中断两次，才意识到缺这一层 —— 这是「先有可观测，才谈得上控制」。

本模块提供两件事：

1. **成本核算**：在 `track_turn()` 上下文内挂 `LlmUsageHandler`，把这一轮里的
   每一次 LLM 调用（含工具内部的子调用，LangChain 会把 callbacks 透传下去）
   累加到同一个 `TurnUsage`，轮次结束打一条结构化日志：
   `llmCalls / promptTokens / completionTokens / totalTokens / estimatedCostYuan`。
2. **限流**：`QuotaService` 用 Redis 做「每用户 / 每会话每日轮次」计数，
   超限直接拒绝（非流式 HTTP 429、流式由路由抛 429）。

设计取舍：

- **token 数取真实值，金额是估算**：单价写在 config，日志字段名带 `estimated`，
  只用于「发现异常用量」，不作为账单口径。
- **按「轮」限而不是按 token 限**：用户能理解、实现简单，也不会因为一次长上下文
  就把正常用户误杀。
- **Redis 不可用时 fail-open**：限流是保护措施，不该因为它自己挂掉就把所有人挡在门外
  （与项目里 ES / Neo4j 的降级风格一致）：放行 + WARN。
- **计数在放行前自增**：宁可多算一次（保守），也不放过多调一次。
- **背景任务不计入本轮**：长期记忆 / 摘要折叠是 fire-and-forget，在 `track_turn()`
  退出后才发起，故意不串进本轮用量（否则日志会等它们跑完、也没了「本轮」的语义）。
"""
import functools
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import settings
from app.db.redis import redis_service

logger = logging.getLogger("LlmBudget")

# 配额计数 key 前缀；窗口 24h + 1h 缓冲（跨时区/时钟漂移下不会提前过期）
_QUOTA_KEY_PREFIX = "kh:llm:turns"
_QUOTA_WINDOW_SECONDS = 25 * 3600


@dataclass
class TurnUsage:
    """一轮问答内的 LLM 用量（agent 主循环 + 工具内子调用累加）"""

    llm_calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.llm_calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        """估算成本（元）；单价见 `AI_PRICE_*_PER_MTOK`"""
        return (
            self.prompt_tokens * settings.AI_PRICE_INPUT_PER_MTOK
            + self.completion_tokens * settings.AI_PRICE_OUTPUT_PER_MTOK
        ) / 1_000_000

    def as_log_fields(self) -> dict[str, Any]:
        """结构化日志字段（金额字段显式标 estimated）"""
        return {
            "llmCalls": self.llm_calls,
            "failedCalls": self.failed_calls,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "estimatedCostYuan": round(self.cost, 6),
        }


# 当前轮次的用量累加器。用 ContextVar 而不是显式传参：
# agent 主循环 → 工具 → 改写/切题 LLM 的调用链很深，靠参数传递会污染一长串签名。
_TURN_USAGE: ContextVar[TurnUsage | None] = ContextVar("llm_turn_usage", default=None)


def current_usage() -> TurnUsage | None:
    """当前轮次的累加器；不在 `track_turn()` 上下文内时为 None"""
    return _TURN_USAGE.get()


@contextmanager
def track_turn() -> Iterator[TurnUsage]:
    """开启一轮用量统计（应包住 agent 调用 + 子 LLM 调用）"""
    usage = TurnUsage()
    token = _TURN_USAGE.set(usage)
    try:
        yield usage
    finally:
        _TURN_USAGE.reset(token)


def start_turn() -> tuple[TurnUsage, Token]:
    """手动开启一轮统计，返回 (累加器, 复原令牌)

    给「没法用 `with` 包」的场景用（如异步生成器：with 要包住整个 yield 循环，
    会带来大段缩进改动）。必须配对调用 `end_turn(token)`。
    """
    usage = TurnUsage()
    return usage, _TURN_USAGE.set(usage)


def end_turn(token: Token) -> None:
    """结束一轮统计（还原 ContextVar）"""
    _TURN_USAGE.reset(token)


def extract_tokens(result: Any) -> tuple[int, int] | None:
    """从 LangChain `LLMResult` 里取 (prompt_tokens, completion_tokens)

    兼容两种形态（不同 langchain 版本 / 不同模型提供方不一致）：
      1. 新版：`generations[].message.usage_metadata`（input_tokens/output_tokens）
      2. 旧版：`llm_output.token_usage`（prompt_tokens/completion_tokens）
    取不到返回 None（不猜、不按字数估 —— 估出来的数字会污染成本口径）。
    """
    for generation_list in getattr(result, "generations", None) or []:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "usage_metadata", None)
            if metadata:
                return (
                    int(metadata.get("input_tokens") or 0),
                    int(metadata.get("output_tokens") or 0),
                )

    llm_output = getattr(result, "llm_output", None) or {}
    token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if token_usage:
        return (
            int(token_usage.get("prompt_tokens") or 0),
            int(token_usage.get("completion_tokens") or 0),
        )
    return None


class LlmUsageHandler(BaseCallbackHandler):
    """把每次 LLM 调用的 token 用量累加到当前轮次

    挂在 agent / LLM 的 `config={"callbacks": [...]}` 上即可；
    LangChain 会把 callbacks 透传给工具内部的子调用（改写、切题判定等），
    所以一个 handler 就能覆盖整轮。
    """

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ARG002
        usage = current_usage()
        if usage is None:
            return
        tokens = extract_tokens(response)
        if tokens is None:
            # 拿不到 usage 也要计数：否则「调用次数」会低估
            usage.llm_calls += 1
            return
        usage.add(*tokens)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:  # noqa: ARG002
        usage = current_usage()
        if usage is not None:
            usage.failed_calls += 1


def track_llm_turn(label: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """给「一整轮对话」加用量统计（协程形态）

    比在方法体里写 `with` 更好：不用为一层缩进重排整个方法体，且天然覆盖
    检索阶段的子 LLM 调用（改写 / 切题判定）—— 它们和作答同属这一轮。

    ⚠️ **不适用于异步生成器**（如流式对话）：装饰器在返回生成器时就结束了统计，
    生成器内部还没开始迭代。流式场景请用 `start_turn()` / `end_turn()` 手动包。
    """

    def decorator(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            usage, token = start_turn()
            try:
                return await fn(*args, **kwargs)
            finally:
                end_turn(token)
                logger.info("%s LLM 用量：%s", label, usage.as_log_fields())

        return wrapper

    return decorator


# Langfuse trace handler：懒加载 + 全局复用
# - 懒加载：SDK 未装 / 未配 key 时零开销，CI 不受影响
# - 复用同一实例：handler 设计上可跨 run 复用（LangChain 按 run_id 区分），
#   而 `_build_llm` 是**每个请求**都调一次，不复用会每轮新建 10+ 个客户端
_LANGFUSE_HANDLER: BaseCallbackHandler | None = None
_LANGFUSE_RESOLVED = False


def _langfuse_handler() -> BaseCallbackHandler | None:
    """自托管 Langfuse 的 LangChain 回调（未配置 / 未安装 → None）

    刻意**不放在关键路径**：只旁路上报 trace，Langfuse 挂了不影响任何一次对话
    （只丢观测数据）。这与限流的分工是本质区别 —— 限流必须同步、必须在关键路径。
    """
    global _LANGFUSE_HANDLER, _LANGFUSE_RESOLVED  # noqa: PLW0603
    if _LANGFUSE_RESOLVED:
        return _LANGFUSE_HANDLER
    _LANGFUSE_RESOLVED = True

    if settings.LANGFUSE_TRACING == "false":
        return None
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        return None

    try:
        from langfuse.langchain import CallbackHandler  # noqa: PLC0415
    except ImportError:
        logger.warning("未安装 langfuse SDK，跳过 trace 上报")
        return None

    try:
        _LANGFUSE_HANDLER = CallbackHandler()
        logger.info("Langfuse trace 已启用：host=%s", settings.LANGFUSE_HOST)
    except Exception as err:  # noqa: BLE001 — 观测层失败不能影响对话
        logger.warning("Langfuse handler 初始化失败（不影响对话）：%s", err)
    return _LANGFUSE_HANDLER


def usage_callbacks() -> list[BaseCallbackHandler]:
    """给 LLM **构造器**用：挂上即自动统计与上报，无需逐个改 invoke 调用点

    为什么挂构造器而不是 invoke：LLM 构造点只有 8 个（agent 主循环 / 改写 / 切题 /
    长期记忆 / 摘要 / KG 抽取 / 图像 OCR / 评测判分），而 invoke 调用点更多且会持续
    新增 —— 挂构造器是「加一次、全覆盖」，不会随新代码漏统。

    这里同时是**可观测回调的单一注册点**：
      - `LlmUsageHandler`：本地 token / 成本累加（同步、无外部依赖、CI 可测）
      - Langfuse handler：trace 上报（异步旁路，未配置则不加载）

    本地那套与 Langfuse 是互补而非替代：Langfuse 挂了前者照常工作，
    前者也只有它能在不连外部服务的情况下断言成本。
    """
    callbacks: list[BaseCallbackHandler] = [LlmUsageHandler()]
    trace_handler = _langfuse_handler()
    if trace_handler is not None:
        callbacks.append(trace_handler)
    return callbacks


@dataclass
class QuotaDecision:
    """限流判定结果"""

    allowed: bool
    reason: str = ""
    scope: str = ""
    used: int = 0
    limit: int = 0


class QuotaService:
    """按用户 / 会话的每日轮次限流（Redis 计数，滚动 24h 窗口）"""

    def __init__(self, per_user: int | None = None, per_session: int | None = None) -> None:
        self._per_user = per_user
        self._per_session = per_session

    @property
    def per_user(self) -> int:
        return (
            settings.AI_DAILY_TURN_LIMIT_PER_USER
            if self._per_user is None
            else self._per_user
        )

    @property
    def per_session(self) -> int:
        return (
            settings.AI_DAILY_TURN_LIMIT_PER_SESSION
            if self._per_session is None
            else self._per_session
        )

    @staticmethod
    def _today() -> str:
        """按 UTC 日期分桶（跨集群部署时不会因本地时区不同而错位）"""
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def _key(self, scope: str, identifier: str) -> str:
        return f"{_QUOTA_KEY_PREFIX}:{scope}:{identifier}:{self._today()}"

    async def consume(
        self, user_id: str, session_id: str | None = None
    ) -> QuotaDecision:
        """记一次问答并判定是否超限

        先自增再比较：计数略偏保守，但不会因为竞态放过多调一次。
        Redis 不可用 / 报错时 fail-open 放行（限流不该成为新的单点故障）。
        """
        per_user = self.per_user
        per_session = self.per_session
        if per_user <= 0 and per_session <= 0:
            return QuotaDecision(allowed=True)

        if redis_service.client is None:
            logger.warning("Redis 不可用，跳过配额检查（fail-open）")
            return QuotaDecision(allowed=True)

        try:
            if per_user > 0:
                used = await redis_service.incr(
                    self._key("user", user_id), _QUOTA_WINDOW_SECONDS
                )
                if used > per_user:
                    return QuotaDecision(
                        allowed=False,
                        reason=f"今日问答次数已达上限（{per_user} 次），请明天再试",
                        scope="user",
                        used=used,
                        limit=per_user,
                    )
            if per_session > 0 and session_id:
                used = await redis_service.incr(
                    self._key("session", session_id), _QUOTA_WINDOW_SECONDS
                )
                if used > per_session:
                    return QuotaDecision(
                        allowed=False,
                        reason=f"本会话今日问答次数已达上限（{per_session} 次）",
                        scope="session",
                        used=used,
                        limit=per_session,
                    )
        except Exception as err:  # noqa: BLE001 — 限流失败不能阻断对话
            logger.warning("配额检查失败，放行（fail-open）：%s", err)
            return QuotaDecision(allowed=True)

        return QuotaDecision(allowed=True)


# 全局单例
quota_service = QuotaService()
