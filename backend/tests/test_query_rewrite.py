"""查询改写的纯函数：意图→工具开关、评估上下文截断。"""
from app.pipeline.types import ChunkHit
from app.services.chat_query_rewrite import (
    _RELEVANCE_SNIPPET_LEN,
    ChatQueryRewriteService,
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


def test_summarize_hits_keeps_tail() -> None:
    # 关键句在 chunk 中后段：截断过短会让评估器误判不切题（回归守卫）
    hit = ChunkHit(
        chunkId="c",
        documentId="d",
        documentTitle="差旅与费用报销管理办法",
        content=("出差交通费按职级报销。" * 60) + "私车公用需填写《私车公用申请表》。",
        heading="交通费",
        score=0.2,
    )
    assert _RELEVANCE_SNIPPET_LEN >= 800, "截断长度应能覆盖完整 chunk"
    assert "私车公用申请表" in summarize_hits([hit])
