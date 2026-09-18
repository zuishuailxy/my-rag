"""检索改写（追问 → 独立短查询）冒烟测试

验证点（对应 TS ai/chat-query-rewrite.service.ts + chat-memory.util.ts）：
1. 前提断言：`_rewriter` 是否存在必须与「是否配置 LLM Key」一致
   （否则后面的回退断言会把「配了 key 但调用失败」误读成「没配 key 的正常降级」而假绿）
2. 无历史 / 空上下文（历史全空白）→ 直接回退原问题 + need_retrieve=True，不调 LLM
3. 成功路径（注入假 rewriter）：query 去首尾空白、need_retrieve 透传、
   发给 LLM 的消息是 `[SystemMessage(REWRITE_PROMPT), HumanMessage(对话…当前问题…)]`
4. 寒暄路径（假 rewriter 返回 need_retrieve=False）→ 原样透传给调用方
5. 模型给出空白 standalone_query → 回退原问题（对齐 TS `.trim() || question`）
6. LLM 抛异常 → 回退原问题且**不向上抛**（改写失败不能阻断对话）
7. 真实 LLM 调用 3 例（**仅当配置了 LLM Key 时**）：指代消解（那谁审批→差旅）、
   寒暄（谢谢→need_retrieve=False）、独立制度问题（年假→need_retrieve=True）。
   未配置时明确打印 SKIP —— ⚠️ **SKIP 不算通过**，别当绿灯读。

⚠️ 与短期/长期记忆同一个教训：本模块所有失败路径都是「WARN + 回退原文」，
若不做前提断言，调用失败会被误读成正常回退。第 2~6 步用注入假 rewriter 的方式
把成功/失败两条路径都钉死，第 7 步才用真实模型验提示词效果。
第 7 步是 3 次真实 LLM 调用（prompt 很短），会消耗少量额度。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_chat_query_rewrite.py

前置：LLM_API_KEY 已配置（第 7 步需要）。不污染任何持久层（纯内存计算）。
"""

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.services.chat_query_rewrite import (
    REWRITE_PROMPT,
    ChatQueryRewriteService,
    RewriteResult,
    chat_query_rewrite_service,
)


class _FakeRewriter:
    """假 rewriter：按注入的行为返回结果或抛错，并记录收到的消息"""

    def __init__(self, result: RewriteResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.messages: list[Any] | None = None

    async def ainvoke(self, messages: list[Any]) -> RewriteResult:
        self.messages = messages
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


async def main() -> None:
    has_key = bool(
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    real_rewriter = chat_query_rewrite_service._rewriter
    print(
        f"   · 配置：LLM Key={'已配置' if has_key else '未配置'}、"
        f"AI_QUERY_REWRITE_TIMEOUT_MS={settings.AI_QUERY_REWRITE_TIMEOUT_MS}、"
        f"MODEL_NAME={settings.MODEL_NAME or '(空)'}、"
        f"LLM_MODEL={settings.LLM_MODEL}"
    )

    # 1) 前提断言：rewriter 存在性与 key 一致
    assert (real_rewriter is not None) is has_key, (
        "rewriter 是否存在必须与「是否配置 LLM Key」一致，否则回退断言会假绿"
    )
    print(
        f"   1) 前提断言 ✓ rewriter {'已构建' if has_key else '未构建（将只验回退路径）'}"
    )

    try:
        history = [
            HumanMessage("公司的差旅制度里住宿标准是多少？"),
            AIMessage("按职级分档，一线城市每晚不超过 600 元。"),
        ]

        # 2a) 无历史 → 回退，不调 LLM
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", [])
        assert plan == {"query": "那谁审批？", "need_retrieve": True}, (
            f"无历史应原样回退，实际 {plan}"
        )
        print("   2a) 无历史 → 回退原问题 + need_retrieve=True ✓")

        # 2b) 历史全空白（compact 后为空）→ 回退
        blank = [SystemMessage("系统消息不进上下文"), HumanMessage("   ")]
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", blank)
        assert plan == {"query": "那谁审批？", "need_retrieve": True}, (
            f"空上下文应原样回退，实际 {plan}"
        )
        print("   2b) 空上下文（历史全空白）→ 回退原问题 ✓")

        # 3) 成功路径：注入假 rewriter，钉死输出与消息构造
        fake = _FakeRewriter(RewriteResult(standalone_query="  差旅审批流程  ", need_retrieve=True))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", history)
        assert plan == {"query": "差旅审批流程", "need_retrieve": True}, (
            f"应透传改写结果且去空白，实际 {plan}"
        )
        assert fake.messages is not None and len(fake.messages) == 2, "应发两条消息"
        assert isinstance(fake.messages[0], SystemMessage), "第 1 条应是 SystemMessage"
        assert fake.messages[0].content == REWRITE_PROMPT, "system 必须是 REWRITE_PROMPT 原文"
        assert isinstance(fake.messages[1], HumanMessage), "第 2 条应是 HumanMessage"
        user_text = fake.messages[1].content
        assert "对话：" in user_text and "当前问题：那谁审批？" in user_text, (
            f"user 消息应含对话上下文与当前问题，实际 {user_text!r}"
        )
        assert "住宿标准" in user_text, "上下文应来自 compact_rewrite_context（含最近历史）"
        print("   3) 成功路径 ✓ query 去空白、need_retrieve 透传、消息构造正确")

        # 4) 寒暄路径：need_retrieve=False 原样透传
        fake = _FakeRewriter(RewriteResult(standalone_query="谢谢你", need_retrieve=False))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.rewrite("谢谢你", history)
        assert plan == {"query": "谢谢你", "need_retrieve": False}, (
            f"寒暄应透传 need_retrieve=False，实际 {plan}"
        )
        print("   4) 寒暄路径 ✓ need_retrieve=False 透传（调用方可跳过检索）")

        # 5) 模型给出空白 query → 回退原问题
        fake = _FakeRewriter(RewriteResult(standalone_query="   ", need_retrieve=True))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", history)
        assert plan == {"query": "那谁审批？", "need_retrieve": True}, (
            f"空白 standalone_query 应回退原问题（对齐 TS trim() || question），实际 {plan}"
        )
        print("   5) 空白 query → 回退原问题 ✓")

        # 6) LLM 抛异常 → 回退原文且不抛
        fake = _FakeRewriter(error=RuntimeError("模拟超时"))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", history)
        assert plan == {"query": "那谁审批？", "need_retrieve": True}, (
            f"异常应回退原问题，实际 {plan}"
        )
        print("   6) LLM 异常 → 回退原问题且不抛 ✓（改写失败不阻断对话）")

        # 7) 真实 LLM（需要 key）
        if not has_key:
            print("   7) SKIP —— 未配置 LLM Key，真实改写未验证（SKIP 不算通过）")
        else:
            real_service = ChatQueryRewriteService()
            assert real_service._rewriter is not None, "配置了 key 应能构建 rewriter"

            # 7a) 指代消解：那谁审批？ → 应带上「差旅」
            plan = await real_service.rewrite("那谁审批？", history)
            assert plan["need_retrieve"] is True, f"制度追问应 need_retrieve=True，实际 {plan}"
            assert ("差旅" in plan["query"]) or ("审批" in plan["query"]), (
                f"改写应消解指代补出「差旅/审批」，实际 {plan['query']!r}"
            )
            print(f"   7a) 真实指代消解 ✓ 「那谁审批？」→ {plan['query']!r}")

            # 7b) 寒暄：谢谢你 → need_retrieve=False
            greeting_history = [
                HumanMessage("你好"),
                AIMessage("你好，我是企业知识库助手，有什么可以帮你？"),
            ]
            plan = await real_service.rewrite("谢谢你", greeting_history)
            assert plan["need_retrieve"] is False, (
                f"寒暄应 need_retrieve=False，实际 {plan}"
            )
            print(f"   7b) 真实寒暄判定 ✓ 「谢谢你」→ need_retrieve=False，query={plan['query']!r}")

            # 7c) 独立制度问题：年假 → need_retrieve=True
            plan = await real_service.rewrite("年假有多少天？", greeting_history)
            assert plan["need_retrieve"] is True, (
                f"制度问题应 need_retrieve=True，实际 {plan}"
            )
            assert plan["query"], "standalone_query 不应为空"
            print(f"   7c) 真实独立问题 ✓ 「年假有多少天？」→ {plan['query']!r}")
    finally:
        # 还原单例，避免污染同进程内其它使用方
        chat_query_rewrite_service._rewriter = real_rewriter

    print("\n检索改写（追问 → 独立短查询）冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
