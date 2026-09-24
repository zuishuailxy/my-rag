"""检索评估 → 前端 RetrieveEval 的映射。"""
from app.services.agentic_retrieve import to_eval
from app.services.chat_query_rewrite import HitGrade


def test_to_eval_reason_mapping() -> None:
    assert to_eval(HitGrade(True, "relevant", "相关"), "q", False).reason == "ok"
    assert to_eval(HitGrade(True, "relevant", "相关"), "q", True).reason == "retried_ok"
    assert to_eval(HitGrade(False, "empty", "空"), "q", False).reason == "empty"
    assert to_eval(HitGrade(False, "empty", "空"), "q", True).reason == "retried_empty"
    assert to_eval(HitGrade(False, "irrelevant", "不相关"), "q", False).reason == "irrelevant"
    assert (
        to_eval(HitGrade(False, "irrelevant", "不相关"), "q", True).reason
        == "retried_irrelevant"
    )


def test_to_eval_payload_camel_case() -> None:
    payload = to_eval(HitGrade(True, "relevant", "相关"), "q1", True, "q2").to_payload()
    assert payload["ok"] is True
    assert payload["query"] == "q1"
    assert payload["retryQuery"] == "q2", "前端契约是 camelCase retryQuery"
