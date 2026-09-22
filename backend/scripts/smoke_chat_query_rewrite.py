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
8. classify 意图路由（路线 A：TypeSafe 管决策、LLM 管改写）：_to_plan 纯函数
   5 路意图 → 工具开关；TypeSafe 关闭时的兜底链（无历史 / LLM 异常 → kb、
   need_retrieve=False → chitchat）；真实 TypeSafe 需 key + SDK（否则 SKIP）。
9. 切题判定（检索后：召回文档与问题是否切题）：无召回 / 未配置判定器 / 判定异常
   → 返回 None（调用方按切题处理，不阻断）；成功路径钉死消息构造；真实 LLM 需 key
   （切题文档 → True、无关文档 → False）。
10. TypeSafe(jev) 切题判定：优先于 LLM（irrelevant 被采信）、失败才回退 LLM；
    真实 jev 需 key + SDK（否则 SKIP）。
11. 不足后改写 rewrite_after_retrieve（对应 TS rewriteAfterRetrieve）：irrelevant/
    empty 共用 RETRY_REWRITE_PROMPT，消息含问题/上次词/不足原因/命中摘要；结果为空/
    异常/未配 → 回退原问题，原问题=上次词 → 空串（永不 None）。
12. 改写门控 rewrite()（检索前：消解指代 vs 自足）：TypeSafe 判「自足/依赖上文/
    无需检索」，自足→不调 LLM、无关→need_retrieve=False、依赖上文→LLM 改写；
    门控不可用回退 LLM；真实门控需 key + SDK（否则 SKIP）。
13. agentic_retrieve 编排（对应 TS agentic-retrieve.ts）：首轮切题不重试、跑偏改写
    再查（retried_ok）、空命中改写失败→empty、检索异常→error、on_eval/on_rewrite
    回调与 camelCase payload；to_eval 映射。

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
from app.core.logging import setup_logging
from app.pipeline.types import ChunkHit
from app.services.agentic_retrieve import (
    retrieve_and_grade,
    retrieve_until_relevant,
    to_eval,
)
from app.services.chat_query_rewrite import (
    CHAT_INTENTS,
    INTENT_LABEL,
    RELEVANCE_CHOICES,
    RELEVANCE_PROMPT,
    RETRY_REWRITE_PROMPT,
    REWRITE_GATE_CHOICES,
    REWRITE_PROMPT,
    ChatQueryRewriteService,
    HitGrade,
    RelevanceResult,
    RetryRewriteResult,
    RewriteResult,
    chat_query_rewrite_service,
    summarize_hits,
)


class _FakeRewriter:
    """假 rewriter：按注入的行为返回结果或抛错，并记录收到的消息"""

    def __init__(self, result: RewriteResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.messages: list[Any] | None = None

    async def ainvoke(
        self, messages: list[Any], config: Any = None
    ) -> RewriteResult:
        self.messages = messages
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _FakeRetryWriter:
    """假不足后改写器：返回 RetryRewriteResult 或抛错，并记录消息"""

    def __init__(
        self, result: RetryRewriteResult | None = None, error: Exception | None = None
    ):
        self.result = result
        self.error = error
        self.messages: list[Any] | None = None

    async def ainvoke(
        self, messages: list[Any], config: Any = None
    ) -> RetryRewriteResult:
        self.messages = messages
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _FakeJudge:
    """假切题判定器：按注入的行为返回结果或抛错，并记录收到的消息"""

    def __init__(self, result: RelevanceResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.messages: list[Any] | None = None

    async def ainvoke(
        self, messages: list[Any], config: Any = None
    ) -> RelevanceResult:
        self.messages = messages
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _FakeChoice:
    """假 TypeSafe Choice 构造器：只记录 instructions / criteria"""

    def __init__(self, instructions: Any = None, criteria: Any = None):
        self.instructions = instructions
        self.criteria = criteria


class _FakeAnswer:
    def __init__(self, choice: str, confidence: float):
        self.choice = choice
        self.confidence = confidence


class _FakeTypeSafeResponse:
    """假 system_one 返回：按请求的 question key 生成 choices[name].choice / .confidence"""

    def __init__(self, answers: dict[str, str], confidence: float = 0.9):
        self.choices = {
            name: _FakeAnswer(choice, confidence) for name, choice in answers.items()
        }


class _FakeTypeSafeClient:
    """假 TypeSafe 客户端：异步上下文管理器 + 可配置的 system_one 结果

    `answers`：question key → 返回的 choice（如 {"relevance": "irrelevant"}）；
    未配置的 key 回落 `default_answer`。
    """

    answers: dict[str, str] = {}
    default_answer = "relevant"
    error: Exception | None = None
    last_state: dict[str, Any] | None = None
    last_questions: dict[str, Any] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeTypeSafeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def system_one(self, state: Any, questions: Any) -> _FakeTypeSafeResponse:
        type(self).last_state = state
        type(self).last_questions = questions
        if type(self).error is not None:
            raise type(self).error
        answers = {
            name: type(self).answers.get(name, type(self).default_answer)
            for name in questions
        }
        return _FakeTypeSafeResponse(answers)


# agentic_retrieve 单测用的假检索/改写（模块函数是依赖注入，不碰全局单例）
_ROW_HIT = {
    "chunkId": "c9",
    "documentId": "d9",
    "documentTitle": "差旅审批流程",
    "content": "差旅由部门负责人审批",
    "heading": None,
    "score": 0.9,
}
_ROW_OTHER = {
    "chunkId": "c8",
    "documentId": "d8",
    "documentTitle": "年假制度",
    "content": "年假按工龄",
    "heading": None,
    "score": 0.8,
}


class _FakeRetrieval:
    """假混合检索：按 query 查表返回 camelCase 行，并记录调用"""

    def __init__(self, table: dict[str, list[dict]]):
        self.table = table
        self.calls: list[str] = []

    async def retrieve(self, query: str, top_k: int, user: Any = None) -> list[dict]:
        self.calls.append(query)
        return self.table.get(query, [])


class _FakeRewriteService:
    """假改写服务：按调用顺序返回注入的 HitGrade，固定返回改写词"""

    def __init__(self, grades: list[HitGrade], retry_query: str | None = None):
        self.grades = list(grades)
        self.retry_query = retry_query
        self.grade_calls = 0
        self.rewrite_calls: list[tuple[str, str]] = []

    async def grade_hits(self, question: str, hits: list[ChunkHit]) -> HitGrade:
        grade = self.grades[min(self.grade_calls, len(self.grades) - 1)]
        self.grade_calls += 1
        return grade

    async def rewrite_after_retrieve(
        self, question: str, used_query: str, grade: HitGrade, hits: list[ChunkHit]
    ) -> str | None:
        self.rewrite_calls.append((used_query, grade.reason))
        return self.retry_query


async def main() -> None:
    # 启用统一日志（否则 root 默认 WARNING，logger.info 全部看不见）
    setup_logging()
    has_key = bool(
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    real_rewriter = chat_query_rewrite_service._rewriter
    real_judge = chat_query_rewrite_service._relevance_judge
    real_retry_writer = chat_query_rewrite_service._retry_writer
    real_typesafe_cls = chat_query_rewrite_service._typesafe_client_cls
    real_typesafe_choice_cls = chat_query_rewrite_service._typesafe_choice_cls
    # 确定性用例（第 2~7 步，rewrite/classify）先关 TypeSafe：
    # rewrite() 现在是 classify() 的薄封装，会走 TypeSafe 意图/门控；不关会真调网络、结果不可复现。
    # 第 8f/10d/12e 均用**新实例**验真实 TypeSafe，不受影响。
    chat_query_rewrite_service._typesafe_client_cls = None
    chat_query_rewrite_service._typesafe_choice_cls = None
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
    assert (real_retry_writer is not None) is has_key, "retry_writer 存在性同理"
    print(
        f"   1) 前提断言 ✓ rewriter/retry_writer "
        f"{'已构建' if has_key else '未构建（将只验回退路径）'}"
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
        # 8) classify 意图路由（路线 A：TypeSafe 管决策、LLM 管改写）
        #    8a~8e 强制关闭 TypeSafe，钉死「未配 TypeSafe 时的兜底链」——否则配了 key 时
        #    会真调 TypeSafe，结果不可复现（与上面注入假 rewriter 同思路）。
        chat_query_rewrite_service._typesafe_client_cls = None
        chat_query_rewrite_service._typesafe_choice_cls = None

        # 8a) _to_plan 纯函数：5 路意图 → 工具开关 + 中文 label
        assert chat_query_rewrite_service._to_plan("kb", "q") == {
            "intent": "kb", "label": "知识库", "query": "q",
            "allow_retrieve": True, "allow_web": False,
        }, "_to_plan(kb) 应只开检索"
        assert chat_query_rewrite_service._to_plan("chitchat", "q")["allow_retrieve"] is False
        assert chat_query_rewrite_service._to_plan("chitchat", "q")["allow_web"] is False
        assert chat_query_rewrite_service._to_plan("profile", "q")["allow_retrieve"] is False
        assert chat_query_rewrite_service._to_plan("web", "q")["allow_retrieve"] is False
        assert chat_query_rewrite_service._to_plan("web", "q")["allow_web"] is True
        assert chat_query_rewrite_service._to_plan("kb_then_web", "q")["allow_retrieve"] is True
        assert chat_query_rewrite_service._to_plan("kb_then_web", "q")["allow_web"] is True
        assert set(INTENT_LABEL) == set(CHAT_INTENTS), "每个意图都应有中文名"
        print("   8a) _to_plan 纯函数 ✓ 5 路意图 → 工具开关、label 齐全")

        # 8b) 无历史 → intent=kb 兜底 + query=原问题（不调 TypeSafe 也不调 LLM）
        plan = await chat_query_rewrite_service.classify("年假有多少天？", [])
        assert plan == {
            "intent": "kb", "label": "知识库", "query": "年假有多少天？",
            "allow_retrieve": True, "allow_web": False,
        }, f"无历史应兜底 kb，实际 {plan}"
        print("   8b) 无历史 → intent=kb 兜底 + query=原问题 ✓")

        # 8c) 有历史 + need_retrieve=True → kb + query 改写
        fake = _FakeRewriter(RewriteResult(standalone_query="差旅审批流程", need_retrieve=True))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.classify("那谁审批？", history)
        assert plan["intent"] == "kb" and plan["allow_retrieve"] is True and plan["allow_web"] is False, f"实际 {plan}"
        assert plan["query"] == "差旅审批流程", f"query 应改写，实际 {plan}"
        print("   8c) 有历史 + need_retrieve=True → intent=kb、query 改写 ✓")

        # 8d) 有历史 + need_retrieve=False → chitchat + 不检索
        fake = _FakeRewriter(RewriteResult(standalone_query="谢谢你", need_retrieve=False))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.classify("谢谢你", history)
        assert plan["intent"] == "chitchat" and plan["allow_retrieve"] is False, f"实际 {plan}"
        print("   8d) 有历史 + need_retrieve=False → intent=chitchat、不检索 ✓")

        # 8e) LLM 异常 → intent=kb 兜底 + query=原问题（失败不阻断）
        fake = _FakeRewriter(error=RuntimeError("模拟超时"))
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.classify("那谁审批？", history)
        assert plan["intent"] == "kb" and plan["query"] == "那谁审批？", f"实际 {plan}"
        print("   8e) LLM 异常 → intent=kb 兜底 + query=原问题 ✓")

        # 8f) 真实 TypeSafe（需要 TYPESAFE_API_KEY + typesafe_sdk 已安装）
        #     直接验 _route_intent 而非 classify：classify 在 TypeSafe 失败时会静默回退 kb，
        #     若只看 classify 会把「调用失败」误读成「正常返回 kb」而假绿（同冒烟前提断言教训）。
        if not settings.TYPESAFE_API_KEY:
            print("   8f) SKIP —— 未配置 TYPESAFE_API_KEY，真实 TypeSafe 意图未验证（SKIP 不算通过）")
        else:
            try:
                import typesafe_sdk  # noqa: F401
            except ImportError:
                print("   8f) SKIP —— typesafe_sdk 未安装，真实 TypeSafe 意图未验证（SKIP 不算通过）")
            else:
                _real_svc = ChatQueryRewriteService()
                assert _real_svc._typesafe_client_cls is not None, "配了 key 且 SDK 已装应能构建 client"
                intent = await _real_svc._route_intent("年假有多少天？", "")
                assert intent is not None, "TypeSafe 调用失败（返回 None），真实意图未验证"
                assert intent in CHAT_INTENTS, f"意图应在 5 路内，实际 {intent!r}"
                print(f"   8f) 真实 TypeSafe ✓ 「年假有多少天？」→ intent={intent!r}")

        # 9) 切题判定（检索后：召回文档与问题是否切题）
        on_topic_hit = ChunkHit(
            chunkId="c1",
            documentId="d1",
            documentTitle="差旅审批流程",
            content="差旅费用由部门负责人审批，超过 5000 元需总经理审批。",
            heading="审批权限",
        )
        off_topic_hit = ChunkHit(
            chunkId="c2",
            documentId="d2",
            documentTitle="食堂菜单",
            content="本周食堂供应红烧肉、清蒸鱼、炒时蔬。",
            heading="午餐",
        )

        # 9a) 无召回 → 直接 None，不调判定器
        chat_query_rewrite_service._relevance_judge = _FakeJudge(
            RelevanceResult(relevant=True, reason="x")
        )
        assert await chat_query_rewrite_service.check_relevance("差旅谁审批？", []) is None
        print("   9a) 无召回 → None（不调判定器）✓")

        # 9b) 未配置判定器（未配置 LLM Key）→ None，调用方按切题处理
        chat_query_rewrite_service._relevance_judge = None
        assert (
            await chat_query_rewrite_service.check_relevance("差旅谁审批？", [on_topic_hit])
            is None
        )
        print("   9b) 未配置判定器 → None（按切题处理，不阻断）✓")

        # 9c) 成功路径：钉死输出与消息构造
        fake_judge = _FakeJudge(RelevanceResult(relevant=True, reason="资料含差旅审批权限"))
        chat_query_rewrite_service._relevance_judge = fake_judge
        result = await chat_query_rewrite_service.check_relevance(
            "差旅谁审批？", [on_topic_hit]
        )
        assert result is not None and result.relevant is True, f"实际 {result}"
        assert fake_judge.messages is not None and len(fake_judge.messages) == 2, "应发两条消息"
        assert isinstance(fake_judge.messages[0], SystemMessage), "第 1 条应是 SystemMessage"
        assert fake_judge.messages[0].content == RELEVANCE_PROMPT, (
            "system 必须是 RELEVANCE_PROMPT 原文"
        )
        assert isinstance(fake_judge.messages[1], HumanMessage), "第 2 条应是 HumanMessage"
        judge_text = fake_judge.messages[1].content
        assert "用户问题：差旅谁审批？" in judge_text, f"应含用户问题，实际 {judge_text!r}"
        assert "差旅审批流程" in judge_text, "应含召回文档标题"
        print("   9c) 成功路径 ✓ 输出透传、消息构造正确")

        # 9d) 判定器抛异常 → 返回 None 且不抛（判定失败不阻断作答）
        chat_query_rewrite_service._relevance_judge = _FakeJudge(error=RuntimeError("模拟超时"))
        assert (
            await chat_query_rewrite_service.check_relevance("差旅谁审批？", [on_topic_hit])
            is None
        )
        print("   9d) 判定器异常 → None 且不抛 ✓")

        # 9e) 真实 LLM 切题判定（需要 key）：切题文档 → True、无关文档 → False
        if not has_key:
            print("   9e) SKIP —— 未配置 LLM Key，真实切题判定未验证（SKIP 不算通过）")
        else:
            _real_svc2 = ChatQueryRewriteService()
            # 强制关掉 TypeSafe，隔离验证 LLM 回退判定（check_relevance 现在优先走 jev）
            _real_svc2._typesafe_client_cls = None
            _real_svc2._typesafe_choice_cls = None
            assert _real_svc2._relevance_judge is not None, "配置了 key 应能构建判定器"
            good = await _real_svc2.check_relevance("差旅审批流程是什么？", [on_topic_hit])
            assert good is not None, "真实判定失败（返回 None），未验证"
            assert good.relevant is True, f"切题文档应 relevant=True，实际 {good}"
            bad = await _real_svc2.check_relevance("差旅审批流程是什么？", [off_topic_hit])
            assert bad is not None, "真实判定失败（返回 None），未验证"
            assert bad.relevant is False, f"无关文档应 relevant=False，实际 {bad}"
            print(f"   9e) 真实切题判定 ✓ 切题→True、无关→False（reason={bad.reason!r}）")

        # 10) TypeSafe(jev) 切题判定：优先于 LLM，失败才回退
        chat_query_rewrite_service._typesafe_client_cls = _FakeTypeSafeClient
        chat_query_rewrite_service._typesafe_choice_cls = _FakeChoice

        # 10a) TypeSafe 返回 irrelevant → 采信（即使 LLM 判定为 True）
        _FakeTypeSafeClient.answers = {"relevance": "irrelevant"}
        _FakeTypeSafeClient.error = None
        chat_query_rewrite_service._relevance_judge = _FakeJudge(
            RelevanceResult(relevant=True, reason="LLM 误判")
        )
        result = await chat_query_rewrite_service.check_relevance(
            "差旅谁审批？", [on_topic_hit]
        )
        assert result is not None and result.relevant is False, f"应采信 TypeSafe，实际 {result}"
        assert result.reason == "资料与问题不相关", f"reason 应是自然语句，实际 {result.reason!r}"
        assert _FakeTypeSafeClient.last_state is not None, "应调用 TypeSafe"
        assert "差旅审批流程" in str(_FakeTypeSafeClient.last_state.get("documents")), (
            "TypeSafe state 应含召回资料"
        )
        assert _FakeTypeSafeClient.last_questions is not None
        _choice_obj = _FakeTypeSafeClient.last_questions["relevance"]
        assert set(_choice_obj.criteria) == set(RELEVANCE_CHOICES), "应传两路 Choice"
        print("   10a) TypeSafe 判定 ✓ 优先于 LLM（irrelevant 被采信）")

        # 10b) TypeSafe 返回 relevant → True
        _FakeTypeSafeClient.answers = {"relevance": "relevant"}
        result = await chat_query_rewrite_service.check_relevance(
            "差旅谁审批？", [on_topic_hit]
        )
        assert result is not None and result.relevant is True, f"实际 {result}"
        print("   10b) TypeSafe 判定 ✓ relevant 透传")

        # 10c) TypeSafe 调用异常 → 回退 LLM（此处 LLM 判 True，证明确实走了回退）
        _FakeTypeSafeClient.error = RuntimeError("模拟 TypeSafe 超时")
        chat_query_rewrite_service._relevance_judge = _FakeJudge(
            RelevanceResult(relevant=True, reason="LLM 回退")
        )
        result = await chat_query_rewrite_service.check_relevance(
            "差旅谁审批？", [on_topic_hit]
        )
        assert result is not None and result.relevant is True, f"异常应回退 LLM，实际 {result}"
        assert result.reason == "LLM 回退", f"应来自 LLM 回退，实际 {result.reason!r}"
        print("   10c) TypeSafe 异常 → 回退 LLM ✓")

        # 10d) 真实 TypeSafe(jev)（需要 key + SDK）：直接验 _route_relevance，
        #      避免 check_relevance 在 TypeSafe 失败时静默回退 LLM 造成假绿。
        if not settings.TYPESAFE_API_KEY:
            print("   10d) SKIP —— 未配置 TYPESAFE_API_KEY，真实 jev 切题未验证（SKIP 不算通过）")
        else:
            try:
                import typesafe_sdk  # noqa: F401
            except ImportError:
                print("   10d) SKIP —— typesafe_sdk 未安装，真实 jev 切题未验证（SKIP 不算通过）")
            else:
                _real_svc3 = ChatQueryRewriteService()
                assert _real_svc3._typesafe_client_cls is not None, "配了 key 且 SDK 已装应能构建 client"
                _ctx_good = summarize_hits([on_topic_hit])
                good = await _real_svc3._route_relevance("差旅审批流程是什么？", _ctx_good)
                assert good is not None, "TypeSafe 切题调用失败（返回 None），未验证"
                assert good.relevant is True, f"切题文档应 relevant=True，实际 {good}"
                _ctx_bad = summarize_hits([off_topic_hit])
                bad = await _real_svc3._route_relevance("差旅审批流程是什么？", _ctx_bad)
                assert bad is not None, "TypeSafe 切题调用失败（返回 None），未验证"
                assert bad.relevant is False, f"无关文档应 relevant=False，实际 {bad}"
                print(f"   10d) 真实 jev 切题判定 ✓ 切题→True、无关→False（reason={bad.reason!r}）")

        # 11) 不足后改写 rewrite_after_retrieve（对应 TS rewriteAfterRetrieve，永不 None）
        # 11a) irrelevant（跑偏）→ RETRY_REWRITE_PROMPT，消息含问题/上次词/不足原因/摘要
        fake = _FakeRetryWriter(RetryRewriteResult(query="差旅费用报销标准"))
        chat_query_rewrite_service._retry_writer = fake
        query = await chat_query_rewrite_service.rewrite_after_retrieve(
            "报销怎么弄？",
            "报销怎么弄？",
            HitGrade(ok=False, reason="irrelevant", text="资料与问题不相关"),
            [off_topic_hit],
        )
        assert query == "差旅费用报销标准", f"实际 {query!r}"
        assert fake.messages is not None and len(fake.messages) == 2, "应发两条消息"
        assert fake.messages[0].content == RETRY_REWRITE_PROMPT, "应用 RETRY_REWRITE_PROMPT"
        body = fake.messages[1].content
        assert "用户问题：报销怎么弄？" in body and "上次检索词：报销怎么弄？" in body, (
            f"应含原问题与上次检索词，实际 {body!r}"
        )
        assert "不足原因：资料与问题不相关" in body and "食堂菜单" in body, (
            f"应含不足原因与命中摘要，实际 {body!r}"
        )
        print("   11a) 跑偏改写 ✓ RETRY_REWRITE_PROMPT、含问题/上次词/不足原因/命中摘要")

        # 11b) empty（无命中）→ 提示「上次无命中。」；结果与上次相同则回退原问题
        fake = _FakeRetryWriter(RetryRewriteResult(query="报销怎么弄？"))
        chat_query_rewrite_service._retry_writer = fake
        query = await chat_query_rewrite_service.rewrite_after_retrieve(
            "报销怎么弄？",
            "报销流程",
            HitGrade(ok=False, reason="empty", text="未检索到资料"),
            [],
        )
        assert "上次无命中。" in fake.messages[1].content, "empty 应提示无命中"
        assert query == "报销怎么弄？", f"结果同上次应回退原问题，实际 {query!r}"
        print("   11b) 空命中 ✓ 提示无命中、结果同上次则回退原问题")

        # 11c) 空结果/异常/未配 LLM → 回退原问题；原问题=上次词 → 空串（无法再查）
        _empty_grade = HitGrade(ok=False, reason="empty", text="未检索到资料")
        chat_query_rewrite_service._retry_writer = _FakeRetryWriter(
            RetryRewriteResult(query="   ")
        )
        assert (
            await chat_query_rewrite_service.rewrite_after_retrieve(
                "报销怎么弄？", "报销流程", _empty_grade, []
            )
            == "报销怎么弄？"
        )
        chat_query_rewrite_service._retry_writer = _FakeRetryWriter(
            error=RuntimeError("模拟超时")
        )
        assert (
            await chat_query_rewrite_service.rewrite_after_retrieve(
                "报销怎么弄？", "报销流程", _empty_grade, []
            )
            == "报销怎么弄？"
        )
        chat_query_rewrite_service._retry_writer = None
        assert (
            await chat_query_rewrite_service.rewrite_after_retrieve(
                "报销怎么弄？", "报销流程", _empty_grade, []
            )
            == "报销怎么弄？"
        )
        assert (
            await chat_query_rewrite_service.rewrite_after_retrieve(
                "报销怎么弄？", "报销怎么弄？", _empty_grade, []
            )
            == ""
        ), "原问题与上次检索词相同应返回空串"
        print("   11c) 空结果/异常/未配 → 回退原问题；原问题=上次词 → 空串 ✓")

        # 12) 改写门控 rewrite()：TypeSafe 判自足/依赖上文/无需检索，只有追问才调 LLM
        chat_query_rewrite_service._typesafe_client_cls = _FakeTypeSafeClient
        chat_query_rewrite_service._typesafe_choice_cls = _FakeChoice
        _FakeTypeSafeClient.error = None

        # 12a) 问题自足 → 不调 LLM，直接用原问题
        _FakeTypeSafeClient.answers = {"rewrite_gate": "standalone"}
        spy = _FakeRewriter(
            RewriteResult(standalone_query="不该被调用", need_retrieve=True)
        )
        chat_query_rewrite_service._rewriter = spy
        plan = await chat_query_rewrite_service.rewrite("年假有多少天？", history)
        assert plan == {"query": "年假有多少天？", "need_retrieve": True}, f"实际 {plan}"
        assert spy.messages is None, "自足问题不应调 LLM 改写"
        assert _FakeTypeSafeClient.last_questions is not None
        _gate_obj = _FakeTypeSafeClient.last_questions["rewrite_gate"]
        assert set(_gate_obj.criteria) == set(REWRITE_GATE_CHOICES), "应传三路 Choice"
        print("   12a) 自足 → 跳过 LLM 改写，直接用原问题 ✓")

        # 12b) 无需检索（寒暄）→ 不调 LLM，need_retrieve=False
        _FakeTypeSafeClient.answers = {"rewrite_gate": "no_retrieve"}
        plan = await chat_query_rewrite_service.rewrite("谢谢你", history)
        assert plan == {"query": "谢谢你", "need_retrieve": False}, f"实际 {plan}"
        assert spy.messages is None, "寒暄不应调 LLM 改写"
        print("   12b) 无需检索 → 跳过改写，need_retrieve=False ✓")

        # 12c) 依赖上文 → 才调 LLM 改写消解指代
        _FakeTypeSafeClient.answers = {"rewrite_gate": "needs_context"}
        fake = _FakeRewriter(
            RewriteResult(standalone_query="差旅审批流程", need_retrieve=True)
        )
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", history)
        assert plan == {"query": "差旅审批流程", "need_retrieve": True}, f"实际 {plan}"
        assert fake.messages is not None, "依赖上文应调 LLM 改写"
        print("   12c) 依赖上文 → LLM 改写消解指代 ✓")

        # 12d) 门控不可用（TypeSafe 关闭）→ 回退 LLM 改写（保持旧行为，零回归）
        chat_query_rewrite_service._typesafe_client_cls = None
        chat_query_rewrite_service._typesafe_choice_cls = None
        fake = _FakeRewriter(
            RewriteResult(standalone_query="差旅审批流程", need_retrieve=True)
        )
        chat_query_rewrite_service._rewriter = fake
        plan = await chat_query_rewrite_service.rewrite("那谁审批？", history)
        assert plan == {"query": "差旅审批流程", "need_retrieve": True}, f"实际 {plan}"
        assert fake.messages is not None, "门控不可用应回退 LLM 改写"
        print("   12d) 门控不可用 → 回退 LLM 改写（零回归）✓")

        # 12e) 真实 TypeSafe 门控（需要 key + SDK）
        if not settings.TYPESAFE_API_KEY:
            print("   12e) SKIP —— 未配置 TYPESAFE_API_KEY，真实改写门控未验证（SKIP 不算通过）")
        else:
            try:
                import typesafe_sdk  # noqa: F401
            except ImportError:
                print("   12e) SKIP —— typesafe_sdk 未安装，真实改写门控未验证（SKIP 不算通过）")
            else:
                _real_svc4 = ChatQueryRewriteService()
                assert _real_svc4._typesafe_client_cls is not None
                g1 = await _real_svc4._route_rewrite_gate("那谁审批？")
                assert g1 == "needs_context", f"「那谁审批？」应依赖上文，实际 {g1!r}"
                g2 = await _real_svc4._route_rewrite_gate("年假有多少天？")
                assert g2 == "standalone", f"「年假有多少天？」应自足，实际 {g2!r}"
                g3 = await _real_svc4._route_rewrite_gate("谢谢你")
                assert g3 == "no_retrieve", f"「谢谢你」应无需检索，实际 {g3!r}"
                print(f"   12e) 真实改写门控 ✓ 那谁审批→{g1}、年假→{g2}、谢谢你→{g3}")

        # 13) agentic_retrieve 编排（对应 TS agentic-retrieve.ts，依赖注入不碰全局单例）
        # 13a) 首轮切题 → 直接返回 hits，不重试
        retr = _FakeRetrieval({"差旅审批流程": [_ROW_HIT]})
        rw = _FakeRewriteService([HitGrade(ok=True, reason="relevant", text="资料与问题相关")])
        out = await retrieve_until_relevant(
            question="差旅谁审批？",
            query="差旅审批流程",
            top_k=5,
            user=None,
            retrieval=retr,
            rewrite=rw,
        )
        assert out.eval.ok and out.eval.reason == "ok", f"实际 {out.eval}"
        assert len(out.hits) == 1 and out.used_query == "差旅审批流程", f"实际 {out}"
        assert rw.rewrite_calls == [], "切题不应改写"
        print("   13a) 首轮切题 → 返回 hits、不重试 ✓")

        # 13b) 首轮跑偏 → 改写再查切题（retried_ok）
        retr = _FakeRetrieval({"差旅审批流程": [_ROW_OTHER], "差旅审批": [_ROW_HIT]})
        rw = _FakeRewriteService(
            [
                HitGrade(ok=False, reason="irrelevant", text="资料与问题不相关"),
                HitGrade(ok=True, reason="relevant", text="资料与问题相关"),
            ],
            retry_query="差旅审批",
        )
        evals: list[Any] = []
        rewrites: list[str] = []
        out = await retrieve_until_relevant(
            question="差旅谁审批？",
            query="差旅审批流程",
            top_k=5,
            user=None,
            retrieval=retr,
            rewrite=rw,
            on_eval=evals.append,
            on_rewrite=rewrites.append,
        )
        assert out.eval.ok and out.eval.reason == "retried_ok", f"实际 {out.eval}"
        assert out.eval.query == "差旅审批流程" and out.eval.retry_query == "差旅审批", (
            f"实际 {out.eval}"
        )
        assert out.used_query == "差旅审批", f"实际 {out.used_query}"
        assert retr.calls == ["差旅审批流程", "差旅审批"], f"应再查一次，实际 {retr.calls}"
        assert [e.reason for e in evals] == ["irrelevant", "retried_ok"], (
            f"实际 {[e.reason for e in evals]}"
        )
        assert rewrites == ["差旅审批"], f"实际 {rewrites}"
        assert out.eval.to_payload()["retryQuery"] == "差旅审批", "payload 应是 camelCase"
        print("   13b) 跑偏 → 改写再查切题（retried_ok，两次 eval / camelCase payload）✓")

        # 13c) 首轮空命中且改写失败 → hits 空、eval=empty
        retr = _FakeRetrieval({})
        rw = _FakeRewriteService(
            [HitGrade(ok=False, reason="empty", text="未检索到资料")], retry_query=None
        )
        out = await retrieve_until_relevant(
            question="年假多少天？",
            query="年假",
            top_k=5,
            user=None,
            retrieval=retr,
            rewrite=rw,
        )
        assert out.hits == [] and out.eval.reason == "empty", f"实际 {out}"
        print("   13c) 空命中且改写失败 → hits 空、eval=empty ✓")

        # 13d) 检索抛异常 → 收敛成 error 评估（不抛）
        class _BoomRetrieval:
            async def retrieve(self, query: str, top_k: int, user: Any = None) -> list[dict]:
                raise RuntimeError("模拟 ES 挂了")

        g = await retrieve_and_grade(
            question="q",
            query="q",
            top_k=5,
            user=None,
            retrieval=_BoomRetrieval(),
            rewrite=_FakeRewriteService([HitGrade(True, "relevant", "x")]),
        )
        assert g.eval.reason == "error" and g.hits == [], f"实际 {g.eval}"
        print("   13d) 检索异常 → error 评估、不抛 ✓")

        # 13e) to_eval 映射（retry 前缀）
        assert to_eval(HitGrade(True, "relevant", "相关"), "q", False).reason == "ok"
        assert to_eval(HitGrade(True, "relevant", "相关"), "q", True).reason == "retried_ok"
        assert to_eval(HitGrade(False, "empty", "空"), "q", True).reason == "retried_empty"
        assert (
            to_eval(HitGrade(False, "irrelevant", "不相关"), "q", True).reason
            == "retried_irrelevant"
        )
        print("   13e) to_eval 映射（retry 前缀）✓")

    finally:
        # 还原单例，避免污染同进程内其它使用方
        chat_query_rewrite_service._rewriter = real_rewriter
        chat_query_rewrite_service._relevance_judge = real_judge
        chat_query_rewrite_service._retry_writer = real_retry_writer
        chat_query_rewrite_service._typesafe_client_cls = real_typesafe_cls
        chat_query_rewrite_service._typesafe_choice_cls = real_typesafe_choice_cls

    print("\n检索改写（追问 → 独立短查询）冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
