"""查询改写的纯函数：意图→工具开关、评估上下文不截断、门控输入口径。"""
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.pipeline.types import ChunkHit
from app.services.chat_query_rewrite import (
    ChatQueryRewriteService,
    RewriteResult,
    summarize_hits,
)


def test_to_plan_kb_keeps_graph_queries() -> None:
    plan = ChatQueryRewriteService._to_plan("kb", "q", [" 发票 ", "报销", ""])
    assert plan["graph_queries"] == ["发票", "报销"], "应去空白并保留"
    assert plan["allow_retrieve"] is True and plan["allow_graph"] is True
    assert plan["allow_web"] is False


def test_to_plan_non_kb_clears_graph_queries() -> None:
    plan = ChatQueryRewriteService._to_plan("web", "q", ["发票"])
    assert plan["graph_queries"] == [], "非 kb 意图应清空图谱词"
    assert plan["allow_retrieve"] is False and plan["allow_graph"] is False
    assert plan["allow_web"] is True


def test_to_plan_chitchat_no_tools() -> None:
    plan = ChatQueryRewriteService._to_plan("chitchat", "谢谢")
    assert not plan["allow_retrieve"] and not plan["allow_graph"] and not plan["allow_web"]


def test_summarize_hits_keeps_full_content() -> None:
    """关键句在 chunk 中后段时不能被截掉

    回归守卫（两次）：曾截 180 —— 评估器只看到开头的「差旅管理办法」标题 →
    误判不切题 → 合法命中被丢 → 回答「知识库无内容」；改 1200 后只是「恰好够」，
    chunk size 一调就重新截。现在改为不截断，长度上限交给 RAG_CHUNK_SIZE。
    """
    tail = "私车公用需填写《私车公用申请表》。"
    hit = ChunkHit(
        chunkId="c",
        documentId="d",
        documentTitle="差旅与费用报销管理办法",
        # 约 3300 字，远超历史上任何一个截断阈值
        content=("出差交通费按职级报销。" * 300) + tail,
        heading="交通费",
        score=0.2,
    )
    out = summarize_hits([hit])
    assert tail in out, "正文尾部必须保留（不再按长度截断）"
    assert hit.documentTitle in out
    # 压空白仍生效：连空白被压成单空格（不是截断，是有意的扁平化）
    assert "  " not in out


def test_summarize_hits_flattens_whitespace() -> None:
    """压空白保留：省 token 且把表格/多行文本压平，与截断无关"""
    hit = ChunkHit(
        chunkId="c",
        documentId="d",
        documentTitle="表格文档",
        content="第一行\n\n\n第二行\t\t第三行",
        heading=None,
        score=0.5,
    )
    out = summarize_hits([hit])
    assert "第一行 第二行 第三行" in out


class _FakeChoice:
    """TypeSafe Choice 原语的替身：只保留 instructions / criteria 供断言"""

    def __init__(self, instructions: str, criteria: dict[str, Any]) -> None:
        self.instructions = instructions
        self.criteria = criteria


class _FakeAnswer:
    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.confidence = 0.9
        self.probabilities: dict[str, float] = {}


class _FakeResponse:
    def __init__(self, choices: dict[str, Any]) -> None:
        self.choices = choices


class _FakeTypeSafeClient:
    """记录 state、按 key 返回可配置选项：用于验证判定链路的输入口径"""

    states: list[dict[str, Any]] = []
    answers: dict[str, str] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeTypeSafeClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def system_one(
        self, state: dict[str, Any], questions: dict[str, Any]
    ) -> _FakeResponse:
        type(self).states.append(dict(state))
        return _FakeResponse(
            {
                name: _FakeAnswer(self.answers.get(name, "standalone"))
                for name in questions
            }
        )


class _FakeRewriter:
    """记录改写器的 messages，用来断言它到底看到了多宽的历史"""

    def __init__(self) -> None:
        self.messages: list[Any] | None = None

    async def ainvoke(
        self, messages: list[Any], config: dict[str, Any] | None = None
    ) -> RewriteResult:
        self.messages = messages
        return RewriteResult(
            standalone_query="年假申请天数", need_retrieve=True, graph_queries=["年假"]
        )


async def test_rewrite_gate_does_not_consume_history() -> None:
    # 门控只判「这句话自身有没有指代/省略」：不吃历史，
    # 避免历史里出现「这个」把自足问题误判成依赖上文（面试稿 Q12 的设计口径）
    service = ChatQueryRewriteService.__new__(ChatQueryRewriteService)
    service._typesafe_client_cls = _FakeTypeSafeClient
    service._typesafe_choice_cls = _FakeChoice
    _FakeTypeSafeClient.states.clear()

    await service._route_rewrite_gate("那住宿标准呢")
    assert _FakeTypeSafeClient.states[-1] == {"question": "那住宿标准呢"}


async def test_classify_rewriter_sees_history_beyond_four_turns() -> None:
    # 窗口放宽后的核心价值：指代对象在 4 轮之外，改写器仍要能看到
    history: list[Any] = [
        HumanMessage("年假需要提前几天申请"),
        AIMessage("年假需提前 3 个工作日申请。"),
    ]
    for i in range(4):
        history.append(HumanMessage(f"无关话题{i}"))
        history.append(AIMessage(f"无关回答{i}"))
    history.append(HumanMessage("那调休呢"))

    service = ChatQueryRewriteService.__new__(ChatQueryRewriteService)
    service._typesafe_client_cls = _FakeTypeSafeClient
    service._typesafe_choice_cls = _FakeChoice
    rewriter = _FakeRewriter()
    service._rewriter = rewriter
    _FakeTypeSafeClient.answers = {"intent": "kb", "rewrite_gate": "needs_context"}
    _FakeTypeSafeClient.states.clear()

    plan = await service.classify("那调休呢", history)
    assert plan["intent"] == "kb"
    assert rewriter.messages is not None, "依赖上文应调 LLM 改写"
    prompt = rewriter.messages[1].content
    assert "年假需要提前几天申请" in prompt, "4 轮之前的指代对象必须进入改写上下文"
