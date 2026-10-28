"""短期记忆的纯函数：紧凑上下文的窗口 / 单条截断 / 总字符预算。

这些断言是回归守卫：窗口从 4 条放宽到 12 条、并加总预算之后，
「指代对象在 4 轮之外」这种场景必须还能被改写器看到。
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


def test_total_budget_drops_oldest_whole_lines() -> None:
    # 12 条满额用户消息 = 12×(200+3+1) 字符，超预算 → 只装最新的若干条
    history = [HumanMessage(str(i) * 200) for i in range(12)]
    context = compact_rewrite_context(history)
    lines = context.split("\n")
    assert len(context) <= _REWRITE_CONTEXT_MAX_CHARS == 2000, "整段不得超过预算"
    assert 0 < len(lines) < 12, "长消息应触发丢弃，而不是全部保留"
    assert lines[0] == "用户：" + "3" * 200, "预算不够时保最新的、丢最老的"
    assert lines[-1] == "用户：" + ("11" * 200)[:200], "最新一条应在末尾"


def test_per_message_truncation() -> None:
    context = compact_rewrite_context([HumanMessage("长" * 500), AIMessage("答" * 500)])
    user_line, assistant_line = context.split("\n")
    assert len(user_line) == len("用户：") + 200, "用户侧截断到 200 字"
    assert len(assistant_line) == len("助手：") + 120, "助手侧截断到 120 字"


def test_long_answer_does_not_starve_other_messages() -> None:
    # 单条上限的独立价值：一条 3000 字长回答不能把 2000 预算吃光。
    # 实测过三种写法：只留总预算时，这条长回答会让整段上下文变空（或只剩它一条），
    # 前面的用户问题——往往才是指代来源——全被挤掉。
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
    assert "年假需要提前几天申请" in context, "长回答不应吃光预算"
    assert "那调休呢" in context, "最新一轮必须保留"
    assert len(context.split("\n")) == 12, "12 条应全部装下（单条已截断）"


def test_empty_and_blank_history() -> None:
    assert compact_rewrite_context([]) == ""
    assert compact_rewrite_context([HumanMessage("   ")]) == "", "空白正文应被跳过"
