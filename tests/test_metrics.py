from graphbench.metrics import latency_statistics, percentile
from graphbench.models import RawLatencySample


def sample(duration: float | None, success: bool = True, warmup: bool = False) -> RawLatencySample:
    return RawLatencySample("db", "lookup", 1, 1, "fixture", duration, success, warmup=warmup)


def test_percentiles_use_inclusive_linear_method() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 3.85


def test_failures_and_warmups_remain_visible_but_excluded_from_latency() -> None:
    stats = latency_statistics(
        [sample(1.0), sample(99.0, warmup=True), sample(2.0), sample(None, False)]
    )
    assert stats["attempt_count"] == 3
    assert stats["successful_count"] == 2
    assert stats["failure_count"] == 1
    assert stats["p50_ms"] == 1.5
    assert stats["error_rate"] == 1 / 3
