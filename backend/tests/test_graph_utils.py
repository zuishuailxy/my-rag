"""图谱纯工具函数：实体名归一 / 关键词拆分 / 命中合并 / 出处片段 / 上下文格式化。"""
from app.services.graph_build import (
    GraphChatHit,
    _evidence_snippet,
    format_graph_context,
    merge_graph_hits,
    normalize_entity_name,
    split_graph_keywords,
)


def test_normalize_entity_name() -> None:
    assert normalize_entity_name("《发票管理办法》") == "发票管理办法"
    assert normalize_entity_name("  发票  ") == "发票"
    assert normalize_entity_name("") == ""
    assert normalize_entity_name(None) == ""


def test_split_graph_keywords() -> None:
    assert split_graph_keywords("发票 报销/《差旅》") == ["发票", "报销", "差旅"]
    # 去重（大小写不敏感）
    assert split_graph_keywords(["报销", "报销"]) == ["报销"]


def test_merge_graph_hits_dedup() -> None:
    def hit(query: str, names: list[str]) -> GraphChatHit:
        return {
            "query": query,
            "entities": [{"name": n, "type": None, "description": None} for n in names],
            "relations": [],
            "documents": [],
        }

    merged = merge_graph_hits(hit("差旅", ["差旅"]), hit("审批", ["差旅", "审批"]))
    assert [e["name"] for e in merged["entities"]] == ["差旅", "审批"]
    assert merged["query"] == "差旅 / 审批"


def test_evidence_snippet_window() -> None:
    content = "A" * 500 + "私车公用申请表" + "B" * 500
    ev = _evidence_snippet(content, "私车公用申请表")
    assert ev and "私车公用申请表" in ev, "应取实体附近窗口"
    assert len(ev) < len(content), "应截断，不是整块"
    assert _evidence_snippet(None, "x") is None


def test_format_graph_context_empty() -> None:
    empty: GraphChatHit = {
        "query": "q",
        "entities": [],
        "relations": [],
        "documents": [],
    }
    assert format_graph_context(empty) == ""


def test_format_graph_context_with_evidence() -> None:
    hit: GraphChatHit = {
        "query": "q",
        "entities": [{"name": "差旅", "type": "CONCEPT", "description": None}],
        "relations": [
            {"source": "差旅", "relation": "需要", "target": "审批", "evidence": "差旅需审批"}
        ],
        "documents": [{"documentId": "d1", "title": "差旅办法"}],
    }
    ctx = format_graph_context(hit)
    assert "差旅 → 审批（需要）" in ctx
    assert "出处：差旅需审批" in ctx
    assert "来源文档：差旅办法" in ctx
