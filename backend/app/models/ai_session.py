"""
AI 会话模型（PostgreSQL kh_ai_session / kh_ai_message）

对话持久化：一次会话（kh_ai_session）下挂多条消息（kh_ai_message），
消息随会话删除级联清理（DDL 已声明 ON DELETE CASCADE）。

schema 真相源：init-scripts/postgresql/init.sql（末尾「AI 会话」段）。
改字段 / 索引请先改那里的 DDL，再同步本文件，最后对存量库手动 ALTER。

⚠️ updated_at 必须同时写 server_default 与 onupdate：init.sql 无触发器，
now() 只在 INSERT 生效，只写 server_default 会导致 UPDATE 不刷新时间戳。
created_at 只有 server_default。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["AiSession", "AiMessage"]

# 消息角色
MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"


class AiSession(Base):
    """AI 会话（kh_ai_session）"""

    __tablename__ = "kh_ai_session"

    # 会话 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 所属用户 → kh_user.id（DDL 未建 FK，由应用层保证存在）
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 会话标题（80 字内）
    title: Mapped[str] = mapped_column(String(80), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # 与 DDL 的 (user_id, updated_at DESC) 保持一致（SQLAlchemy 里 DESC 需走 text）
    __table_args__ = (
        Index("idx_kh_ai_session_user_updated", "user_id", text("updated_at DESC")),
    )


class AiMessage(Base):
    """AI 会话消息（kh_ai_message）"""

    __tablename__ = "kh_ai_message"

    # 消息 ID（雪花，应用层生成）
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    # 所属会话 → kh_ai_session.id（删会话级联删消息）
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kh_ai_session.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 角色：user / assistant
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # 消息正文
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 引用来源（RAG 命中片段），结构由 service 层决定
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("idx_kh_ai_message_session_id", "session_id", "created_at"),)
