"""rerank 降级兜底（纯逻辑，mock 掉混合检索与 reranker）

验证：rerank 不可用时按**向量余弦相似度**兜底过滤，而不是原样返回 RRF 结果
（否则阈值整体失效、无关问题全部漏进来）；显式关过滤（minScore <= 0）时不介入；
rerank 正常时走原 rerank 路径，兜底门槛不参与。
"""
from dataclasses import replace

import app.services.retrieval as retrieval
from app.pipeline.types import ChunkHit
from app.services.retrieval import HybridRetrievalService


def _row(cid: str, vector_score: float, rrf: float = 0.03) -> dict:
    return {
        "chunkId": cid,
        "documentId": "d-" + cid,
        "documentTitle": "标题-" + cid,
        "content": "正文",
        "heading": None,
        "score": rrf,
        "bm25Score": 1.0,
        "vectorScore": vector_score,
    }


async def _retrieve(
    monkeypatch,
    rows: list[dict],
    rerank=None,
    min_score: float = 0.7,
    min_vector_score: float = 0.37,
) -> list[dict]:
    async def fake_hybrid(*args, **kwargs):  # noqa: ANN002, ANN003
        return list(rows)

    async def fake_rerank(query, candidates, top_n):  # noqa: ANN001
        return rerank(candidates) if callable(rerank) else rerank

    monkeypatch.setattr(retrieval, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(retrieval.reranker_service, "rerank", fake_rerank)
    service = HybridRetrievalService(
        min_score=min_score, min_vector_score=min_vector_score
    )
    return await service.retrieve("问题", 5, None)


async def test_rerank_down_filters_by_vector_score(monkeypatch) -> None:
    """rerank 返回 None（降级）：低于兜底门槛的候选被过滤"""
    rows = [_row("relevant", 0.55), _row("noise", 0.22)]
    got = await _retrieve(monkeypatch, rows)
    assert [g["chunkId"] for g in got] == ["relevant"]


async def test_rerank_down_all_below_floor_returns_empty(monkeypatch) -> None:
    """全部候选都低于兜底门槛 → 返回空（对齐「召回不到相关资料」）"""
    rows = [_row("noise1", 0.30), _row("noise2", 0.21)]
    assert await _retrieve(monkeypatch, rows) == []


async def test_min_score_zero_disables_fallback(monkeypatch) -> None:
    """显式关过滤（minScore <= 0）时不介入，保持 RRF 原顺序"""
    rows = [_row("relevant", 0.55), _row("noise", 0.22)]
    got = await _retrieve(monkeypatch, rows, min_score=0.0)
    assert [g["chunkId"] for g in got] == ["relevant", "noise"]


async def test_rerank_and_vector_both_down_keeps_rrf(monkeypatch) -> None:
    """rerank 与向量通道**同时**挂掉时，不能把候选滤空

    兜底门控依赖向量分；向量通道挂了（欠费 / 网络）时 vectorScore 全为 0，
    若照旧门控会把候选全部滤掉 → 整体召回变成 0，比不兜底还差。
    """
    rows = [_row("a", 0.0), _row("b", 0.0)]
    got = await _retrieve(monkeypatch, rows)
    assert [g["chunkId"] for g in got] == ["a", "b"], "向量无数据时应退回 RRF 原序"


async def test_rerank_ok_uses_rerank_path(monkeypatch) -> None:
    """rerank 正常时走 rerank 分数过滤，兜底门槛不参与

    两个候选的向量分都远低于兜底门槛（0.05），但 rerank 给 b 打 0.9：
    若兜底门槛被误用，b 也会被丢掉。
    """
    rows = [_row("a", 0.05), _row("b", 0.05)]

    def rerank(candidates: list[ChunkHit]) -> list[ChunkHit]:
        return [replace(hit, score=score) for hit, score in zip(candidates, [0.2, 0.9])]

    got = await _retrieve(monkeypatch, rows, rerank=rerank)
    assert [g["chunkId"] for g in got] == ["b"]
