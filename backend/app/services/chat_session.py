"""AI 会话服务（对应 TS ai/chat-session.service.ts，逐段转写）

会话 CRUD 与问答落库。列表按 updated_at 倒序（最近对话置顶）；
消息按 created_at / id 升序（对齐 TS order: { createdAt: 'ASC', id: 'ASC' }）。

删除顺序与 TS 保真：先删消息再删会话——DDL 的 session_id 外键虽有
ON DELETE CASCADE，但 TS 是显式两步删除，不依赖库级级联。

刻意差异：
- append_turn 里显式赋 updated_at = func.now()：标题不变且无字段变更时
  SQLAlchemy 不发 UPDATE，updated_at 会停在上轮——TS 靠 new Date() 强制
  save() 触发更新，这里用 SQL 表达式赋值达到同样效果（每次问答必刷新）。
- _get_owned 非法 / 不存在的会话 id 统一 404「会话不存在」（parse_bigint
  语义，路径参数非法值 404；TS 靠 bigint 查不到自然 404，行为一致）。
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
from app.schemas.ai import CreateSessionRequest, QuerySessionRequest, UpdateSessionRequest

logger = logging.getLogger("ChatSessionService")

# 默认标题（对齐 TS DEFAULT_TITLE）
DEFAULT_TITLE = "新对话"


def title_from_question(question: str) -> str:
    """首问 → 会话标题：压缩空白，30 字截断加省略号（对齐 TS titleFromQuestion）"""
    text = re.sub(r"\s+", " ", question).strip()
    if not text:
        return DEFAULT_TITLE
    return f"{text[:30]}…" if len(text) > 30 else text


class ChatSessionService:
    """AI 会话：分页 / 新建 / 改名 / 删除 / 消息列表 / 问答落库"""

    async def page_mine(
        self, session: AsyncSession, user_id: str, query: QuerySessionRequest
    ) -> dict:
        """本人会话分页，updated_at 倒序（对应 TS pageMine）"""
        page = query.page
        page_size = query.pageSize
        cond = AiSession.user_id == int(user_id)
        total = await session.scalar(select(func.count()).select_from(AiSession).where(cond))
        stmt = (
            select(AiSession)
            .where(cond)
            .order_by(AiSession.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.scalars(stmt)).all()
        return {"items": list(rows), "total": int(total or 0), "page": page, "pageSize": page_size}

    async def create(
        self, session: AsyncSession, user_id: str, dto: CreateSessionRequest
    ) -> AiSession:
        """新建会话：标题 trim 后取前 80 字，缺省用默认标题（对应 TS create）"""
        title = (dto.title or "").strip() or DEFAULT_TITLE
        entity = AiSession(id=next_snowflake_id(), user_id=int(user_id), title=title[:80])
        session.add(entity)
        await session.commit()
        # server_default 的 created_at / updated_at 需 refresh 后可读
        await session.refresh(entity)
        logger.info("AI 会话已创建：sessionId=%s, userId=%s", entity.id, user_id)
        return entity

    async def rename(
        self, session: AsyncSession, user_id: str, session_id: str, dto: UpdateSessionRequest
    ) -> AiSession:
        """会话改名：trim + 截断 80 字（对应 TS rename）"""
        entity = await self._get_owned(session, user_id, session_id)
        entity.title = dto.title.strip()[:80]
        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def remove(self, session: AsyncSession, user_id: str, session_id: str) -> dict:
        """删除会话：先删消息再删会话，返回 { message: '已删除' }（对应 TS remove）"""
        entity = await self._get_owned(session, user_id, session_id)
        await session.execute(sa_delete(AiMessage).where(AiMessage.session_id == entity.id))
        await session.execute(sa_delete(AiSession).where(AiSession.id == entity.id))
        await session.commit()
        logger.info("AI 会话已删除：sessionId=%s, userId=%s", session_id, user_id)
        return {"message": "已删除"}

    async def list_messages(
        self, session: AsyncSession, user_id: str, session_id: str
    ) -> list[AiMessage]:
        """会话消息列表：created_at / id 升序（对应 TS listMessages）"""
        entity = await self._get_owned(session, user_id, session_id)
        stmt = (
            select(AiMessage)
            .where(AiMessage.session_id == entity.id)
            .order_by(AiMessage.created_at.asc(), AiMessage.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def append_turn(
        self,
        session: AsyncSession,
        user_id: str,
        session_id: str | None,
        question: str,
        answer: str,
        sources: list[dict] | None,
    ) -> AiSession:
        """问答落库：无 sessionId 则新建；标题仍为默认名时用首问覆盖（对应 TS appendTurn）"""
        if session_id:
            entity = await self._get_owned(session, user_id, session_id)
        else:
            entity = await self.create(
                session, user_id, CreateSessionRequest(title=title_from_question(question))
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
            # sources 为空落 NULL（对齐 TS sources.length ? sources : null）
            sources=sources or None,
        )
        session.add_all([user_msg, assistant_msg])
        await session.commit()
        return entity

    async def _get_owned(
        self, session: AsyncSession, user_id: str, session_id: str
    ) -> AiSession:
        """按 id + userId 双条件查本人会话；非法 / 不存在 / 非本人统一 404（对应 TS getOwned）"""
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
