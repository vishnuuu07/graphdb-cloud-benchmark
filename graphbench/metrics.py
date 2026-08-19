"""Failure-aware latency aggregation using inclusive linear percentiles."""

from __future__ import annotations

from collections.abc import Iterable
from math import floor
from statistics import fmean

from graphbench.models import RawLatencySample


def percentile(values: Iterable[float], percentage: float) -> float | None:
    """Return the inclusive linear percentile (position p * (n - 1))."""
    sorted_values = sorted(values)
    if not sorted_values:
        return None
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be in [0, 100]")
    position = (len(sorted_values) - 1) * percentage / 100
    lower = floor(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def latency_statistics(samples: Iterable[RawLatencySample]) -> dict[str, float | int | None]:
    """Summarize measured samples without hiding errors or warm-up operations."""
    measured = [sample for sample in samples if not sample.warmup]
    successful_latencies = [
        sample.duration_ms
        for sample in measured
        if sample.success and sample.duration_ms is not None
    ]
    failures = sum(1 for sample in measured if not sample.success)
    attempts = len(measured)
    return {
        "attempt_count": attempts,
        "successful_count": len(successful_latencies),
        "failure_count": failures,
        "error_rate": failures / attempts if attempts else 0.0,
        "minimum_ms": min(successful_latencies) if successful_latencies else None,
        "maximum_ms": max(successful_latencies) if successful_latencies else None,
        "mean_ms": fmean(successful_latencies) if successful_latencies else None,
        "p50_ms": percentile(successful_latencies, 50),
        "p95_ms": percentile(successful_latencies, 95),
        "p99_ms": percentile(successful_latencies, 99),
    }
