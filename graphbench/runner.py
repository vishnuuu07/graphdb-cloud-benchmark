"""Timing helpers for future concrete benchmark execution."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

from graphbench.models import RawLatencySample

Result = TypeVar("Result")
_SECRET_PATTERN = re.compile(r"(?i)(password|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+")
_URI_CREDENTIAL_PATTERN = re.compile(r"//[^/@\s:]+:[^/@\s]+@")


def sanitize_error_message(message: str) -> str:
    """Keep diagnostic context while redacting common credentials and URI userinfo."""
    sanitized = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    return _URI_CREDENTIAL_PATTERN.sub("//[REDACTED]@", sanitized)


def timed_operation(
    *,
    database: str,
    workload: str,
    round_number: int,
    iteration: int,
    fixture_id: str,
    operation: Callable[[], Result],
    result_count: Callable[[Result], int] | None = None,
    warmup: bool = False,
) -> RawLatencySample:
    """Time one already-connected operation with perf_counter_ns and preserve failures."""
    started = time.perf_counter_ns()
    try:
        result = operation()
    except Exception as exc:  # adapters can raise third-party driver exceptions
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        return RawLatencySample(
            database=database,
            workload=workload,
            round=round_number,
            iteration=iteration,
            fixture_id=fixture_id,
            duration_ms=duration_ms,
            success=False,
            error_type=type(exc).__name__,
            error_message=sanitize_error_message(str(exc)),
            warmup=warmup,
        )
    duration_ms = (time.perf_counter_ns() - started) / 1_000_000
    return RawLatencySample(
        database=database,
        workload=workload,
        round=round_number,
        iteration=iteration,
        fixture_id=fixture_id,
        duration_ms=duration_ms,
        success=True,
        result_count=result_count(result) if result_count else None,
        warmup=warmup,
    )
