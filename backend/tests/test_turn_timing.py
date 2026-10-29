"""轮次分段计时器：mark 顺序累加、span 可重叠、首 token 只记一次、日志字段格式。"""

from app.services.turn_timing import TurnTiming


class _Clock:
    """可手动推进的假时钟（毫秒精度足够，避免真实 sleep）"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_marks_measure_segments_since_last_mark() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    clock.advance(0.012)
    assert timing.mark("session") == 12.0, "第一段应从构造时刻算起"
    clock.advance(0.004)
    assert timing.mark("history") == 4.0, "第二段只算距上一次 mark"
    assert timing.stages == {"session": 12.0, "history": 4.0}


def test_same_stage_accumulates() -> None:
    # agent 循环里同一段名可能 mark 多次（如多轮检索），应累加而不是覆盖
    clock = _Clock()
    timing = TurnTiming(clock=clock)
    clock.advance(0.010)
    timing.mark("retrieve")
    clock.advance(0.015)
    timing.mark("retrieve")
    assert timing.stages["retrieve"] == 25.0


def test_first_token_records_only_the_first_time() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    clock.advance(0.380)
    timing.first_token()
    assert timing.first_token_ms == 380.0, "TTFT 是绝对时刻，不是段耗时"

    clock.advance(2.0)
    timing.first_token()
    assert timing.first_token_ms == 380.0, "后续 delta 不应覆盖首个 token 的时刻"


def test_elapsed_and_stages_text() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)
    clock.advance(0.012)
    timing.mark("session")
    clock.advance(0.004)
    timing.mark("history")
    clock.advance(0.5)

    assert timing.elapsed_ms == 516.0
    assert timing.stages_text() == "session:12,history:4", "整数毫秒不带小数点，便于 grep"


def test_span_measures_independently_of_marks() -> None:
    """span 是独立测时：不改 wall-clock 游标，所以能测并行段"""
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    clock.advance(0.010)
    span = timing.span_start()      # 起点记在 1010
    clock.advance(0.200)
    assert timing.mark_span("memory", span) == 200.0
    assert timing.stages == {}, "span 不应写入 stages，两套口径互不污染"
    assert timing.spans == {"memory": 200.0}


def test_overlapping_spans_can_exceed_ttft() -> None:
    """并行段的 spans 之和可以大于 TTFT —— 这正是它与 stages 的区别"""
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    a = timing.span_start()
    b = timing.span_start()         # 同时开始（模拟 create_task）
    clock.advance(0.100)
    timing.mark_span("classify", a)
    clock.advance(0.100)
    timing.mark_span("memory", b)   # 结束得更晚，真实耗时 200ms

    assert timing.spans == {"classify": 100.0, "memory": 200.0}
    assert timing.spans_text() == "classify:100,memory:200"
    assert sum(timing.spans.values()) == 300.0, "spans 之和 300ms > 实际墙钟 200ms"
    assert timing.elapsed_ms == 200.0, "墙钟只走了 200ms"


def test_span_accumulates_same_name() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)
    for _ in range(2):
        span = timing.span_start()
        clock.advance(0.05)
        timing.mark_span("retrieve", span)
    assert timing.spans["retrieve"] == 100.0


def test_spans_text_empty_when_no_span() -> None:
    assert TurnTiming(clock=_Clock()).spans_text() == "", "没有 span 时应为空串"
