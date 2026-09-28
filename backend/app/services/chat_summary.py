"""会话滚动摘要（Redis + Postgres 双写；滑出热窗口的旧历史增量折叠）

解决的问题：`ChatShortMemoryService` 的热窗口只保留最近 N 条 human/ai 原文，
更早的历史默认被丢弃（`load_working_history` 只取最近 N 条），AI 看不到。
本服务把**滑出窗口**的消息增量折叠进一段会话摘要，作答时作为 SystemMessage
前置，让 AI 仍能感知更早的上下文。

与其它记忆的分工：
  - 短期记忆（Redis 热窗口）：最近 N 条**原文**，逐字保真。
  - 本模块：窗口**之外**的旧历史，压成一段摘要（有损）。
  - Mem0 session 层：从对话里抽出的**事实**（任务/待办/偏好），不是对话摘要。

存储（双写，与短期记忆同款「Redis 优先 / DB 兜底」）：
  Redis key `{PREFIX}:{userId}:{sessionId}:summary`
            值 JSON `{"summary": "...", "covered_until_message_id": 123}`
  Postgres `kh_ai_session_summary`（独立表，删会话 CASCADE 自动清）

折叠水位 `covered_until_message_id`：已折叠进摘要的**最大消息 id**。
每次只折叠 `id > 水位 且 id < 热窗口最旧消息 id` 的消息。
⚠️ 用 id 而非计数（删除/补写会让计数漂移）的前提是「同会话内后写入的消息 id 更大」。
雪花 id 在本项目**单进程内严格递增**（41 位时间戳在高位 + 同毫秒 12 位序列号递增；
时钟回拨直接 raise 而非生成更小 id）——见 common/snowflake.py。多进程 / 多 worker
部署时该前提不保证（machine_id 硬编码为 1，既可能撞 id 也无法保证全局有序）。

调用方式（ai_stream）：
  作答前 `try_load` → `build_system_message` 前置进 messages；
  落库后 `_spawn(fold(...))` 后台增量折叠（fire-and-forget，失败只 WARN）。

未实现：多 worker 部署时同一会话的折叠需要 Redis 分布式锁（当前是进程内 asyncio 锁）。
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.core.config import settings
from app.services.llm_budget import usage_callbacks
from app.db.redis import redis_service
from app.db.session import async_session_factory
from app.models.ai_session import MESSAGE_ROLE_ASSISTANT, MESSAGE_ROLE_USER, AiMessage
from app.models.ai_session_summary import AiSessionSummary

logger = logging.getLogger("ChatSessionSummaryService")

__all__ = [
    "ChatSessionSummaryService",
    "SessionSummary",
    "chat_session_summary_service",
]


@dataclass
class SessionSummary:
    """一段会话摘要 + 折叠水位（对应 kh_ai_session_summary 一行）"""

    summary: str
    covered_until_message_id: int


def _build_summary_llm() -> ChatOpenAI | None:
    """构建摘要用 LLM；未配置 API Key 返回 None（此时折叠跳过、摘要仍可读）

    Key / baseURL / model fallback 链与 ai_chat 一致；temperature 0 + 关 thinking——
    摘要要可复现、要快，思考内容对摘要无益还拉长耗时。
    """
    api_key = (
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    if not api_key:
        return None

    base_url = settings.OPENAI_BASE_URL or settings.LLM_BASE_URL
    model = settings.MODEL_NAME or settings.LLM_MODEL
    timeout_ms = settings.AI_CHAT_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0

    return ChatOpenAI(
        api_key=SecretStr(api_key),
        model=model,
        temperature=0,
        timeout=timeout_s,
        max_retries=0,
        # 用量/成本统计：挂一次即覆盖该 LLM 的所有调用点
        callbacks=usage_callbacks(),
        # DeepSeek 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
        base_url=base_url,
        # deepseek-v4-flash 默认 thinking 模式，摘要类调用关闭
        extra_body={"thinking": {"type": "disabled"}},
    )


def _fold_system_prompt(max_chars: int) -> str:
    """增量折叠的 system 提示词：合并旧摘要 + 新滑出的对话"""
    return (
        "你在维护一段持续更新的会话摘要，供后续对话使用。\n"
        "把「新增对话」合并进「已有摘要」，输出更新后的摘要。\n"
        "\n"
        "## 要求\n"
        "- 保留：话题、已确认结论、待办、关键实体（人名 / 系统名 / 单据号）\n"
        "- 丢弃：寒暄、重复、助手复述的制度原文与检索资料\n"
        "- 不要编造未出现的信息\n"
        "- 只输出摘要正文，不要解释\n"
        f"- 不超过 {max_chars} 字"
    )


class ChatSessionSummaryService:
    """会话滚动摘要：Redis + Postgres 双写，滑出窗口的旧历史增量折叠"""

    def __init__(self) -> None:
        self._max_chars = settings.CHAT_SUMMARY_MAX_CHARS
        # 摘要比热窗口贵（丢了要重新折叠），复用短期记忆的 TTL；DB 才是持久层
        self._ttl = settings.CHAT_SHORT_MEMORY_TTL_SECONDS
        self._key_prefix = settings.CHAT_SHORT_MEMORY_KEY_PREFIX
        self._llm = _build_summary_llm()
        # 每会话一把进程内锁：折叠是后台 fire-and-forget，连轮到达时避免同一会话重复折叠。
        # 单进程 uvicorn 足够；多 worker 部署需换 Redis 分布式锁（见模块注释「未实现」）。
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        """是否可折叠（未配 LLM Key 时为 False；此时摘要只读不写）"""
        return self._llm is not None

    def build_system_message(
        self, state: SessionSummary | None
    ) -> SystemMessage | None:
        """把摘要拼成一条 SystemMessage；空摘要返回 None（调用方据此跳过注入）"""
        if state is None or not state.summary.strip():
            return None
        return SystemMessage(
            f"【早前对话摘要】\n{state.summary}\n\n"
            "以上摘要仅作背景，细节以本轮检索资料为准。"
        )

    async def try_load(
        self, user_id: str, session_id: str
    ) -> SessionSummary | None:
        """读摘要：Redis 优先，miss / 失败回落到 Postgres；两边都没有返回 None

        与短期记忆一致：返回 None 表示「没有摘要」，不表示「去重算」。
        任何异常只 WARN（摘要不可用不能阻断对话）。
        """
        key = self._key(user_id, session_id)
        try:
            raw = await redis_service.get(key)
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return SessionSummary(
                        summary=str(parsed.get("summary") or ""),
                        covered_until_message_id=int(
                            parsed.get("covered_until_message_id") or 0
                        ),
                    )
        except Exception as err: # noqa: BLE001
            logger.warning("会话摘要 Redis 读取失败，改走数据库：%s", err)

        try:
            async with async_session_factory() as db:
                row = await db.get(AiSessionSummary, int(session_id))
                if row is None:
                    return None
                return SessionSummary(
                    summary=row.summary or "",
                    covered_until_message_id=int(row.covered_until_message_id or 0),
                )
        except Exception as err: # noqa: BLE001
            logger.warning("会话摘要读取失败：%s", err)
            return None

    async def save(
        self, user_id: str, session_id: str, state: SessionSummary
    ) -> None:
        """双写：Redis（带 TTL）+ Postgres（upsert）。任一侧失败只 WARN。"""
        payload = json.dumps(
            {
                "summary": state.summary,
                "covered_until_message_id": state.covered_until_message_id,
            },
            ensure_ascii=False,
        )
        try:
            await redis_service.set(self._key(user_id, session_id), payload, self._ttl)
        except Exception as err: # noqa: BLE001
            logger.warning("会话摘要 Redis 写入失败：%s", err)

        try:
            async with async_session_factory() as db:
                row = await db.get(AiSessionSummary, int(session_id))
                if row is None:
                    row = AiSessionSummary(session_id=int(session_id))
                    db.add(row)
                row.summary = state.summary
                row.covered_until_message_id = state.covered_until_message_id
                await db.commit()
        except Exception as err: # noqa: BLE001
            logger.warning("会话摘要落库失败：%s", err)

    async def clear(self, user_id: str, session_id: str) -> None:
        """删除会话摘要（会话删除时调用；Redis 与 DB 都清，失败只 WARN）"""
        try:
            await redis_service.delete(self._key(user_id, session_id))
        except Exception as err: # noqa: BLE001
            logger.warning("会话摘要 Redis 删除失败：%s", err)
        try:
            async with async_session_factory() as db:
                await db.execute(
                    sa_delete(AiSessionSummary).where(
                        AiSessionSummary.session_id == int(session_id)
                    )
                )
                await db.commit()
        except Exception as err: # noqa: BLE001
            logger.warning("会话摘要删除失败：%s", err)

    async def fold(self, user_id: str, session_id: str, window_size: int) -> None:
        """把滑出热窗口、且尚未折叠的消息增量并入摘要

        由 ai_stream 落库后 fire-and-forget 调用。同会话用进程内锁串行，
        避免连续请求的两个后台折叠任务重复折叠同一批消息。失败只 WARN。
        """
        if self._llm is None:
            return
        lock = self._locks.setdefault(str(session_id), asyncio.Lock())
        async with lock:
            try:
                await self._fold_locked(user_id, session_id, window_size)
            except Exception as err: # noqa: BLE001
                logger.warning(
                    "会话摘要折叠失败：sessionId=%s, err=%s", session_id, err
                )

    async def _fold_locked(
        self, user_id: str, session_id: str, window_size: int
    ) -> None:
        state = await self.try_load(user_id, session_id) or SessionSummary("", 0)
        sid = int(session_id)
        limit = max(window_size, 1)

        async with async_session_factory() as db:
            # 热窗口 = 最近 limit 条（只算 human/ai，与短期记忆同口径）
            stmt = (
                select(AiMessage)
                .where(
                    AiMessage.session_id == sid,
                    AiMessage.role.in_((MESSAGE_ROLE_USER, MESSAGE_ROLE_ASSISTANT)),
                )
                .order_by(AiMessage.id.desc())
                .limit(limit)
            )
            window = list((await db.scalars(stmt)).all())
            if len(window) < limit:
                return # 消息还没超过窗口，没有东西可折叠
            # desc 的最后一个 = 窗口最旧；比它更旧的才是「滑出窗口」的
            window_start_id = window[-1].id
            if window_start_id <= state.covered_until_message_id:
                return # 没有新的滑出（已折叠到窗口边界）

            older_stmt = (
                select(AiMessage)
                .where(
                    AiMessage.session_id == sid,
                    AiMessage.role.in_((MESSAGE_ROLE_USER, MESSAGE_ROLE_ASSISTANT)),
                    AiMessage.id > state.covered_until_message_id,
                    AiMessage.id < window_start_id,
                )
                .order_by(AiMessage.id.asc())
            )
            older = list((await db.scalars(older_stmt)).all())
            if not older:
                return

            new_summary = await self._summarize(state.summary, older)
            new_covered = older[-1].id

        await self.save(user_id, session_id, SessionSummary(new_summary, new_covered))
        logger.info(
            "会话摘要已折叠：sessionId=%s, n=%d, coveredUntil=%s, chars=%d",
            session_id,
            len(older),
            new_covered,
            len(new_summary),
        )

    async def _summarize(
        self, old_summary: str, messages: list[AiMessage]
    ) -> str:
        assert self._llm is not None # 调用方 fold 已判空
        transcript = "\n".join(
            f"{'用户' if m.role == MESSAGE_ROLE_USER else '助手'}：{(m.content or '').strip()}"
            for m in messages
        )
        prompt = (
            f"已有摘要：\n{old_summary.strip() or '（无）'}\n\n"
            f"新增对话：\n{transcript}\n\n"
            "请输出更新后的摘要。"
        )
        resp = await self._llm.ainvoke(
            [
                SystemMessage(_fold_system_prompt(self._max_chars)),
                HumanMessage(prompt),
            ]
        )
        text = (resp.content if isinstance(resp.content, str) else str(resp.content)).strip()
        if len(text) > self._max_chars:
            # 兜底硬截断：prompt 已限字数，模型偶尔超长；不截断会让摘要无界增长
            logger.warning(
                "会话摘要超长（%d > %d），硬截断", len(text), self._max_chars
            )
            text = f"{text[: self._max_chars]}…"
        return text

    def _key(self, user_id: str, session_id: str) -> str:
        """摘要 key（userId / sessionId 一律 str 归一，防 int/str 两种形态长出两个 key）"""
        return f"{self._key_prefix}:{user_id!s}:{session_id!s}:summary"


# 全局单例
chat_session_summary_service = ChatSessionSummaryService()
