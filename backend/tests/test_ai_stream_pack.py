"""stream 侧纯逻辑：命中打包（全局编号 + 事件）与引用编号解析。"""
from app.pipeline.types import ChunkHit
from app.services.ai_stream import _CITATION_RE, _pack_retrieve_result, excerpt


class _CollectWriter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


def _hit(doc: str, idx: int = 0) -> ChunkHit:
    return ChunkHit(
        chunkId=f"c{idx}",
        documentId=f"d{doc}",
        documentTitle=f"文档-{doc}",
        content=f"正文-{doc}",
        heading="节",
        score=0.9,
    )


def test_pack_global_index_and_events() -> None:
    persist: list[dict] = []
    w1 = _CollectWriter()
    r1 = _pack_retrieve_result([_hit("A")], "q1", persist, w1, {"eval": {"ok": True}})
    assert [s["index"] for s in persist] == [1]
    assert r1["items"][0]["index"] == 1 and "[1]" in r1["context"]
    assert w1.events[0]["type"] == "data-sources"

    # 第二次检索：编号全局连续（2,3），data-sources 推全量
    w2 = _CollectWriter()
    r2 = _pack_retrieve_result([_hit("B"), _hit("C")], "q2", persist, w2)
    assert [s["index"] for s in persist] == [1, 2, 3]
    assert [s["index"] for s in r2["items"]] == [2, 3]
    assert w2.events[0]["type"] == "data-sources" and len(w2.events[0]["data"]) == 3
    assert [e["type"] for e in w2.events[1:]] == ["source-document", "source-document"]


def test_citation_regex() -> None:
    assert [m.group(1) for m in _CITATION_RE.finditer("见[1]和[3]，无[10]")] == ["1", "3", "10"]
    assert _CITATION_RE.findall("没有引用") == []


def test_excerpt_truncates() -> None:
    assert excerpt("  a   b  ") == "a b"
    assert excerpt("x" * 300).endswith("...")


def test_context_keeps_full_content() -> None:
    """正文全文进 context，不做 800 字符截断

    回归守卫：曾用 `_CONTEXT_SNIPPET_LEN = 800` 硬截（从 TS 版对齐而来）。
    实测库里 467 条 chunk，40% 超过 800 字符；用评测集 112 个答案关键词匹配，
    4.1% 的命中落在 800 字符之后 → 被静默切掉。长度上限由 RAG_CHUNK_SIZE 保证，
    不在拼接时二次裁剪。
    """
    tail = "<<答案在这>>"
    content = "中" * 1500 + tail
    hit = ChunkHit(
        chunkId="c-long",
        documentId="d1",
        documentTitle="长文",
        content=content,
        heading="节",
        score=0.9,
    )
    out = _pack_retrieve_result([hit], "q", [], _CollectWriter())

    assert tail in out["context"], "context 不能丢掉 800 字符之后的内容"
    assert content in out["context"], "正文应完整保留"
    # excerpt 仍然截断：它只用于前端溯源卡片展示，与送模型的正文无关
    assert len(out["items"][0]["excerpt"]) <= 203

