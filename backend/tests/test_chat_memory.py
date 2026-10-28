"""短期记忆的纯函数：紧凑上下文的窗口 / 全文输入 / 硬顶。

这些断言是回归守卫：
  - 窗口 12 条，「指代对象在 4 轮之外」必须还能被改写器看到
  - 消息全文进入，不按条截断（截断会丢掉位置靠后的指代对象）
  - 只有单条超硬顶（粘贴整份文档）才截，且不返回空串
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.services.chat_memory import (
    _REWRITE_CONTEXT_MAX_CHARS,
    _REWRITE_HISTORY_WINDOW,
    compact_rewrite_context,
)


def test_window_keeps_only_recent_messages() -> None:
    history = [
        HumanMessage(f"问{i}") if i % 2 == 0 else AIMessage(f"答{i}")
        for i in range(30)
    ]
    lines = compact_rewrite_context(history).split("\n")
    assert len(lines) == _REWRITE_HISTORY_WINDOW == 12, "窗口应为最近 12 条"
    assert lines[0] == "用户：问18", "窗口左端应是倒数第 12 条"
    assert lines[-1] == "助手：答29", "窗口右端应是最新一条"


def test_indirect_reference_beyond_four_turns_survives() -> None:
    # 指代对象在第 1 轮，中间隔 4 轮无关话题（共 11 条）：旧的 4 条窗口会丢掉它
    history: list = [
        HumanMessage("年假需要提前几天申请"),
        AIMessage("年假需提前 3 个工作日申请。"),
    ]
    for i in range(4):
        history.append(HumanMessage(f"无关话题{i}"))
        history.append(AIMessage(f"无关回答{i}"))
    history.append(HumanMessage("这个流程要多久"))

    context = compact_rewrite_context(history)
    assert "年假" in context, "4 轮之前的指代对象必须留在上下文里"
    assert context.rstrip().endswith("这个流程要多久"), "最新一轮必须在末尾"


def test_messages_are_not_truncated() -> None:
    # 全文进入：用户 500 字、助手 500 字都原样保留，不再截 200 / 120
    context = compact_rewrite_context([HumanMessage("长" * 500), AIMessage("答" * 500)])
    user_line, assistant_line = context.split("\n")
    assert user_line == "用户：" + "长" * 500, "用户消息应全文保留"
    assert assistant_line == "助手：" + "答" * 500, "助手消息应全文保留"


def test_hard_cap_truncates_oversized_single_message() -> None:
    # 单条就超过硬顶（用户粘贴整份文档）：截到硬顶保留开头，
    # 而不是整条丢弃 —— 整条丢弃会让上下文变空串，改写器直接不跑
    context = compact_rewrite_context([HumanMessage("长" * (_REWRITE_CONTEXT_MAX_CHARS + 5000))])
    assert len(context) <= _REWRITE_CONTEXT_MAX_CHARS, "不得超过硬顶"
    assert context.startswith("用户：长"), "应保留开头而不是丢弃整条"
    assert len(context) > _REWRITE_CONTEXT_MAX_CHARS - 10, "应尽量用满硬顶"


def test_long_answer_keeps_earlier_history() -> None:
    # 一条 3000 字长回答不该把前面的历史挤掉：硬顶足够宽，12 条全部保留
    history: list = [
        HumanMessage("年假需要提前几天申请"),
        AIMessage("年假需提前 3 个工作日申请。"),
    ]
    for i in range(4):
        history.append(HumanMessage(f"无关话题{i}"))
        history.append(AIMessage(f"无关回答{i}"))
    history.append(HumanMessage("那调休呢"))
    history.append(AIMessage("制度原文" * 750))

    context = compact_rewrite_context(history)
    assert "年假需要提前几天申请" in context, "长回答不应挤掉更早的用户提问"
    assert "那调休呢" in context, "最新一轮必须保留"
    assert len(context.split("\n")) == 12, "12 条应全部装下（单条不截断）"
    assert len(context) > 3000, "长回答应全文进入，而不是被截成 120 字"


def test_empty_and_blank_history() -> None:
    assert compact_rewrite_context([]) == ""
    assert compact_rewrite_context([HumanMessage("   ")]) == "", "空白正文应被跳过"
