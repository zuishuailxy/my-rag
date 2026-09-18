"""对话记忆工具（对应 TS ai/chat-memory.util.ts，逐函数转写）

四个纯函数：不查库、不依赖 Redis，便于单测与在检索改写器里复用。
  - message_text             取消息正文（LangChain content 可能是 str 或块列表）
  - is_working_message       只有 human / ai 两类算「有效记忆」
  - db_rows_to_messages      kh_ai_message 行 → LangChain 消息
  - compact_rewrite_context  检索改写器用的紧凑上下文（助手只留短摘要）

前两个是短期记忆窗口的过滤口径：Redis 与 Postgres 两条来源都用同一套判定，
保证「回填」与「热窗口」内容一致（否则回填一次会把检索资料/思考块灌进窗口）。

刻意差异（相对 TS）：
  - `compact_rewrite_context` 的截断用 Python 码点切片（`text[:200]`），TS 的
    `slice(0, 200)` 按 UTF-16 码元切 —— 对中文两者等价，差别只在 emoji 等
    代理对字符上（Python 不会把字符切成半个）。语义按 TS 意图对齐：限制长度、
    避免制度原文污染 query。
"""

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.models.ai_session import MESSAGE_ROLE_ASSISTANT, MESSAGE_ROLE_USER, AiMessage

__all__ = [
    "compact_rewrite_context",
    "db_rows_to_messages",
    "is_working_message",
    "message_text",
]

# 检索改写上下文的截断长度（对齐 TS 的 200 / 120 字面量）
_REWRITE_USER_MAX_CHARS = 200
_REWRITE_ASSISTANT_MAX_CHARS = 120
# 改写只回看最近几轮（对齐 TS `history.slice(-4)`）
_REWRITE_HISTORY_WINDOW = 4


def message_text(message: BaseMessage) -> str:
    """取消息正文纯文本

    对应 TS messageText：content 既可能是 str，也可能是块列表
    （OpenAI/DeepSeek 风格的多模态 part），块列表时只取每块的 text 字段再拼接。
    取不到内容返回空串（调用方据此跳过该条）。
    """
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        # TS `String(content ?? '').trim()`：None → 空串
        return str(content if content is not None else "").strip()

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and "text" in part:
            value = part.get("text")
            parts.append(str(value if value is not None else ""))
    return "".join(parts).strip()


def is_working_message(message: BaseMessage) -> bool:
    """是否属于「有效记忆」：只有 human / ai 两类

    对应 TS `HumanMessage.isInstance(m) || AIMessage.isInstance(m)`。
    刻意排除 system（Mem0 系统消息）与 tool（检索工具调用结果）——
    这些是一次性上下文，进窗口会污染后续轮次。

    注：AIMessageChunk 是 AIMessage 的子类，此处同样算有效（与 TS `instanceof` 一致）；
    但从 Redis / 数据库恢复的消息不会是 chunk 类型。
    """
    return isinstance(message, (HumanMessage, AIMessage))


def db_rows_to_messages(rows: Sequence[AiMessage]) -> list[BaseMessage]:
    """kh_ai_message 行 → LangChain 消息（对应 TS dbRowsToMessages）

    只取 role = user / assistant 且正文非空的行；正文先 trim，全空白则跳过
    （对齐 TS `const text = row.content?.trim(); if (!text) continue;`）。
    其余 role（如将来新增的 system）一律忽略 —— 与 is_working_message 同口径。
    """
    out: list[BaseMessage] = []
    for row in rows:
        text = (row.content or "").strip()
        if not text:
            continue
        if row.role == MESSAGE_ROLE_USER:
            out.append(HumanMessage(text))
        elif row.role == MESSAGE_ROLE_ASSISTANT:
            out.append(AIMessage(text))
    return out


def compact_rewrite_context(history: Sequence[BaseMessage]) -> str:
    """给检索改写器用的紧凑上下文（对应 TS compactRewriteContext）

    只回看最近 `_REWRITE_HISTORY_WINDOW` 条，助手侧截断到更短——
    因为助手回答里常含制度原文，直接喂给改写器会让 query 被原文带偏。
    格式与 TS 一致：每条一行 `用户：…` / `助手：…`，用换行拼接。
    """
    if not history:
        return ""
    lines: list[str] = []
    for message in list(history)[-_REWRITE_HISTORY_WINDOW:]:
        text = message_text(message)
        if not text:
            continue
        if isinstance(message, HumanMessage):
            lines.append(f"用户：{text[:_REWRITE_USER_MAX_CHARS]}")
        elif isinstance(message, AIMessage):
            lines.append(f"助手：{text[:_REWRITE_ASSISTANT_MAX_CHARS]}")
    return "\n".join(lines)
