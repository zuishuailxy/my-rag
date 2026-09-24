"""
雪花 ID 生成器

Twitter Snowflake 布局：41 位时间戳 + 10 位机器 ID + 12 位序列号 = 63 位，
落在 PostgreSQL BIGINT（有符号 64 位）范围内。
"""
import threading
import time


class SnowflakeIdGenerator:
    """线程安全的雪花 ID 生成器"""

    def __init__(self, machine_id: int = 1, epoch_ms: int = 1609459200000) -> None:
        # 10 位机器 ID，默认 1；epoch 取 2021-01-01 00:00:00 UTC
        self._machine_id = machine_id & 0x3FF
        self._epoch = epoch_ms
        self._sequence = 0
        self._last_ts = -1
        self._lock = threading.Lock()

    def next_id(self) -> int:
        with self._lock:
            ts = int(time.time() * 1000)
            if ts < self._last_ts:
                raise RuntimeError("Clock moved backwards")
            if ts == self._last_ts:
                self._sequence = (self._sequence + 1) & 0xFFF
                if self._sequence == 0:
                    # 同毫秒序列号耗尽，等待进入下一毫秒
                    while ts <= self._last_ts:
                        ts = int(time.time() * 1000)
            else:
                self._sequence = 0
            self._last_ts = ts
            return ((ts - self._epoch) << 22) | (self._machine_id << 12) | self._sequence


# 进程级单例
next_snowflake_id = SnowflakeIdGenerator().next_id
