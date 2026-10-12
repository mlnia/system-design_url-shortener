"""Twitter Snowflake style 64-bit unique ID generator (System Design Interview, ch. 7).

Layout (most significant bit first):

    | 1 bit sign (0) | 41 bits timestamp ms | 5 bits datacenter | 5 bits machine | 12 bits sequence |
"""

import threading
import time
from collections.abc import Callable

EPOCH_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z

TIMESTAMP_BITS = 41
DATACENTER_BITS = 5
MACHINE_BITS = 5
SEQUENCE_BITS = 12

MAX_TIMESTAMP = (1 << TIMESTAMP_BITS) - 1
MAX_DATACENTER_ID = (1 << DATACENTER_BITS) - 1
MAX_MACHINE_ID = (1 << MACHINE_BITS) - 1
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1

MACHINE_SHIFT = SEQUENCE_BITS
DATACENTER_SHIFT = SEQUENCE_BITS + MACHINE_BITS
TIMESTAMP_SHIFT = SEQUENCE_BITS + MACHINE_BITS + DATACENTER_BITS


class ClockMovedBackwardsError(RuntimeError):
    pass


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class SnowflakeGenerator:
    def __init__(
        self,
        datacenter_id: int,
        machine_id: int,
        *,
        epoch_ms: int = EPOCH_MS,
        clock: Callable[[], int] = _now_ms,
        max_backwards_ms: int = 10,
    ) -> None:
        if not 0 <= datacenter_id <= MAX_DATACENTER_ID:
            raise ValueError(f"datacenter_id must be in [0, {MAX_DATACENTER_ID}]")
        if not 0 <= machine_id <= MAX_MACHINE_ID:
            raise ValueError(f"machine_id must be in [0, {MAX_MACHINE_ID}]")
        self.datacenter_id = datacenter_id
        self.machine_id = machine_id
        self._epoch_ms = epoch_ms
        self._clock = clock
        self._max_backwards_ms = max_backwards_ms
        self._lock = threading.Lock()
        self._last_ts = -1
        self._sequence = 0

    def next_id(self) -> int:
        with self._lock:
            ts = self._clock()
            if ts < self._last_ts:
                drift = self._last_ts - ts
                if drift > self._max_backwards_ms:
                    raise ClockMovedBackwardsError(f"clock moved backwards by {drift} ms")
                # Small NTP adjustment: wait it out instead of risking duplicates.
                ts = self._wait_until(self._last_ts)

            if ts == self._last_ts:
                self._sequence = (self._sequence + 1) & MAX_SEQUENCE
                if self._sequence == 0:
                    # 4096 IDs already issued in this millisecond.
                    ts = self._wait_until(self._last_ts + 1)
            else:
                self._sequence = 0
            self._last_ts = ts

            elapsed = ts - self._epoch_ms
            if not 0 <= elapsed <= MAX_TIMESTAMP:
                raise RuntimeError("timestamp out of range for the configured epoch")
            return (
                (elapsed << TIMESTAMP_SHIFT)
                | (self.datacenter_id << DATACENTER_SHIFT)
                | (self.machine_id << MACHINE_SHIFT)
                | self._sequence
            )

    def _wait_until(self, target_ms: int) -> int:
        ts = self._clock()
        while ts < target_ms:
            time.sleep(0.0001)
            ts = self._clock()
        return ts

    @staticmethod
    def parse(snowflake_id: int, epoch_ms: int = EPOCH_MS) -> dict[str, int]:
        return {
            "timestamp_ms": (snowflake_id >> TIMESTAMP_SHIFT) + epoch_ms,
            "datacenter_id": (snowflake_id >> DATACENTER_SHIFT) & MAX_DATACENTER_ID,
            "machine_id": (snowflake_id >> MACHINE_SHIFT) & MAX_MACHINE_ID,
            "sequence": snowflake_id & MAX_SEQUENCE,
        }
