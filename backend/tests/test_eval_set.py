"""评测集完整性：eval/rag_eval.jsonl 可解析、id 唯一、字段齐全。"""
import json
from pathlib import Path

EVAL_FILE = Path(__file__).resolve().parents[1] / "eval" / "rag_eval.jsonl"
GEN_FILE = Path(__file__).resolve().parents[1] / "eval" / "gen_eval.jsonl"


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
    # 多跳用例期望两篇文档，「单一要点列表」对它不适用，所以只要求单跳正例给 keyword
    for case in positive:
        if case.get("multi_hop"):
            continue
        assert case["expect_keywords"], "单跳正例应给 keyword（供来源/忠实度校验）"


def test_multi_hop_cases_are_well_formed() -> None:
    """多跳用例必须期望 ≥2 篇文档 —— 否则它根本测不到「跨文档」能力

    语义提醒：单跳命中判定是 any-of，多跳是 **all-of**；
    评测脚本按 `multi_hop` 字段把两者分开统计，误标会让指标相互掩盖。
    """
    cases = _load()
    multi = [c for c in cases if c.get("multi_hop")]
    assert multi, "应有多跳用例"
    for case in multi:
        assert len(case["expect_documents"]) >= 2, f"{case['id']} 只期望 1 篇，称不上多跳"
        assert case["category"] == "multi_hop", f"{case['id']} 的 category 应为 multi_hop"


def test_gen_eval_set_integrity() -> None:
    """生成侧评测集：可回答问题必须给期望要点，不可回答问题必须没有要点"""
    lines = [ln for ln in GEN_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    cases = [json.loads(ln) for ln in lines]
    assert len(cases) >= 10, "生成侧评测集至少 10 条"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "id 必须唯一"

    answerable = [c for c in cases if c["answerable"]]
    unanswerable = [c for c in cases if not c["answerable"]]
    assert answerable, "需有可回答问题"
    assert unanswerable, "需有不可回答问题（验证拒答率）"
    for case in answerable:
        assert case["expect_facts"], "可回答问题必须给期望要点（算要点覆盖率）"
        assert case["expect_documents"], "可回答问题应有期望文档"
    for case in unanswerable:
        assert not case["expect_facts"], "不可回答问题不应有期望要点"


def test_eval_set_has_hard_cases() -> None:
    """难题（近义改写 / 避开目标文档词汇）要够量，否则指标会被简单题淹没"""
    cases = _load()
    hard = [c for c in cases if c.get("difficulty") == "hard"]
    assert len(hard) >= 10, "难题不足，检索指标会失去区分度"
    assert all(c["expect_documents"] for c in hard), "难题应均为正例（可判定命中）"
