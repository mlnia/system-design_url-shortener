import threading

import pytest

from app.snowflake import EPOCH_MS, MAX_SEQUENCE, ClockMovedBackwardsError, SnowflakeGenerator


def test_ids_are_unique_and_increasing():
    gen = SnowflakeGenerator(3, 7)
    ids = [gen.next_id() for _ in range(20_000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    assert all(0 < i < 2**63 for i in ids)


def test_parse_roundtrip():
    gen = SnowflakeGenerator(5, 9, clock=lambda: EPOCH_MS + 123_456)
    parts = SnowflakeGenerator.parse(gen.next_id())
    assert parts == {"timestamp_ms": EPOCH_MS + 123_456, "datacenter_id": 5, "machine_id": 9, "sequence": 0}


def test_sequence_overflow_waits_for_next_millisecond():
    now = EPOCH_MS + 1_000
    gen = SnowflakeGenerator(0, 0, clock=lambda: now)
    ids = [gen.next_id() for _ in range(MAX_SEQUENCE + 1)]
    assert SnowflakeGenerator.parse(ids[-1])["sequence"] == MAX_SEQUENCE

    # Next call sees the same ms (sequence exhausted) and must spin until the clock ticks.
    readings = iter([now, now, now + 1])
    gen._clock = lambda: next(readings)
    parts = SnowflakeGenerator.parse(gen.next_id())
    assert parts["timestamp_ms"] == now + 1
    assert parts["sequence"] == 0


def test_large_clock_rollback_raises():
    readings = iter([EPOCH_MS + 10_000, EPOCH_MS + 5_000])
    gen = SnowflakeGenerator(0, 0, clock=lambda: next(readings))
    gen.next_id()
    with pytest.raises(ClockMovedBackwardsError):
        gen.next_id()


def test_thread_safety():
    gen = SnowflakeGenerator(1, 1)
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        local = [gen.next_id() for _ in range(2_000)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 16_000


@pytest.mark.parametrize(("dc", "machine"), [(-1, 0), (32, 0), (0, -1), (0, 32)])
def test_rejects_out_of_range_worker_ids(dc, machine):
    with pytest.raises(ValueError):
        SnowflakeGenerator(dc, machine)
