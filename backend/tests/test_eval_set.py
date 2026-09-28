"""评测集完整性：eval/rag_eval.jsonl 可解析、id 唯一、字段齐全。"""
import json
from pathlib import Path

EVAL_FILE = Path(__file__).resolve().parents[1] / "eval" / "rag_eval.jsonl"


def _load() -> list[dict]:
    lines = [ln for ln in EVAL_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_eval_set_integrity() -> None:
    cases = _load()
    assert len(cases) >= 60, "评测集至少 60 条"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "id 必须唯一"
    for case in cases:
        assert case["question"].strip(), "question 不能为空"
        assert isinstance(case["expect_documents"], list)
        assert case.get("category"), "每条都应有 category"


def test_eval_set_has_positive_and_negative() -> None:
    cases = _load()
    positive = [c for c in cases if c["expect_documents"]]
    negative = [c for c in cases if not c["expect_documents"]]
    assert len(positive) >= 30, "正例不足"
    assert len(negative) >= 5, "负例不足（用于验证空召回率）"
    for case in positive:
        assert case["expect_keywords"], "正例应给 keyword（供后续来源/忠实度校验）"


def test_eval_set_has_hard_cases() -> None:
    """难题（近义改写 / 避开目标文档词汇）要够量，否则指标会被简单题淹没"""
    cases = _load()
    hard = [c for c in cases if c.get("difficulty") == "hard"]
    assert len(hard) >= 10, "难题不足，检索指标会失去区分度"
    assert all(c["expect_documents"] for c in hard), "难题应均为正例（可判定命中）"
