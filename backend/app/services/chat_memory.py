"""对话记忆工具

四个纯函数：不查库、不依赖 Redis，便于单测与在检索改写器里复用。
  - message_text 取消息正文（LangChain content 可能是 str 或块列表）
  - is_working_message 只有 human / ai 两类算「有效记忆」
  - db_rows_to_messages kh_ai_message 行 → LangChain 消息
  - compact_rewrite_context 检索改写器用的上下文（消息全文，不按条截断）

前两个是短期记忆窗口的过滤口径：Redis 与 Postgres 两条来源都用同一套判定，
保证「回填」与「热窗口」内容一致（否则回填一次会把检索资料/思考块灌进窗口）。
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

# 检索改写上下文的窗口
# 12 条 ≈ 6 轮问答。实测：指代对象在第一轮、中间隔 8 轮无关话题时，
# 4 条窗口（2 轮）会把指代对象挤出窗口 → 改写出「报销单需提前几天申请？」（错），
# 12 条能覆盖到。热窗口本身是 20 条，12 条不越界。
_REWRITE_HISTORY_WINDOW = 12
# 硬顶：只防「用户粘贴整份文档」这类极端输入撑爆上下文，不是日常压缩。
# 真实回答 p99 = 2305 字符、12 条全量约 2.8 万字符，正常对话远达不到这里。
_REWRITE_CONTEXT_MAX_CHARS = 30000


def message_text(message: BaseMessage) -> str:
    """取消息正文纯文本，也可能是块列表
    （OpenAI/DeepSeek 风格的多模态 part），块列表时只取每块的 text 字段再拼接。
    取不到内容返回空串（调用方据此跳过该条）。
    """
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        #(content ?? '').trim()`：None → 空串
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
    """是否属于「有效记忆」：只有 human / ai 两类) || AIMessage.isInstance(m)`。
    刻意排除 system（Mem0 系统消息）与 tool（检索工具调用结果）——
    这些是一次性上下文，进窗口会污染后续轮次。

    注：AIMessageChunk 是 AIMessage 的子类，此处同样算有效；
    但从 Redis / 数据库恢复的消息不会是 chunk 类型。
    """
    return isinstance(message, (HumanMessage, AIMessage))


def db_rows_to_messages(rows: Sequence[AiMessage]) -> list[BaseMessage]:
    """kh_ai_message 行 → LangChain 消息

    只取 role = user / assistant 且正文非空的行；正文先 trim，全空白则跳过
    （if (!text) continue;`）。
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
    """给检索改写器 / 意图路由用的上下文

    窗口：最近 `_REWRITE_HISTORY_WINDOW` 条（用户、助手各算一条），
    **消息正文全文进入**，不按条截断 —— 改写要消解的是「第 5 项」「末尾那条
    例外」这类指代，被截掉的那半句往往正好装着指代对象。实测：全文输入比
    截 120 字多带回了位置靠后的实体（「拒绝日志」→ 带出 ausearch -m avc），
    也没有出现条文细节污染检索词的情况。

    硬顶：整段不超过 `_REWRITE_CONTEXT_MAX_CHARS` 字符，**从最近往回累加**。
    只有单条自身就超硬顶（粘贴整份文档）时才会截该条，正常对话不触发。

    格式：每条一行 `用户：…` / `助手：…`，按时间顺序输出。
    """
    if not history:
        return ""
    lines: list[str] = []
    used = 0
    for message in reversed(list(history)[-_REWRITE_HISTORY_WINDOW:]):
        text = message_text(message)
        if not text:
            continue
        if isinstance(message, HumanMessage):
            prefix = "用户："
        elif isinstance(message, AIMessage):
            prefix = "助手："
        else:
            continue
        line = prefix + text
        cost = len(line) + 1  # +1：行间换行符
        if used + cost > _REWRITE_CONTEXT_MAX_CHARS:
            # 走到这里说明单条就顶到硬顶：截到剩余额度，保住最新话题一截，
            # 而不是整条丢掉让上下文变空
            room = _REWRITE_CONTEXT_MAX_CHARS - used - len(prefix) - 1
            if room <= 0:
                break
            line = prefix + text[:room]
            cost = len(line) + 1
        lines.append(line)
        used += cost
    lines.reverse()
    return "\n".join(lines)
