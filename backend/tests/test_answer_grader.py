"""生成侧判分：确定性部分 + 判分失败 fail-open

LLM 判分本身不在这里测（要真调模型、且天然有方差），只测：
  - 引用编号抽取（去重 / 保序）
  - 要点覆盖率（空白与大小写归一）
  - 忠实度换算
  - 未配 Key / 调用失败 → 返回 None（不把「判分失败」算成「回答错误」）
"""
from app.services.answer_grader import (
    AnswerGrade,
    AnswerGrader,
    citation_ok,
    extract_citations,
    fact_coverage,
    faithfulness_score,
)


def _grade(supported: list[str], unsupported: list[str], issues: list[str]) -> AnswerGrade:
    return AnswerGrade(
        supported_claims=supported,
        unsupported_claims=unsupported,
        citation_issues=issues,
        reason="test",
    )


def test_extract_citations_dedup_and_order() -> None:
    assert extract_citations("结论 [2]，依据 [1]，再次 [2]。") == [2, 1]
    assert extract_citations("这条回答没有引用") == []


def test_fact_coverage_normalizes_whitespace_and_case() -> None:
    # 「600 元」与「600元」应视为同一要点（生成时是否带空格不稳定）
    assert fact_coverage("住宿上限 600 元/晚", ["600 元"]) == 1.0
    assert fact_coverage("住宿上限600元/晚", ["600 元"]) == 1.0
    assert fact_coverage("ABC 流程", ["abc"]) == 1.0
    # 命中一半
    assert fact_coverage("只提了北京", ["北京", "上海"]) == 0.5


def test_fact_coverage_ignores_markdown_punctuation() -> None:
    """生成会加 markdown 修饰，比对前必须先剥标点

    实测踩过：`` `df` 命令 `` 与期望要点「df命令」因中间的反引号被判不命中，
    把「引用了正确命令」误计成漏点（一批 8 条因此显示覆盖率 0）。
    """
    assert fact_coverage("可以用 `df` 命令查看磁盘", ["df命令"]) == 1.0
    assert fact_coverage("**chmod** 用来变更权限", ["chmod"]) == 1.0
    assert fact_coverage("上班时间：09:04:10", ["09:04:10"]) == 1.0


def test_fact_coverage_empty_facts_is_perfect() -> None:
    """「不可回答」用例没有期望要点，不应被扣分"""
    assert fact_coverage("知识库里没有相关内容。", []) == 1.0


def test_faithfulness_score() -> None:
    assert faithfulness_score(_grade(["a", "b"], [], [])) == 1.0
    assert faithfulness_score(_grade(["a"], ["b"], [])) == 0.5
    # 一条断言都没提取到（如拒答）→ 无话可说即无幻觉
    assert faithfulness_score(_grade([], [], [])) == 1.0


def test_citation_ok() -> None:
    assert citation_ok(_grade(["a"], [], [])) is True
    assert citation_ok(_grade(["a"], [], ["[2] 指向的资料不支持该句"])) is False


async def test_grader_disabled_returns_none(monkeypatch) -> None:
    grader = AnswerGrader()
    monkeypatch.setattr(grader, "_judge", None)
    assert grader.is_enabled() is False
    assert await grader.grade("问题", "回答", ["资料"]) is None


async def test_grader_fail_open_on_error(monkeypatch) -> None:
    """判分器抛错不能让整批评测中断，应按 fail-open 返回 None"""

    class _Boom:
        async def ainvoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("judge down")

    grader = AnswerGrader()
    monkeypatch.setattr(grader, "_judge", _Boom())
    assert grader.is_enabled() is True
    assert await grader.grade("问题", "回答", ["资料"]) is None
