"""rerank 入参不截断（回归守卫）

历史：`_MAX_DOC_CHARS = 2000` 是从 TS 版对齐来的硬截断，注释只写「与 TS 一致」，
从未验证过。实测库里 467 条 chunk，最长的 rerank 输入是 1093 字符 —— **该阈值
一次都没触发过**；而 DashScope qwen3.7-text-rerank 的单条上限是 30,000 token
（≈6 万字符），2000 只有真实上限的 1/30。

结论：长度应由 RAG_CHUNK_SIZE 保证，不在送 API 时做二次静默裁剪。
超长时打 WARN 让它可观测（API 会直接 400，不会自己截断）。
"""
import logging
from typing import Any

import app.services.reranker as reranker_module
from app.pipeline.types import ChunkHit
from app.services.reranker import RerankerService


def _hit(content: str) -> ChunkHit:
    return ChunkHit(
        chunkId="c1",
        documentId="d1",
        documentTitle="文档标题",
        content=content,
        heading="章节",
        score=0.5,
    )


def _service() -> RerankerService:
    """绕过 __init__（它依赖 settings），直接装配一个「已启用」的实例"""
    svc = object.__new__(RerankerService)
    svc.enabled = True
    svc.api_key = "test-key"
    svc.model = "test-rerank"
    svc.endpoint = "http://test.invalid/rerank"
    return svc


class _FakeResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, Any]:
        return {"output": {"results": [{"index": 0, "relevance_score": 0.93}]}}


class _FakeClient:
    """替身 client：捕获发出去的 payload，不发真实请求"""

    last_payload: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        _FakeClient.last_payload = json or {}
        return _FakeResponse()


def test_to_document_keeps_full_content() -> None:
    content = "甲" * 3000
    doc = RerankerService._to_document(_hit(content))
    assert content in doc
    assert not doc.endswith("..."), "不应再追加截断省略号"
    assert len(doc) > 3000


async def test_payload_sends_full_text(monkeypatch: Any) -> None:
    tail = "<<答案在尾部>>"
    content = "乙" * 2500 + tail
    monkeypatch.setattr(reranker_module.httpx, "AsyncClient", _FakeClient)

    out = await _service().rerank("问题", [_hit(content)], 1)

    sent = _FakeClient.last_payload["input"]["documents"][0]
    assert tail in sent, "rerank 入参不能丢正文尾部"
    assert content in sent
    assert out and out[0].score == 0.93


async def test_oversized_input_warns_but_not_truncated(
    monkeypatch: Any, caplog: Any
) -> None:
    """超长只告警、不裁剪 —— 让 chunk 配置失控变得可观测"""
    content = "丙" * (reranker_module._WARN_DOC_CHARS + 500)
    monkeypatch.setattr(reranker_module.httpx, "AsyncClient", _FakeClient)

    with caplog.at_level(logging.WARNING, logger="RerankerService"):
        await _service().rerank("问题", [_hit(content)], 1)

    assert "输入异常长" in caplog.text
    sent = _FakeClient.last_payload["input"]["documents"][0]
    assert content in sent, "告警不代表可以截断"
