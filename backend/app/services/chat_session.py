"""AI 会话服务

会话 CRUD 与问答落库。列表按 updated_at 倒序（最近对话置顶）；
消息按 created_at / id 升序。

：先删消息再删会话——DDL 的 session_id 外键虽有
ON DELETE CASCADE，，不依赖库级级联。

缓存联动：
- `list_recent_messages` 只查库，供调用方在 Redis 热窗口 miss 时回填；它**自己
  不写 Redis**——写窗口是 ChatShortMemoryService 的职责，两处各自实现过滤口径
  迟早会漂移（「只存 human/ai 原文」的判定集中在 chat_memory.py）。
- `remove` 除删库外还要清该会话的短期记忆（Redis 热窗口）与长期记忆会话层
  （Mem0 session 层）：只删库不清缓存，会话内容会在 TTL 内继续被当作「历史
  记忆」召回，表现为「删了的会话还在影响回答」。
  两处清理失败只 WARN，不影响删除结果（缓存不可用不能阻断删除）。
"""

import logging
import re

from fastapi import HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint
from app.common.snowflake import next_snowflake_id
from app.models.ai_session import (
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    AiMessage,
    AiSession,
)
from app.schemas.ai import (
    CreateSessionRequest,
    QuerySessionRequest,
    UpdateSessionRequest,
)
from app.services.chat_long_memory import chat_long_memory_service
from app.services.chat_short_memory import chat_short_memory_service
from app.services.chat_summary import chat_session_summary_service

logger = logging.getLogger("ChatSessionService")

# 默认标题
DEFAULT_TITLE = "新对话"


def title_from_question(question: str) -> str:
    """首问 → 会话标题：压缩空白，30 字截断加省略号"""
    text = re.sub(r"\s+", " ", question).strip()
    if not text:
        return DEFAULT_TITLE
    return f"{text[:30]}…" if len(text) > 30 else text


class ChatSessionService:
    """AI 会话：分页 / 新建 / 改名 / 删除 / 消息列表 / 问答落库"""

    async def page_mine(
        self, session: AsyncSession, user_id: str, query: QuerySessionRequest
    ) -> dict:
        """本人会话分页，updated_at 倒序"""
        page = query.page
        page_size = query.pageSize
        cond = AiSession.user_id == int(user_id)
        total = await session.scalar(
            select(func.count()).select_from(AiSession).where(cond)
        )
        stmt = (
            select(AiSession)
            .where(cond)
            .order_by(AiSession.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.scalars(stmt)).all()
        return {
            "items": list(rows),
            "total": int(total or 0),
            "page": page,
            "pageSize": page_size,
        }

    async def create(
        self, session: AsyncSession, user_id: str, dto: CreateSessionRequest
    ) -> AiSession:
        """新建会话：标题 trim 后取前 80 字，缺省用默认标题"""
        title = (dto.title or "").strip() or DEFAULT_TITLE
        entity = AiSession(
            id=next_snowflake_id(), user_id=int(user_id), title=title[:80]
        )
        session.add(entity)
        await session.commit()
        # server_default 的 created_at / updated_at 需 refresh 后可读
        await session.refresh(entity)
        logger.info("AI 会话已创建：sessionId=%s, userId=%s", entity.id, user_id)
        return entity

    async def rename(
        self,
        session: AsyncSession,
        user_id: str,
        session_id: str,
        dto: UpdateSessionRequest,
    ) -> AiSession:
        """会话改名：trim + 截断 80 字"""
        entity = await self._get_owned(session, user_id, session_id)
        entity.title = dto.title.strip()[:80]
        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def remove(
        self, session: AsyncSession, user_id: str, session_id: str
    ) -> dict:
        """删除会话：先删消息再删会话，最后清缓存，返回 { message: '已删除' }。清理顺序：库删除提交后再清 Redis / Mem0——
        先清缓存再删库的话，若删库失败就出现「缓存已清但数据还在」，
        虽不影响正确性（回落数据库）但会让窗口白丢一次。
        """
        entity = await self._get_owned(session, user_id, session_id)
        await session.execute(
            sa_delete(AiMessage).where(AiMessage.session_id == entity.id)
        )
        await session.execute(sa_delete(AiSession).where(AiSession.id == entity.id))
        await session.commit()
        # 缓存清理失败只 WARN（三个 service 内部已各自吞异常并记日志）
        await chat_short_memory_service.clear(user_id, session_id)
        await chat_long_memory_service.clear_session(user_id, session_id)
        # 滚动摘要：Redis + kh_ai_session_summary 两边都要清（DB 侧虽有 CASCADE，
        # 但这里显式删一遍，不依赖库级级联——与「先删消息再删会话」同一思路）
        await chat_session_summary_service.clear(user_id, session_id)
        logger.info("AI 会话已删除：sessionId=%s, userId=%s", session_id, user_id)
        return {"message": "已删除"}

    async def list_messages(
        self, session: AsyncSession, user_id: str, session_id: str
    ) -> list[AiMessage]:
        """会话消息列表：created_at / id 升序"""
        entity = await self._get_owned(session, user_id, session_id)
        stmt = (
            select(AiMessage)
            .where(AiMessage.session_id == entity.id)
            .order_by(AiMessage.created_at.asc(), AiMessage.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def list_recent_messages(
        self, session: AsyncSession, user_id: str, session_id: str, limit: int
    ) -> list[AiMessage]:
        """最近 N 条消息，**时间正序**返回

        用途：Redis 热窗口 miss 时，由调用方取这批行 → db_rows_to_messages →
        回填 ChatShortMemoryService。**只查库，不写缓存**（写窗口是短期记忆服务
        的职责，见模块注释「缓存联动」）。

        实现要点：
        - 先 `_get_owned`（本人校验，非本人 / 非法 id 404），再查消息。
        - SQL 用 created_at DESC, id DESC 取「最新的 N 条」，返回前 reverse 成
          正序——不能反过来用 ASC + limit（那取到的是最旧的 N 条）。这里必须
          走 SQL 侧 LIMIT 而不是取全量再切片：会话可能很长，全量拉进内存只为
          截 20 条是浪费。
        - limit 做 `max(limit, 1)` 兜底：
          limit<=0 时退化成 1 条，绝不退化成「不限量」。
          非 int 在这里直接是类型错误，不构造等价行为。
        """
        entity = await self._get_owned(session, user_id, session_id)
        stmt = (
            select(AiMessage)
            .where(AiMessage.session_id == entity.id)
            .order_by(AiMessage.created_at.desc(), AiMessage.id.desc())
            .limit(max(limit, 1))
        )
        rows = list((await session.scalars(stmt)).all())
        rows.reverse()
        return rows

    async def append_turn(
        self,
        session: AsyncSession,
        user_id: str,
        session_id: str | None,
        question: str,
        answer: str,
        sources: list[dict] | None,
    ) -> AiSession:
        """问答落库：无 sessionId 则新建；标题仍为默认名时用首问覆盖"""
        if session_id:
            entity = await self._get_owned(session, user_id, session_id)
        else:
            entity = await self.create(
                session,
                user_id,
                CreateSessionRequest(title=title_from_question(question)),
            )

        if entity.title == DEFAULT_TITLE:
            entity.title = title_from_question(question)
        # 显式刷新 updated_at（见模块注释「刻意差异」）
        entity.updated_at = func.now()
        session.add(entity)
        await session.commit()
        await session.refresh(entity)

        user_msg = AiMessage(
            id=next_snowflake_id(),
            session_id=entity.id,
            role=MESSAGE_ROLE_USER,
            content=question,
        )
        assistant_msg = AiMessage(
            id=next_snowflake_id(),
            session_id=entity.id,
            role=MESSAGE_ROLE_ASSISTANT,
            content=answer,
            # sources 为空落 NULL
            sources=sources or None,
        )
        session.add_all([user_msg, assistant_msg])
        await session.commit()
        return entity

    async def touch_title(
        self, session: AsyncSession, user_id: str, session_id: str, question: str
    ) -> AiSession:
        """仍为默认标题时用首问覆盖

        标题未变时不发 UPDATE（无实际变更就不落库），updated_at 不前进；
        流式版不在这里刷 updated_at，落库由 append_turn 统一处理。
        """
        entity = await self._get_owned(session, user_id, session_id)
        if entity.title == DEFAULT_TITLE:
            entity.title = title_from_question(question)
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
        return entity

    async def _get_owned(
        self, session: AsyncSession, user_id: str, session_id: str
    ) -> AiSession:
        """按 id + userId 双条件查本人会话；非法 / 不存在 / 非本人统一 404"""
        pk = parse_bigint(session_id)
        if pk is not None:
            stmt = select(AiSession).where(
                AiSession.id == pk, AiSession.user_id == int(user_id)
            )
            entity = (await session.scalars(stmt)).first()
            if entity is not None:
                return entity
        raise HTTPException(status_code=404, detail="会话不存在")


# 全局单例
chat_session_service = ChatSessionService()
