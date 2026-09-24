"""RRF 混合检索融合（纯逻辑，mock 掉两路召回与 PG 查询）

验证：两路都命中的 chunk 排在前面；各通道原始分透传；空 query 直接返回 []。
"""
import app.services.search as search


class _FakeMappings:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def all(self) -> list[dict]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeSession:
    """假 PG 会话：execute(...).mappings().all() 直接返回预置行"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute(self, *args, **kwargs) -> _FakeResult:  # noqa: ANN002, ANN003
        return _FakeResult(self._rows)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False


def _row(cid: str, title: str) -> dict:
    return {
        "chunk_id": cid,
        "document_id": "d-" + cid,
        "document_title": title,
        "content": "正文",
        "heading": None,
        "chunk_index": 0,
        "total_chunks": 1,
        "category_id": None,
        "author_id": None,
        "team_id": None,
        "doc_status": 1,
        "publish_time": None,
    }


async def test_rrf_common_chunk_ranks_first(monkeypatch) -> None:
    async def fake_vec(qv, limit, scope):  # noqa: ANN001
        return [("b", 0.9), ("c", 0.8)]

    async def fake_kw(q, limit, scope):  # noqa: ANN001
        return [("a", 5.0), ("b", 4.0)]

    rows = [_row("a", "A"), _row("b", "B"), _row("c", "C")]
    monkeypatch.setattr(search, "_vector_recall", fake_vec)
    monkeypatch.setattr(search, "_keyword_recall", fake_kw)
    monkeypatch.setattr(search, "async_session_factory", lambda: _FakeSession(rows))

    out = await search.hybrid_search(
        "q", top_k=10, query_vector=[0.1, 0.2], rrf_c=60
    )

    ids = [r["chunkId"] for r in out]
    assert ids[0] == "b", "两路都命中的 chunk RRF 应排第一"
    assert set(ids) == {"a", "b", "c"}
    b = next(r for r in out if r["chunkId"] == "b")
    assert b["bm25Score"] == 4.0 and b["vectorScore"] == 0.9, "各通道原始分应透传"


async def test_hybrid_search_empty_query_returns_empty() -> None:
    assert await search.hybrid_search("   ") == []


def test_clamp_bounds() -> None:
    assert search._clamp(0) == 1
    assert search._clamp(999) == 50
    assert search._clamp(7) == 7


def test_to_int() -> None:
    assert search._to_int("12") == 12
    assert search._to_int("") is None
    assert search._to_int("x") is None
