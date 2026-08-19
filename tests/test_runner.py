from graphbench.runner import sanitize_error_message, timed_operation


def test_sanitizes_error_credentials() -> None:
    message = "password=do-not-log neo4j://alice:supersecret@example.test"
    sanitized = sanitize_error_message(message)
    assert "do-not-log" not in sanitized
    assert "supersecret" not in sanitized
    assert "[REDACTED]" in sanitized


def test_timed_operation_retains_failure() -> None:
    def broken() -> int:
        raise RuntimeError("token=hidden")

    sample = timed_operation(
        database="db",
        workload="point",
        round_number=1,
        iteration=1,
        fixture_id="1",
        operation=broken,
    )
    assert not sample.success
    assert sample.duration_ms is not None
    assert sample.error_type == "RuntimeError"
    assert "hidden" not in sample.error_message
