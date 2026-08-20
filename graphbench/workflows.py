"""Explicit integration workflows; failures are retained in safe, machine-readable artifacts."""

from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from graphbench.adapters import create_adapter
from graphbench.adapters.base import GraphDatabaseAdapter
from graphbench.config import repository_root
from graphbench.environment import sanitize_text
from graphbench.models import LoadMetrics
from graphbench.oracle import CanonicalOracle, fixture_values


class CountMismatchError(RuntimeError):
    pass


class ValidationError(RuntimeError):
    pass


def _data_root() -> Path:
    return repository_root() / "data"


def _nodes() -> list[dict[str, int]]:
    with (_data_root() / "processed" / "nodes.csv").open(newline="", encoding="utf-8") as source:
        return [
            {"id": int(row["id"]), "bucket": int(row["bucket"])} for row in csv.DictReader(source)
        ]


def _relationships() -> list[dict[str, int]]:
    with (_data_root() / "processed" / "relationships.csv").open(
        newline="", encoding="utf-8"
    ) as source:
        return [
            {"source_id": int(row["source_id"]), "target_id": int(row["target_id"])}
            for row in csv.DictReader(source)
        ]


def expected_counts() -> tuple[int, int]:
    return len(_nodes()), len(_relationships())


def validate_adapter(adapter: GraphDatabaseAdapter, *, subset: int | None = None) -> list[str]:
    """Compare deterministic fixture answers with the Python oracle.

    Raises on the first mismatch.
    """
    oracle = CanonicalOracle.from_data_root(_data_root())
    lookup_ids, buckets, starts = fixture_values(_data_root())
    if subset is not None:
        lookup_ids, buckets, starts = lookup_ids[:subset], buckets[:subset], starts[:subset]
    tested: list[str] = []
    checks = [
        ("point_lookup", lookup_ids, oracle.point_lookup, adapter.point_lookup),
        ("filtered_lookup", buckets, oracle.filtered_lookup, adapter.filtered_lookup),
        ("one_hop", starts, lambda value: oracle.path_count(value, 1), adapter.one_hop),
        ("two_hop", starts, lambda value: oracle.path_count(value, 2), adapter.two_hop),
        ("three_hop", starts, lambda value: oracle.path_count(value, 3), adapter.three_hop),
    ]
    for workload, values, expected_fn, actual_fn in checks:
        for fixture in values:
            expected, actual = expected_fn(fixture), actual_fn(fixture)
            if expected != actual:
                raise ValidationError(
                    f"{workload} fixture={fixture}: expected={expected}, actual={actual}"
                )
        tested.append(workload)
    expected_aggregation = oracle.aggregation()
    actual_aggregation = dict(adapter.aggregation())
    if expected_aggregation != actual_aggregation:
        raise ValidationError(
            f"aggregation: expected={expected_aggregation}, actual={actual_aggregation}"
        )
    tested.append("aggregation")
    return tested


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "not available"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _status_path(database: str) -> Path:
    return repository_root() / "results" / "metadata" / f"integration_status_{database}.json"


def _persist_status(database: str, status: dict[str, Any]) -> None:
    _write_json(_status_path(database), status)
    aggregate_path = repository_root() / "results" / "metadata" / "integration_status.json"
    try:
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        aggregate = {}
    aggregate[database] = status
    _write_json(aggregate_path, aggregate)


def smoke(database: str) -> dict[str, Any]:
    adapter = create_adapter(database)
    try:
        adapter.connect()
        healthy = adapter.health_check()
        # A representative harmless query; it does not create or modify graph data.
        adapter.point_lookup(-1)
        return {
            "database": database,
            "connection_success": healthy,
            "metadata": dict(adapter.platform_metadata()),
        }
    finally:
        adapter.close()


def prepare(database: str) -> dict[str, Any]:
    """Prepare only the benchmark graph, then verify its exact canonical contents and workloads."""
    adapter = create_adapter(database)
    expected_nodes, expected_relationships = expected_counts()
    status: dict[str, Any] = {
        "database": database,
        "timestamp": datetime.now().astimezone().isoformat(),
        "git_commit": _git_commit(),
        "connection_success": False,
        "tls_verification_success": None,
        "schema_success": False,
        "load_success": False,
        "load_batch_size": adapter.batch_size,
        "indexes": "not attempted",
        "expected_node_count": expected_nodes,
        "actual_node_count": None,
        "expected_relationship_count": expected_relationships,
        "actual_relationship_count": None,
        "validation_success": False,
        "tested_workloads": [],
    }
    try:
        adapter.connect()
        status["connection_success"] = adapter.health_check()
        if database == "cognodb":
            status["tls_verification_success"] = True
        adapter.reset()
        adapter.create_schema()
        status["schema_success"] = True
        status["indexes"] = {
            "User.id": "created; verification pending",
            "User.bucket": "created; verification pending",
        }
        node_result = adapter.load_nodes(_nodes())
        relationship_result = adapter.load_relationships(_relationships())
        node_seconds = node_result.duration_ms / 1_000
        relationship_seconds = relationship_result.duration_ms / 1_000
        metrics = LoadMetrics(
            database=database,
            nodes_loaded=node_result.loaded,
            relationships_loaded=relationship_result.loaded,
            node_load_seconds=node_seconds,
            relationship_load_seconds=relationship_seconds,
            nodes_per_second=node_result.loaded / node_seconds if node_seconds else 0.0,
            relationships_per_second=relationship_result.loaded / relationship_seconds
            if relationship_seconds
            else 0.0,
            total_load_seconds=node_seconds + relationship_seconds,
            batch_size=adapter.batch_size,
            load_method=getattr(
                adapter,
                "load_method",
                "parameterized transactional driver batches; schema time excluded",
            ),
            success=not node_result.errors and not relationship_result.errors,
            errors=node_result.errors + relationship_result.errors,
        )
        status["load_metrics"] = asdict(metrics)
        _write_json(
            repository_root() / "results" / "metadata" / f"load_{database}.json", asdict(metrics)
        )
        if (
            not metrics.success
            or node_result.loaded != expected_nodes
            or relationship_result.loaded != expected_relationships
        ):
            raise CountMismatchError(
                "driver load incomplete: "
                f"{metrics.errors or 'loaded count differs from canonical input'}"
            )
        actual_nodes, actual_relationships = adapter.verify_counts()
        status["actual_node_count"] = actual_nodes
        status["actual_relationship_count"] = actual_relationships
        if (actual_nodes, actual_relationships) != (expected_nodes, expected_relationships):
            raise CountMismatchError(
                f"database counts expected nodes={expected_nodes}, "
                f"relationships={expected_relationships}; "
                f"actual nodes={actual_nodes}, relationships={actual_relationships}"
            )
        status["load_success"] = True
        status["tested_workloads"] = validate_adapter(adapter)
        status["validation_success"] = True
        return status
    except Exception as exc:
        error = sanitize_text(str(exc))
        status["error"] = error
        if database == "cognodb" and "SSLCertVerificationError" in error:
            status["tls_verification_success"] = False
            status["tls_diagnostic"] = {
                "category": "TLS",
                "reason": "certificate verification failed; certificate has expired",
            }
        raise
    finally:
        try:
            metadata = dict(adapter.platform_metadata())
            status["metadata"] = metadata
            if status["schema_success"]:
                observed = set(metadata.get("indexed_properties", "").split(","))
                if {"id", "bucket"} <= observed:
                    status["indexes"] = {"User.id": "verified", "User.bucket": "verified"}
                elif metadata.get("indexed_properties") == "not observable":
                    status["indexes"] = {
                        "User.id": "created; not observable",
                        "User.bucket": "created; not observable",
                    }
            observation = adapter.observe_resources()
            status["resource_configuration"] = (
                asdict(observation) if observation else "not observable"
            )
            if observation is not None:
                status["configured_cpu_limit"] = observation.cpu_percent
                status["configured_memory_limit"] = observation.memory_bytes
                status["container_image"] = observation.image
                status["container_status"] = observation.container_status
        finally:
            adapter.close()
            _persist_status(database, status)
