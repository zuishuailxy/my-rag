"""检索切题评估 grade_hits：命中外包成 HitGrade，判定器可注入、失败放行。

通过注入假 `_relevance_judge`（并把 TypeSafe 关掉）隔离 LLM / 网络。
"""
from typing import Any

from app.pipeline.types import ChunkHit
from app.services.chat_query_rewrite import (
    ChatQueryRewriteService,
    RelevanceResult,
)


class _FakeJudge:
    def __init__(
        self, relevant: bool = True, reason: str = "r", error: Exception | None = None
    ) -> None:
        self._relevant = relevant
        self._reason = reason
        self._error = error
        self.called = False

    async def ainvoke(self, messages: list[Any], config: Any = None) -> RelevanceResult:
        self.called = True
        if self._error is not None:
            raise self._error
        return RelevanceResult(relevant=self._relevant, reason=self._reason)


def _service(judge: Any) -> ChatQueryRewriteService:
    svc = ChatQueryRewriteService()
    # 关掉 TypeSafe，强制走注入的 LLM 判定器
    svc._typesafe_client_cls = None
    svc._typesafe_choice_cls = None
    svc._relevance_judge = judge
    return svc


def _hit() -> ChunkHit:
    return ChunkHit(
        chunkId="c",
        documentId="d",
        documentTitle="差旅办法",
        content="差旅需审批",
        heading=None,
        score=0.2,
    )


async def test_grade_hits_empty_skips_judge() -> None:
    judge = _FakeJudge(True)
    grade = await _service(judge).grade_hits("q", [])
    assert grade.ok is False and grade.reason == "empty"
    assert judge.called is False, "空召回不应调判定器"


async def test_grade_hits_relevant_uses_reason_as_text() -> None:
    judge = _FakeJudge(relevant=True, reason="资料与问题相关")
    grade = await _service(judge).grade_hits("q", [_hit()])
    assert grade.ok is True and grade.reason == "relevant"
    assert grade.text == "资料与问题相关"


async def test_grade_hits_irrelevant() -> None:
    judge = _FakeJudge(relevant=False, reason="资料与问题不相关")
    grade = await _service(judge).grade_hits("q", [_hit()])
    assert grade.ok is False and grade.reason == "irrelevant"


async def test_grade_hits_fail_open_when_judge_unavailable() -> None:
    # 未配置 TypeSafe 且无 LLM 判定器 → None → 按切题放行，不阻断作答
    grade = await _service(None).grade_hits("q", [_hit()])
    assert grade.ok is True and grade.reason == "relevant"


async def test_grade_hits_fail_open_on_judge_error() -> None:
    judge = _FakeJudge(error=RuntimeError("模拟超时"))
    grade = await _service(judge).grade_hits("q", [_hit()])
    assert grade.ok is True and grade.reason == "relevant"
