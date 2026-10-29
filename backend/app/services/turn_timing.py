"""一轮对话的分段耗时

**为什么需要**：TTFT 是个总数，拆不开只能对外报数、没法指导优化。
真正要回答的是「这几百毫秒花在哪一段、哪一段是我的代码」。

本模块只做计时，不碰 IO；`clock` 可注入，便于单测。

口径（写进日志时要跟这几个定义一致）：
  - 起点 = `stream_chat` 开始执行那一刻，不含 HTTP 建连与反代排队
  - `first_token` = 首个 `reasoning-delta` / `text-delta` 产生的时刻
    （用户屏幕上出现第一个字，不是 SSE 首帧 —— 首帧是 `start`，业务逻辑之前就发了）
  - 相邻 mark 之间不重叠，`stages` 累加近似等于 first_token
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["TurnTiming"]


class TurnTiming:
    """分段计时器：`mark()` 记一段，`first_token()` 记 TTFT"""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._started = clock()
        self._last = self._started
        # 段名 → 毫秒；同名重复 mark 会累加（如 agent 循环里多次检索）
        self.stages: dict[str, float] = {}
        self.first_token_ms: float | None = None

    def mark(self, stage: str) -> float:
        """记录「距上一次 mark」的耗时（毫秒），返回这一段的值"""
        now = self._clock()
        cost = round((now - self._last) * 1000, 1)
        self.stages[stage] = round(self.stages.get(stage, 0.0) + cost, 1)
        self._last = now
        return cost

    def first_token(self) -> None:
        """只在第一次生效：首个正文 / 思考 delta"""
        if self.first_token_ms is None:
            self.first_token_ms = round((self._clock() - self._started) * 1000, 1)

    @property
    def elapsed_ms(self) -> float:
        return round((self._clock() - self._started) * 1000, 1)

    def stages_text(self) -> str:
        """`session:12.3,history:4.1,...` —— 直接拼进一行日志，便于 grep"""
        return ",".join(f"{name}:{value:g}" for name, value in self.stages.items())
