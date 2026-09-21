"""
AI 会话滚动摘要模型（PostgreSQL kh_ai_session_summary）

用途：Redis 热窗口只保留最近 N 条 human/ai 原文，更早的历史默认丢弃。
本表存每会话一段「早前对话摘要」，由 ChatSessionSummaryService 增量折叠
（滑出窗口的消息 → LLM 合并进摘要），作答时作为 SystemMessage 前置。

schema 真相源：init-scripts/postgresql/init.sql（末尾「AI 会话」段后的摘要表）。
⚠️ 刻意用**独立表**而不是给 kh_ai_session 加列：应用启动的 Base.metadata.create_all
只做 CREATE TABLE IF NOT EXISTS，**不会 ALTER 已有表**；独立表在存量库上能自动补建，
加列则要每台库手工 ALTER。

1:1 关系：session_id 既是主键又是外键，删会话 CASCADE 自动清摘要，
不需要像 Redis 那样在 remove() 里显式清理（但 Redis 侧仍要显式 clear）。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["AiSessionSummary"]


class AiSessionSummary(Base):
    """每会话一段滚动摘要（kh_ai_session_summary）"""

    __tablename__ = "kh_ai_session_summary"

    # 会话 ID（1:1；删会话级联删摘要）
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kh_ai_session.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )

    # 早前对话摘要（增量折叠，目标长度见 CHAT_SUMMARY_MAX_CHARS）
    summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )

    # 折叠水位：已折叠进 summary 的**最大消息 id**。
    # 下次只折叠 id > 该值 且 < 热窗口最旧消息 id 的消息 —— 依赖「同会话内后写入的
    # 消息 id 更大」这条前提（雪花 id 单进程内严格递增；多进程部署不保证，
    # 详见 services/chat_summary.py 模块注释）。用 id 而非计数可避免删除/补写导致漂移。
    covered_until_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    # 同时写 server_default 与 onupdate（DDL 无触发器，只写 default 时 UPDATE 不刷新）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
