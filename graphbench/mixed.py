"""Final mixed read/write concurrency campaign for the frozen final campaign."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from graphbench.adapters import create_adapter
from graphbench.config import load_final_profile, repository_root
from graphbench.environment import load_dotenv, sanitize_text
from graphbench.finalization import (
    DATABASES,
    LOCAL_DATABASES,
    _compose,
    _prepare_runtime_environment,
    _stop_local_databases,
    _wait_for_container,
    capture_environment,
    configuration_fingerprint,
    preflight,
)
from graphbench.metrics import latency_statistics
from graphbench.oracle import fixture_values
from graphbench.runner import timed_operation
from graphbench.workflows import expected_counts, prepare, validate_adapter

MIXED_RAW_NAME = "mixed_raw.jsonl"
MIXED_LEVELS = (1, 5, 10, 20, 40)
MAX_LEVEL_SECONDS = 120


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(dict.fromkeys(field for row in rows for field in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class _RawWriter:
    """Serialize samples on a dedicated thread, outside operation timing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="mixed-raw-writer", daemon=True)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def append(self, record: dict[str, Any]) -> None:
        self._items.put(record)

    def close(self) -> None:
        self._items.put(None)
        self.thread.join(timeout=60)
        if self.thread.is_alive():
            raise RuntimeError("mixed raw writer did not terminate within 60s")
        if self.error is not None:
            raise RuntimeError(f"mixed raw writer failed: {sanitize_text(str(self.error))}")

    def _run(self) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                while True:
                    item = self._items.get()
                    if item is None:
                        return
                    handle.write(json.dumps(item, sort_keys=True) + "\n")
        except BaseException as exc:  # surfaced by close(); do not lose persistence errors
            self.error = exc


@dataclass
class _InFlight:
    current: int = 0
    maximum: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def enter(self) -> None:
        with self.lock:
            self.current += 1
            self.maximum = max(self.maximum, self.current)

    def leave(self) -> None:
        with self.lock:
            self.current -= 1


@dataclass
class _Collector:
    samples: list[Any]
    lock: threading.Lock
    writer: _RawWriter
    campaign_id: str
    fingerprint: str
    database: str
    concurrency: int
    phase: str

    def add(
        self,
        sample: Any,
        *,
        category: str,
        workload: str,
        worker_id: int,
        sequence: int,
    ) -> None:
        record = asdict(sample)
        record.update(
            {
                "campaign_id": self.campaign_id,
                "configuration_fingerprint": self.fingerprint,
                "database": self.database,
                "concurrency": self.concurrency,
                "operation_category": category,
                "specific_workload": workload,
                "worker_id": worker_id,
                "operation_sequence": sequence,
                "phase": self.phase,
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        )
        with self.lock:
            self.samples.append(sample)
        self.writer.append(record)


def _fixture_order(seed: int, fixture_ids: list[int]) -> list[int]:
    import random

    rng = random.Random(seed)
    shuffled = list(fixture_ids)
    rng.shuffle(shuffled)
    return shuffled


def _operation_plan(fixture_order: list[int], sequence: int) -> tuple[str, str, int]:
    """Return the frozen 4-read/1-write prefix for a deterministic sequence."""
    # The seed determines a stable permutation of fixture values once per campaign.
    # Category selection is a five-slot prefix, giving the required 80/20 mix.
    category = "read" if sequence % 5 < 4 else "write"
    workload = "mixed_read" if category == "read" else "mixed_write"
    return category, workload, fixture_order[sequence % len(fixture_order)]


def _phase(
    *,
    adapter: Any,
    database: str,
    campaign_id: str,
    fingerprint: str,
    concurrency: int,
    phase: str,
    duration_seconds: int,
    seed: int,
    fixture_ids: list[int],
    writer: _RawWriter,
) -> tuple[list[Any], float, int, bool, str | None]:
    samples: list[Any] = []
    collector = _Collector(
        samples,
        threading.Lock(),
        writer,
        campaign_id,
        fingerprint,
        database,
        concurrency,
        phase,
    )
    stop = threading.Event()
    sequence = 0
    sequence_lock = threading.Lock()
    in_flight = _InFlight(lock=threading.Lock())
    fixture_order = _fixture_order(seed, fixture_ids)
    started = time.monotonic()
    deadline = started + duration_seconds

    def worker(worker_id: int) -> None:
        nonlocal sequence
        while not stop.is_set():
            if time.monotonic() >= deadline:
                return
            with sequence_lock:
                operation_sequence = sequence
                sequence += 1
            category, workload, fixture = _operation_plan(fixture_order, operation_sequence)
            in_flight.enter()
            try:
                operation = (
                    (lambda value=fixture: adapter.mixed_read(value))
                    if category == "read"
                    else (lambda value=fixture: adapter.mixed_write({"id": value}))
                )
                sample = timed_operation(
                    database=database,
                    workload=workload,
                    round_number=0 if phase == "warmup" else 1,
                    iteration=operation_sequence,
                    fixture_id=str(fixture),
                    operation=operation,
                    result_count=int,
                    warmup=phase == "warmup",
                )
                if sample.success and sample.result_count != 1:
                    sample = replace(
                        sample,
                        success=False,
                        error_type="CorrectnessMismatch",
                        error_message=(
                            f"{workload} expected one affected/matching User; "
                            f"observed {sample.result_count}"
                        ),
                    )
                collector.add(
                    sample,
                    category=category,
                    workload=workload,
                    worker_id=worker_id,
                    sequence=operation_sequence,
                )
            finally:
                in_flight.leave()

    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="mixed-worker")
    futures = [executor.submit(worker, worker_id) for worker_id in range(concurrency)]
    watchdog = duration_seconds + (5 if phase == "warmup" else 30)
    done, not_done = wait(futures, timeout=watchdog)
    timed_out = bool(not_done)
    reason = None
    if timed_out:
        reason = f"phase exceeded {watchdog}s watchdog"
        stop.set()
        for future in not_done:
            future.cancel()
    for future in done:
        try:
            future.result()
        except Exception as exc:
            stop.set()
            reason = f"worker failed: {sanitize_text(str(exc))}"
    executor.shutdown(wait=not timed_out, cancel_futures=True)
    elapsed = time.monotonic() - started
    return samples, elapsed, in_flight.maximum, not timed_out and reason is None, reason


def _level_summary(
    *,
    samples: list[Any],
    database: str,
    campaign_id: str,
    fingerprint: str,
    concurrency: int,
    warmup_duration: float,
    measured_duration: float,
    max_in_flight: int,
) -> dict[str, Any]:
    statistics = latency_statistics(samples)
    attempts = int(statistics["attempt_count"])
    successes = int(statistics["successful_count"])
    failures = int(statistics["failure_count"])
    return {
        "campaign_id": campaign_id,
        "configuration_fingerprint": fingerprint,
        "database": database,
        "concurrency": concurrency,
        "status": "complete",
        "attempted_operations": attempts,
        "successful_operations": successes,
        "failed_operations": failures,
        "error_rate": failures / attempts if attempts else 0.0,
        "successful_qps": successes / measured_duration if measured_duration else None,
        "measured_duration_seconds": measured_duration,
        "actual_warmup_duration_seconds": warmup_duration,
        "p50_ms": statistics["p50_ms"],
        "p95_ms": statistics["p95_ms"],
        "p99_ms": statistics["p99_ms"],
        "max_observed_in_flight": max_in_flight,
    }


def _operation_summaries(
    samples: list[Any],
    *,
    database: str,
    campaign_id: str,
    fingerprint: str,
    concurrency: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category, workload in (("read", "mixed_read"), ("write", "mixed_write")):
        subset = [
            sample
            for sample in samples
            if sample.workload == workload and not sample.warmup
        ]
        stats = latency_statistics(subset)
        rows.append(
            {
                "campaign_id": campaign_id,
                "configuration_fingerprint": fingerprint,
                "database": database,
                "concurrency": concurrency,
                "operation_category": category,
                "specific_workload": workload,
                "attempted_operations": stats["attempt_count"],
                "successful_operations": stats["successful_count"],
                "failed_operations": stats["failure_count"],
                "error_rate": stats["error_rate"],
                "p50_ms": stats["p50_ms"],
                "p95_ms": stats["p95_ms"],
                "p99_ms": stats["p99_ms"],
            }
        )
    return rows


def _validate_summaries(rows: list[dict[str, Any]], fingerprint: str) -> dict[str, Any]:
    problems: list[str] = []
    expected = {(database, concurrency) for database in DATABASES for concurrency in MIXED_LEVELS}
    actual = {(row.get("database"), row.get("concurrency")) for row in rows}
    if actual != expected:
        problems.append("missing or unexpected database/concurrency level")
    for row in rows:
        if row.get("configuration_fingerprint") != fingerprint:
            problems.append(f"wrong fingerprint: {row.get('database')}/{row.get('concurrency')}")
        if row.get("status") != "complete":
            continue
        attempts = row["attempted_operations"]
        successes = row["successful_operations"]
        failures = row["failed_operations"]
        duration = row["measured_duration_seconds"]
        qps = row["successful_qps"]
        if successes + failures != attempts:
            problems.append(f"attempt arithmetic: {row['database']}/{row['concurrency']}")
        if duration <= 0 or qps is None or qps < 0:
            problems.append(f"invalid duration/qps: {row['database']}/{row['concurrency']}")
        if (
            row["p50_ms"] is not None
            and row["p95_ms"] is not None
            and row["p95_ms"] < row["p50_ms"]
        ):
            problems.append(f"p95 below p50: {row['database']}/{row['concurrency']}")
        if qps is not None and abs(qps - successes / duration) > max(0.01, qps * 1e-9):
            problems.append(f"qps arithmetic: {row['database']}/{row['concurrency']}")
    return {"valid": not problems, "problems": sorted(set(problems)), "result_sets": len(rows)}


def run_final_mixed_campaign(
    campaign_dir: Path | None = None, databases: tuple[str, ...] = DATABASES
) -> dict[str, Any]:
    """Run the frozen mixed stage in the existing final campaign directory."""
    load_dotenv()
    root = repository_root()
    campaign_dir = campaign_dir or root / "results" / "final" / "final-20260820T022802Z"
    manifest_path = campaign_dir / "campaign_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    campaign_id = manifest["campaign_id"]
    fingerprint = configuration_fingerprint()
    if fingerprint != manifest["configuration_fingerprint"]:
        raise RuntimeError("mixed stage configuration fingerprint does not match campaign")
    expected_campaign = "final-20260820T022802Z"
    if campaign_id != expected_campaign:
        raise RuntimeError(f"mixed stage requires existing campaign {expected_campaign}")
    profile = load_final_profile()
    if tuple(profile["concurrency_levels"]) != MIXED_LEVELS:
        raise RuntimeError("mixed stage concurrency levels do not match frozen final profile")
    if any(database not in DATABASES for database in databases):
        raise RuntimeError("mixed stage received an unsupported database")
    _prepare_runtime_environment()
    raw_path = campaign_dir / "raw" / MIXED_RAW_NAME
    before_read_hashes = {
        str(path.relative_to(campaign_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            campaign_dir / "ingest" / "ingest_results.json",
            campaign_dir / "raw" / "read_raw.jsonl",
            campaign_dir / "summaries" / "read_summary.json",
            campaign_dir / "summaries" / "round_summary.json",
        )
    }
    fixture_ids, _, _ = fixture_values(root / "data")
    writer = _RawWriter(raw_path)
    writer.start()
    summary_path = campaign_dir / "summaries" / "mixed_summary.json"
    operation_path = campaign_dir / "summaries" / "mixed_operation_summary.json"
    error_path = campaign_dir / "mixed_errors.json"
    resource_path = campaign_dir / "metadata" / "mixed_resource_observations.json"
    preflight_path = campaign_dir / "metadata" / "mixed_preflight.json"
    summary_rows: list[dict[str, Any]] = (
        json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else []
    )
    operation_rows: list[dict[str, Any]] = (
        json.loads(operation_path.read_text(encoding="utf-8"))
        if operation_path.is_file()
        else []
    )
    errors: list[dict[str, Any]] = (
        json.loads(error_path.read_text(encoding="utf-8")) if error_path.is_file() else []
    )
    resource_rows: dict[str, Any] = (
        json.loads(resource_path.read_text(encoding="utf-8"))
        if resource_path.is_file()
        else {}
    )
    preflight_rows: dict[str, Any] = (
        json.loads(preflight_path.read_text(encoding="utf-8"))
        if preflight_path.is_file()
        else {}
    )
    repair_rows: dict[str, Any] = {}
    database_correctness: dict[str, Any] = {}
    platform_failures: list[dict[str, Any]] = []
    stage_started = datetime.now().astimezone().isoformat()
    _stop_local_databases()
    try:
        for database in databases:
            adapter = None
            try:
                summary_rows = [row for row in summary_rows if row.get("database") != database]
                operation_rows = [row for row in operation_rows if row.get("database") != database]
                errors = [row for row in errors if row.get("database") != database]
                if database in LOCAL_DATABASES:
                    _compose(database, "up")
                    _wait_for_container(database)
                try:
                    preflight_result = preflight(database)
                except Exception as exc:
                    if database != "falkordb" or "filtered_lookup" not in str(exc):
                        raise
                    repair = prepare(database)
                    repair_rows[database] = {
                        "reason": "preflight canonical correctness failure",
                        "original_error": sanitize_text(str(exc)),
                        "repair": repair,
                    }
                    _write_json(
                        campaign_dir / "metadata" / f"mixed_repair_{database}.json",
                        repair_rows[database],
                    )
                    preflight_result = preflight(database)
                preflight_rows[database] = preflight_result
                _write_json(
                    campaign_dir / "metadata" / f"mixed_preflight_{database}.json",
                    preflight_result,
                )
                adapter = create_adapter(database)
                adapter.connect()
                database_rows: list[dict[str, Any]] = []
                for concurrency in MIXED_LEVELS:
                    level_error: str | None = None
                    before = adapter.observe_resources()
                    adapter.reset_write_state()
                    warmup_samples, warmup_elapsed, warmup_max, warmup_ok, warmup_error = _phase(
                        adapter=adapter,
                        database=database,
                        campaign_id=campaign_id,
                        fingerprint=fingerprint,
                        concurrency=concurrency,
                        phase="warmup",
                        duration_seconds=int(profile["mixed_warmup_seconds"]),
                        seed=int(profile["seed"]),
                        fixture_ids=fixture_ids,
                        writer=writer,
                    )
                    adapter.reset_write_state()
                    (
                        measured_samples,
                        measured_elapsed,
                        measured_max,
                        measured_ok,
                        measured_error,
                    ) = _phase(
                        adapter=adapter,
                        database=database,
                        campaign_id=campaign_id,
                        fingerprint=fingerprint,
                        concurrency=concurrency,
                        phase="measured",
                        duration_seconds=int(profile["mixed_measurement_seconds"]),
                        seed=int(profile["seed"]),
                        fixture_ids=fixture_ids,
                        writer=writer,
                    )
                    if not warmup_ok:
                        level_error = warmup_error
                    if not measured_ok:
                        level_error = measured_error
                    counts = adapter.verify_counts()
                    expected = expected_counts()
                    if counts != expected:
                        level_error = (
                            f"post-level count mismatch: expected={expected}, actual={counts}"
                        )
                    all_samples = warmup_samples + measured_samples
                    if level_error is not None:
                        row = {
                            "campaign_id": campaign_id,
                            "configuration_fingerprint": fingerprint,
                            "database": database,
                            "concurrency": concurrency,
                            "status": "FAILED / NOT COMPLETED",
                            "reason": sanitize_text(level_error),
                            "attempted_operations": None,
                            "successful_operations": None,
                            "failed_operations": None,
                            "error_rate": None,
                            "successful_qps": None,
                            "measured_duration_seconds": measured_elapsed,
                            "actual_warmup_duration_seconds": warmup_elapsed,
                            "p50_ms": None,
                            "p95_ms": None,
                            "p99_ms": None,
                            "max_observed_in_flight": max(warmup_max, measured_max),
                        }
                        platform_failures.append(
                            {
                                "database": database,
                                "concurrency": concurrency,
                                "error_type": "InvalidMixedLevel",
                                "message": sanitize_text(level_error),
                            }
                        )
                    else:
                        row = _level_summary(
                            samples=measured_samples,
                            database=database,
                            campaign_id=campaign_id,
                            fingerprint=fingerprint,
                            concurrency=concurrency,
                            warmup_duration=warmup_elapsed,
                            measured_duration=measured_elapsed,
                            max_in_flight=max(warmup_max, measured_max),
                        )
                        operation_rows.extend(
                            _operation_summaries(
                                all_samples,
                                database=database,
                                campaign_id=campaign_id,
                                fingerprint=fingerprint,
                                concurrency=concurrency,
                            )
                        )
                    summary_rows.append(row)
                    database_rows.append(row)
                    for sample in measured_samples:
                        if not sample.success:
                            errors.append(
                                {
                                    "campaign_id": campaign_id,
                                    "configuration_fingerprint": fingerprint,
                                    "database": database,
                                    "concurrency": concurrency,
                                    "workload": sample.workload,
                                    "error_type": sample.error_type,
                                    "error_message": sanitize_text(sample.error_message or ""),
                                }
                            )
                    after = adapter.observe_resources()
                    resource_rows.setdefault(database, []).append(
                        {
                            "concurrency": concurrency,
                            "before": asdict(before) if before else "not observable",
                            "after": asdict(after) if after else "not observable",
                        }
                    )
                    _write_json(campaign_dir / "summaries" / "mixed_summary.json", summary_rows)
                    _write_json(
                        campaign_dir / "summaries" / "mixed_operation_summary.json",
                        operation_rows,
                    )
                database_correctness[database] = validate_adapter(adapter)
            except Exception as exc:
                message = sanitize_text(str(exc))
                platform_failures.append(
                    {"database": database, "error_type": type(exc).__name__, "message": message}
                )
                errors.append(
                    {"database": database, "error_type": type(exc).__name__, "message": message}
                )
                database_correctness[database] = {
                    "status": "FAILED / NOT COMPLETED",
                    "reason": message,
                }
                for concurrency in MIXED_LEVELS:
                    if not any(
                        row["database"] == database and row["concurrency"] == concurrency
                        for row in summary_rows
                    ):
                        summary_rows.append(
                            {
                                "campaign_id": campaign_id,
                                "configuration_fingerprint": fingerprint,
                                "database": database,
                                "concurrency": concurrency,
                                "status": "FAILED / NOT COMPLETED",
                                "reason": message,
                                "attempted_operations": None,
                                "successful_operations": None,
                                "failed_operations": None,
                                "error_rate": None,
                                "successful_qps": None,
                                "measured_duration_seconds": None,
                                "actual_warmup_duration_seconds": None,
                                "p50_ms": None,
                                "p95_ms": None,
                                "p99_ms": None,
                                "max_observed_in_flight": None,
                            }
                        )
            finally:
                if adapter is not None:
                    adapter.close()
                if database in LOCAL_DATABASES:
                    _compose(database, "down")
    finally:
        try:
            _stop_local_databases()
        finally:
            writer.close()

    completeness = _validate_summaries(summary_rows, fingerprint)
    _write_json(campaign_dir / "summaries" / "mixed_completeness_validation.json", completeness)
    _write_csv(campaign_dir / "summaries" / "mixed_summary.csv", summary_rows)
    _write_csv(campaign_dir / "summaries" / "mixed_operation_summary.csv", operation_rows)
    _write_json(campaign_dir / "metadata" / "mixed_preflight.json", preflight_rows)
    _write_json(campaign_dir / "metadata" / "mixed_repairs.json", repair_rows)
    _write_json(campaign_dir / "metadata" / "mixed_resource_observations.json", resource_rows)
    _write_json(campaign_dir / "metadata" / "mixed_environment.json", capture_environment())
    # Rebuild the public failure index from immutable raw evidence so a harness
    # interruption cannot silently lose failures observed before its last flush.
    raw_errors: list[dict[str, Any]] = []
    if raw_path.is_file():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("phase") == "measured" and not record.get("success", True):
                raw_errors.append(
                    {
                        key: record.get(key)
                        for key in (
                            "campaign_id",
                            "configuration_fingerprint",
                            "database",
                            "concurrency",
                            "workload",
                            "error_type",
                            "error_message",
                        )
                    }
                )
    errors = raw_errors
    _write_json(campaign_dir / "mixed_errors.json", errors)
    after_read_hashes = {
        str(path.relative_to(campaign_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            campaign_dir / "ingest" / "ingest_results.json",
            campaign_dir / "raw" / "read_raw.jsonl",
            campaign_dir / "summaries" / "read_summary.json",
            campaign_dir / "summaries" / "round_summary.json",
        )
    }
    preservation = {
        key: before_read_hashes[key] == after_read_hashes[key] for key in before_read_hashes
    }
    _write_json(campaign_dir / "metadata" / "prompt6_artifact_preservation.json", preservation)
    manifest.update(
        {
            "mixed_stage_started_at": stage_started,
            "mixed_stage_finished_at": datetime.now().astimezone().isoformat(),
            "mixed_results_directory": "raw/mixed_raw.jsonl",
            "ingest_stage": "complete",
            "read_stage": "complete",
            "mixed_stage": "complete" if completeness["valid"] else "partial",
            "mixed_result_completeness": completeness,
            "mixed_platform_failures": platform_failures,
            "mixed_error_count": len(errors),
            "prompt6_artifact_preservation": preservation,
            "campaign_status": (
                "complete_with_measured_failures"
                if completeness["valid"] and errors
                else ("complete" if completeness["valid"] else "incomplete")
            ),
        }
    )
    _write_json(manifest_path, manifest)
    return {
        "campaign_id": campaign_id,
        "configuration_fingerprint": fingerprint,
        "campaign_dir": str(campaign_dir),
        "summary": summary_rows,
        "operation_summary": operation_rows,
        "errors": errors,
        "correctness": database_correctness,
        "completeness": completeness,
        "preservation": preservation,
        "manifest": manifest,
    }


def validate_final_mixed_campaign(campaign_dir: Path | None = None) -> dict[str, Any]:
    """Re-run the lightweight post-workload canonical checks without new measurements."""
    load_dotenv()
    _prepare_runtime_environment()
    root = repository_root()
    campaign_dir = campaign_dir or root / "results" / "final" / "final-20260820T022802Z"
    results: dict[str, Any] = {}
    repairs: dict[str, Any] = {}
    _stop_local_databases()
    try:
        for database in DATABASES:
            adapter = None
            try:
                if database in LOCAL_DATABASES:
                    _compose(database, "up")
                    _wait_for_container(database)
                adapter = create_adapter(database)
                adapter.connect()
                try:
                    counts = adapter.verify_counts()
                    tested = validate_adapter(adapter)
                except Exception as exc:
                    if database != "falkordb" or "filtered_lookup" not in str(exc):
                        raise
                    adapter.close()
                    adapter = None
                    repair = prepare(database)
                    repairs[database] = {
                        "reason": "post-workload restored-index correctness failure",
                        "original_error": sanitize_text(str(exc)),
                        "repair": repair,
                    }
                    adapter = create_adapter(database)
                    adapter.connect()
                    counts = adapter.verify_counts()
                    tested = validate_adapter(adapter)
                results[database] = {
                    "status": "PASS",
                    "counts": {"nodes": counts[0], "relationships": counts[1]},
                    "validation": tested,
                }
                if database in repairs:
                    results[database]["repair"] = repairs[database]
            except Exception as exc:
                results[database] = {
                    "status": "FAILED",
                    "reason": sanitize_text(str(exc)),
                }
            finally:
                if adapter is not None:
                    adapter.close()
                if database in LOCAL_DATABASES:
                    _compose(database, "down")
    finally:
        _stop_local_databases()
    _write_json(
        campaign_dir / "metadata" / "mixed_post_correctness.json",
        {"results": results, "repairs": repairs},
    )
    return results
