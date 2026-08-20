"""Frozen-profile metadata, preflight, and explicitly non-final diagnostics."""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import pstdev
from typing import Any

from graphbench.adapters import create_adapter
from graphbench.config import load_final_profile, repository_root
from graphbench.environment import load_dotenv, sanitize_text
from graphbench.metrics import latency_statistics
from graphbench.oracle import CanonicalOracle, fixture_values
from graphbench.runner import timed_operation
from graphbench.workflows import _git_commit, expected_counts, validate_adapter

DATABASES = ("cognodb", "neo4j", "memgraph", "falkordb", "arangodb")
LOCAL_DATABASES = ("neo4j", "memgraph", "falkordb", "arangodb")
VOLUMES = {
    "neo4j": "docker_neo4j-data",
    "memgraph": "docker_memgraph-data",
    "falkordb": "docker_falkordb-data",
    "arangodb": "docker_arangodb-data",
}

# Neo4j's verified final deployment is the documented resource-parity deviation.
# It is deliberately explicit here rather than silently compared with the 256 MiB
# common target in benchmark-final.yaml.
RESOURCE_EXPECTATIONS = {
    "neo4j": (0.5, 384 * 1024 * 1024),
    "memgraph": (0.5, 256 * 1024 * 1024),
    "falkordb": (0.5, 256 * 1024 * 1024),
    "arangodb": (0.5, 256 * 1024 * 1024),
}
READ_WORKLOADS = (
    "point_lookup",
    "filtered_lookup",
    "one_hop",
    "two_hop",
    "three_hop",
    "aggregation",
)


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workload_manifest() -> dict[str, Any]:
    """Machine-readable canonical workload contract, independent of query language syntax."""
    fixture_root = "data/fixtures"
    return {
        "canonical_graph": {
            "node": "User(id: integer, bucket: integer)",
            "relationship": "(User)-[:VOTED_FOR]->(User)",
            "direction": "outgoing",
        },
        "workloads": {
            "point_lookup": {
                "logical_definition": "count User where id equals fixture id",
                "fixture_source": f"{fixture_root}/lookup_ids.json",
                "result_semantic": "0 or 1 matching canonical user",
                "index_dependency": "User.id / users.id",
            },
            "filtered_lookup": {
                "logical_definition": "count User where bucket equals fixture bucket",
                "fixture_source": f"{fixture_root}/buckets.json",
                "result_semantic": "matching user count",
                "index_dependency": "User.bucket / users.bucket",
            },
            **{
                name: {
                    "logical_definition": f"count exact {depth}-edge outgoing VOTED_FOR paths",
                    "fixture_source": f"{fixture_root}/start_nodes.json",
                    "result_semantic": "relationship-distinct path multiplicity",
                    "index_dependency": "User.id / users.id for start lookup",
                    "direction": "outgoing",
                    "hop_depth": depth,
                }
                for name, depth in (("one_hop", 1), ("two_hop", 2), ("three_hop", 3))
            },
            "aggregation": {
                "logical_definition": "group all Users by bucket and count",
                "fixture_source": "none",
                "result_semantic": "complete bucket-to-count map",
                "index_dependency": "equivalent bucket schema index",
            },
            "mixed_read": {
                "logical_definition": "canonical point lookup",
                "fixture_source": f"{fixture_root}/lookup_ids.json",
                "result_semantic": "0 or 1 matching canonical user",
                "index_dependency": "User.id / users.id",
            },
            "mixed_write": {
                "logical_definition": "increment only benchmark_counter for canonical id",
                "fixture_source": f"{fixture_root}/lookup_ids.json",
                "result_semantic": "one affected user; topology/id/bucket unchanged",
                "index_dependency": "User.id / users.id",
            },
        },
    }


def write_workload_manifest() -> tuple[Path, str]:
    path = repository_root() / "results" / "metadata" / "workload_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = workload_manifest()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, _sha256_file(path)


def configuration_fingerprint() -> str:
    """Fingerprint every input that may make future measurements incomparable."""
    profile = load_final_profile()
    root = repository_root()
    _, fixture_hash = write_workload_manifest()
    dataset_metadata = json.loads((root / "data" / "metadata" / "wiki_vote.json").read_text())
    fixture_hashes = {
        path.name: _sha256_file(path)
        for path in sorted((root / "data" / "fixtures").glob("*.json"))
    }
    payload = {
        "profile": profile,
        "dataset_checksum": dataset_metadata["source_checksum"],
        "dataset_counts": {
            "nodes": dataset_metadata["node_count"],
            "relationships": dataset_metadata["relationship_count"],
        },
        "fixture_hashes": fixture_hashes,
        "workload_manifest_hash": fixture_hash,
        "platforms": ["cognodb", "neo4j", "memgraph", "falkordb", "arangodb"],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return "not observable"
    return (
        result.stdout.strip()
        if result.returncode == 0 and result.stdout.strip()
        else "not observable"
    )


def _host_memory_bytes() -> int | str:
    if os.name != "nt":
        try:
            return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        except (AttributeError, ValueError, OSError):
            return "not observable"
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return int(status.ullTotalPhys)
    except Exception:
        return "not observable"


def capture_environment() -> dict[str, Any]:
    """Capture public benchmark-client facts without usernames, paths, or addresses."""
    profile = load_final_profile()
    root = repository_root()
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "os": platform.system(),
        "os_version": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": _command_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)",
            ]
        )
        if os.name == "nt"
        else platform.processor() or "not observable",
        "logical_cpu_count": os.cpu_count() or "not observable",
        "total_host_ram_bytes": _host_memory_bytes(),
        "python_version": platform.python_version(),
        "neo4j_driver_version": importlib.metadata.version("neo4j"),
        "falkordb_client_version": importlib.metadata.version("falkordb"),
        "arangodb_client_version": importlib.metadata.version("python-arango"),
        "docker_version": _command_output(["docker", "version", "--format", "{{.Server.Version}}"]),
        "docker_compose_version": _command_output(["docker", "compose", "version", "--short"]),
        "git_commit": _command_output(["git", "rev-parse", "HEAD"]),
        "benchmark_config_hash": hashlib.sha256(_canonical_bytes(profile)).hexdigest(),
        "configuration_fingerprint": configuration_fingerprint(),
        "timezone": datetime.now().astimezone().tzname() or "not observable",
        "benchmark_client_region": profile["benchmark_client_region"],
    }
    path = root / "results" / "metadata" / "environment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def preflight(database: str) -> dict[str, Any]:
    """Refuse future benchmark work unless connection, data, indexes, and oracle agree."""
    profile = load_final_profile()
    adapter = create_adapter(database)
    expected = expected_counts()
    try:
        adapter.connect()
        if not adapter.health_check():
            raise RuntimeError("health check returned false")
        actual = adapter.verify_counts()
        if actual != expected:
            raise RuntimeError(f"count mismatch: expected={expected}, actual={actual}")
        resource_payload: dict[str, Any] | str = "not required for remote managed service"
        if database in LOCAL_DATABASES:
            resources = adapter.observe_resources()
            expected_cpu, expected_memory = RESOURCE_EXPECTATIONS[database]
            if (
                resources is None
                or resources.cpu_percent != expected_cpu
                or resources.memory_bytes != expected_memory
            ):
                actual_resources = asdict(resources) if resources else "not observable"
                raise RuntimeError(
                    "final resource preflight failed: "
                    f"expected {expected_cpu} CPU / {expected_memory} bytes, "
                    f"actual={actual_resources}"
                )
            resource_payload = {
                "observed": asdict(resources),
                "expected_cpu_cores": expected_cpu,
                "expected_memory_bytes": expected_memory,
                "resource_parity_status": (
                    "deviation" if database == "neo4j" else "strict compute parity"
                ),
            }
        workloads = validate_adapter(adapter)
        lookup_ids, _, _ = fixture_values(repository_root() / "data")
        if adapter.mixed_write({"id": lookup_ids[0]}) != 1:
            raise RuntimeError("benchmark write primitive did not affect exactly one User")
        adapter.reset_write_state()
        return {
            "database": database,
            "connection": True,
            "counts": {"nodes": actual[0], "relationships": actual[1]},
            "resources": resource_payload,
            "indexes": "verified by adapter",
            "validation": workloads,
            "configuration_fingerprint": configuration_fingerprint(),
            "profile": profile["profile"],
        }
    finally:
        adapter.close()


def transport_baseline(database: str, *, warmup: int = 10, measured: int = 100) -> dict[str, Any]:
    """Record diagnostic-only cheap-request latency; never normalizes benchmark measurements."""
    adapter = create_adapter(database)
    samples = []
    try:
        adapter.connect()
        for iteration in range(warmup):
            samples.append(
                timed_operation(
                    database=database,
                    workload="transport_baseline",
                    round_number=0,
                    iteration=iteration,
                    fixture_id="RETURN 1 equivalent",
                    operation=adapter.health_check,
                    result_count=lambda result: int(bool(result)),
                    warmup=True,
                )
            )
        for iteration in range(measured):
            samples.append(
                timed_operation(
                    database=database,
                    workload="transport_baseline",
                    round_number=1,
                    iteration=iteration,
                    fixture_id="RETURN 1 equivalent",
                    operation=adapter.health_check,
                    result_count=lambda result: int(bool(result)),
                )
            )
    finally:
        adapter.close()
    return {
        "database": database,
        "diagnostic_only": True,
        "not_subtracted_from_query_latency": True,
        "warmup_iterations": warmup,
        "measured_iterations": measured,
        "statistics": latency_statistics(samples),
        "raw_samples": [asdict(sample) for sample in samples],
    }


def write_transport_baselines(databases: tuple[str, ...] = DATABASES) -> dict[str, Any]:
    path = repository_root() / "results" / "metadata" / "transport_baseline.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    results = dict(existing.get("results", {}))
    results.update({database: transport_baseline(database) for database in databases})
    payload = {
        "purpose": "diagnostic-only client/transport floor; never normalize benchmark latency",
        "timestamp": datetime.now().astimezone().isoformat(),
        "results": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _volume_footprint_bytes(volume: str) -> int | str:
    """Measure used data in a named volume; this is not a quota or allocated limit."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/volume",
            "busybox:1.36",
            "du",
            "-sk",
            "/volume",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return "not observable"
    try:
        return int(result.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return "not observable"


def write_fairness_manifest() -> dict[str, Any]:
    """Persist observed and advertised fairness evidence without inferring unavailable limits."""
    root = repository_root()
    profile = load_final_profile()
    _, workload_hash = write_workload_manifest()
    dataset = json.loads((root / "data" / "metadata" / "wiki_vote.json").read_text())
    fixture_hash = hashlib.sha256(
        b"".join(path.read_bytes() for path in sorted((root / "data" / "fixtures").glob("*.json")))
    ).hexdigest()
    try:
        statuses = json.loads(
            (root / "results" / "metadata" / "integration_status.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        statuses = {}
    entries: dict[str, Any] = {
        "cognodb": {
            "database": "cognodb",
            "deployment_type": "managed cloud",
            "cpu": {
                "advertised": "0.5 burstable vCPU",
                "configured": "not observable",
                "observed": "not observable",
            },
            "ram": {
                "advertised": "256 MiB",
                "configured": "not observable",
                "observed": "not observable",
            },
            "storage": {
                "advertised": "1 GiB",
                "configured": "not observable",
                "observed_footprint": "not observable",
            },
            "resource_source": "Wexa assignment specification",
            "observation_type": "advertised",
            "network_topology": "remote TLS/Bolt",
            "resource_parity_status": "advertised compute target; remote managed service",
            "known_deviations": ["WAN/TLS transport differs from local loopback containers"],
        }
    }
    drivers = {
        "neo4j": importlib.metadata.version("neo4j"),
        "memgraph": importlib.metadata.version("neo4j"),
        "falkordb": importlib.metadata.version("falkordb"),
        "arangodb": importlib.metadata.version("python-arango"),
    }
    for database in LOCAL_DATABASES:
        status = statuses.get(database, {})
        resource = status.get("resource_configuration", {})
        if not isinstance(resource, dict):
            resource = {}
        observed_cpu = resource.get("cpu_percent")
        observed_memory = resource.get("memory_bytes")
        parity = (
            "strict compute parity"
            if observed_cpu == profile["target_cpu_cores"]
            and observed_memory == profile["target_memory_bytes"]
            else "resource parity deviation"
        )
        entries[database] = {
            "database": database,
            "deployment_type": "local controlled Docker",
            "cpu": {
                "advertised": None,
                "configured": profile["target_cpu_cores"],
                "observed": observed_cpu if observed_cpu is not None else "not observable",
            },
            "ram": {
                "advertised": None,
                "configured": profile["target_memory_bytes"],
                "observed": observed_memory if observed_memory is not None else "not observable",
            },
            "storage": {
                "advertised": None,
                "configured": "not available/reliable in current Docker Desktop setup",
                "observed_footprint": _volume_footprint_bytes(VOLUMES[database]),
            },
            "container_image": resource.get(
                "image", status.get("container_image", "not observable")
            ),
            "database_version": status.get("metadata", {}).get("server_version")
            or status.get("metadata", {}).get("server_agent", "not observable"),
            "driver": drivers[database],
            "query_language": "AQL" if database == "arangodb" else "Cypher",
            "transport": "HTTP"
            if database == "arangodb"
            else ("Redis protocol" if database == "falkordb" else "Bolt"),
            "network_topology": "local loopback Docker",
            "resource_parity_status": parity,
            "known_deviations": (
                [
                    "FalkorDB RDB restore requires the normal prepare schema rebuild "
                    "before indexed validation."
                ]
                if database == "falkordb"
                else (
                    []
                    if parity == "strict compute parity"
                    else ["observed Docker limit differs from final profile"]
                )
            ),
        }
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "configuration_fingerprint": configuration_fingerprint(),
        "dataset_hash": dataset["source_checksum"],
        "fixture_hash": fixture_hash,
        "workload_manifest_hash": workload_hash,
        "storage_quota_note": (
            "Docker Desktop volume quota is not reliably enforceable here; "
            "observed footprint is not a storage limit."
        ),
        "platforms": entries,
    }
    path = root / "results" / "metadata" / "fairness_manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def dry_run(database: str) -> dict[str, Any]:
    """A tiny, explicitly tagged execution path that cannot be mistaken for final results."""
    preflight_result = preflight(database)
    adapter = create_adapter(database)
    lookup_ids, _, _ = fixture_values(repository_root() / "data")
    samples = []
    try:
        adapter.connect()
        for iteration in range(2):
            samples.append(
                timed_operation(
                    database=database,
                    workload="point_lookup",
                    round_number=0,
                    iteration=iteration,
                    fixture_id=str(lookup_ids[iteration]),
                    operation=lambda value=lookup_ids[iteration]: adapter.point_lookup(value),
                    result_count=int,
                    warmup=True,
                )
            )
        for iteration in range(3):
            samples.append(
                timed_operation(
                    database=database,
                    workload="point_lookup",
                    round_number=1,
                    iteration=iteration,
                    fixture_id=str(lookup_ids[iteration]),
                    operation=lambda value=lookup_ids[iteration]: adapter.point_lookup(value),
                    result_count=int,
                )
            )
    finally:
        adapter.close()
    payload = {
        "run_type": "dry_run",
        "database": database,
        "timestamp": datetime.now().astimezone().isoformat(),
        "configuration_fingerprint": configuration_fingerprint(),
        "preflight": preflight_result,
        "summary": latency_statistics(samples),
        "raw_samples": [asdict(sample) for sample in samples],
    }
    directory = repository_root() / "results" / "dry_runs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{database}_dry_run.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _write_payload(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _compose_file(database: str) -> Path:
    return repository_root() / "docker" / f"{database}-compose.yaml"


def _container_name(database: str) -> str:
    return f"graphbench-{database}"


def _docker_env_value(container: str, key: str) -> str | None:
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspected.returncode:
        return None
    try:
        variables = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        return None
    prefix = f"{key}="
    return next(
        (value.removeprefix(prefix) for value in variables if value.startswith(prefix)), None
    )


def _prepare_runtime_environment() -> None:
    """Fill only absent local runtime settings; values never enter campaign artifacts."""
    if not os.environ.get("NEO4J_PASSWORD"):
        auth = _docker_env_value(_container_name("neo4j"), "NEO4J_AUTH") or ""
        if "/" in auth:
            os.environ["NEO4J_PASSWORD"] = auth.split("/", 1)[1]
    os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
    os.environ.setdefault("NEO4J_USER", "neo4j")
    os.environ.setdefault("ARANGODB_URL", "http://localhost:8529")
    os.environ.setdefault("ARANGODB_USER", "root")
    # A new Arango container needs an initialization password. This stays process-local.
    existing_arango_password = _docker_env_value(
        _container_name("arangodb"), "ARANGO_ROOT_PASSWORD"
    )
    os.environ.setdefault(
        "ARANGODB_PASSWORD", existing_arango_password or secrets.token_urlsafe(18)
    )


def _compose(database: str, action: str) -> None:
    container = _container_name(database)
    if action == "down":
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0 or inspected.stdout.strip() != "true":
            return
        result = subprocess.run(
            ["docker", "stop", container], capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise RuntimeError(
                f"docker stop failed for {database}: {sanitize_text(result.stderr.strip())}"
            )
        return
    existing = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != "true":
            result = subprocess.run(
                ["docker", "start", container], capture_output=True, text=True, check=False
            )
            if result.returncode:
                raise RuntimeError(
                    f"docker start failed for {database}: {sanitize_text(result.stderr.strip())}"
                )
        return
    result = subprocess.run(
        ["docker", "compose", "-f", str(_compose_file(database)), "up", "-d"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = sanitize_text((result.stderr or result.stdout).strip())
        raise RuntimeError(f"docker compose {action} failed for {database}: {detail}")


def _wait_for_container(database: str, timeout_seconds: int = 90) -> None:
    """Wait for the Docker healthcheck before opening the benchmark client."""
    deadline = time.monotonic() + timeout_seconds
    container = _container_name(database)
    while time.monotonic() < deadline:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                container,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        state, _, health = inspected.stdout.strip().partition("|")
        # ArangoDB's image lacks curl, so its Compose healthcheck can be unhealthy
        # while the adapter's authenticated HTTP health check is valid.
        if inspected.returncode == 0 and state == "true" and (
            database != "neo4j" or health in {"", "healthy"}
        ):
            return
        time.sleep(2)
    raise RuntimeError(f"{database} container did not become ready within {timeout_seconds}s")


def _stop_local_databases() -> None:
    """Ensure local competitors do not share host resources with the active run."""
    for database in LOCAL_DATABASES:
        if _compose_file(database).is_file():
            _compose(database, "down")


def _fixture_operation(adapter: Any, workload: str, fixture: Any) -> tuple[Any, Any, Any]:
    if workload == "point_lookup":
        return (lambda: adapter.point_lookup(fixture), int, str(fixture))
    if workload == "filtered_lookup":
        return (lambda: adapter.filtered_lookup(fixture), int, str(fixture))
    if workload == "one_hop":
        return (lambda: adapter.one_hop(fixture), int, str(fixture))
    if workload == "two_hop":
        return (lambda: adapter.two_hop(fixture), int, str(fixture))
    if workload == "three_hop":
        return (lambda: adapter.three_hop(fixture), int, str(fixture))
    return (adapter.aggregation, lambda result: len(result), "all_buckets")


def _expected_result(oracle: Any, workload: str, fixture: Any) -> Any:
    if workload == "point_lookup":
        return oracle.point_lookup(fixture)
    if workload == "filtered_lookup":
        return oracle.filtered_lookup(fixture)
    if workload == "one_hop":
        return oracle.path_count(fixture, 1)
    if workload == "two_hop":
        return oracle.path_count(fixture, 2)
    if workload == "three_hop":
        return oracle.path_count(fixture, 3)
    return oracle.aggregation()


def _sample_record(
    sample: Any, campaign_id: str, fingerprint: str, expected: Any
) -> dict[str, Any]:
    record = asdict(sample)
    actual = record.get("result_count")
    expected_value = len(expected) if isinstance(expected, dict) else expected
    record.update(
        {
            "campaign_id": campaign_id,
            "configuration_fingerprint": fingerprint,
            "timestamp": datetime.now().astimezone().isoformat(),
            "expected_result": expected_value,
            "result_correct": (None if not sample.success else actual == expected_value),
        }
    )
    return record


def validate_final_campaign(campaign_dir: Path) -> dict[str, Any]:
    """Validate measured-sample shape and summary arithmetic without changing results."""
    raw_path = campaign_dir / "raw" / "read_raw.jsonl"
    rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    problems: list[str] = []
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("warmup"):
            continue
        grouped.setdefault(
            (str(row["database"]), str(row["workload"]), int(row["round"])), []
        ).append(row)
        duration = row.get("duration_ms")
        if duration is not None and float(duration) < 0:
            problems.append("negative latency")
    for database in DATABASES:
        for workload in READ_WORKLOADS:
            rounds = [grouped.get((database, workload, round_id), []) for round_id in (1, 2, 3)]
            if any(len(round_rows) != 200 for round_rows in rounds):
                problems.append(f"{database}/{workload}: expected 200 attempts in each round")
    summary_rows = json.loads((campaign_dir / "summaries" / "read_summary.json").read_text())
    for row in summary_rows:
        if int(row["attempt_count"]) != int(row["successful_count"]) + int(row["failure_count"]):
            problems.append(f"{row['database']}/{row['workload']}: attempt arithmetic")
        if (
            row.get("p50_ms") is not None
            and row.get("p95_ms") is not None
            and float(row["p95_ms"]) < float(row["p50_ms"])
        ):
            problems.append(f"{row['database']}/{row['workload']}: p95 below p50")
    result = {"valid": not problems, "problems": sorted(set(problems)), "raw_rows": len(rows)}
    _write_payload(campaign_dir / "summaries" / "completeness_validation.json", result)
    return result


def run_final_campaign(
    databases: tuple[str, ...] = DATABASES, campaign_dir: Path | None = None
) -> dict[str, Any]:
    """Execute the frozen sequential ingest/read campaign and persist raw observations."""
    load_dotenv()
    _prepare_runtime_environment()
    root = repository_root()
    profile = load_final_profile()
    fingerprint = configuration_fingerprint()
    started = datetime.now().astimezone()
    if campaign_dir is None:
        campaign_id = "final-" + started.astimezone().strftime("%Y%m%dT%H%M%SZ")
        campaign_dir = root / "results" / "final" / campaign_id
    else:
        campaign_id = json.loads(
            (campaign_dir / "campaign_manifest.json").read_text(encoding="utf-8")
        )["campaign_id"]
    for name in ("metadata", "ingest", "reads", "raw", "summaries"):
        (campaign_dir / name).mkdir(parents=True, exist_ok=True)
    environment = capture_environment()
    workload_path, workload_hash = write_workload_manifest()
    dataset = json.loads((root / "data" / "metadata" / "wiki_vote.json").read_text())
    fixture_hash = hashlib.sha256(
        b"".join(path.read_bytes() for path in sorted((root / "data" / "fixtures").glob("*.json")))
    ).hexdigest()
    existing_manifest = (
        json.loads((campaign_dir / "campaign_manifest.json").read_text(encoding="utf-8"))
        if (campaign_dir / "campaign_manifest.json").is_file()
        else {}
    )
    manifest: dict[str, Any] = {
        "campaign_id": campaign_id,
        "run_type": "final",
        "started_at": started.isoformat(),
        "git_commit_sha": _git_commit(),
        "configuration_fingerprint": fingerprint,
        "dataset_checksum": dataset["source_checksum"],
        "fixture_checksum": fixture_hash,
        "workload_manifest_hash": workload_hash,
        "fairness_manifest": "metadata/fairness_manifest.json",
        "platforms": list(DATABASES),
        "benchmark_client_environment": "metadata/environment.json",
        "profile": profile,
        "campaign_status": "running",
    }
    if existing_manifest:
        manifest["repair_attempt_failures"] = existing_manifest.get("platform_failures", [])
    _write_payload(campaign_dir / "campaign_manifest.json", manifest)
    _write_payload(campaign_dir / "metadata" / "environment.json", environment)
    _write_payload(
        campaign_dir / "metadata" / "workload_manifest.json", json.loads(workload_path.read_text())
    )
    raw_path = campaign_dir / "raw" / "read_raw.jsonl"
    ingest_rows: list[dict[str, Any]] = (
        json.loads((campaign_dir / "ingest" / "ingest_results.json").read_text())
        if (campaign_dir / "ingest" / "ingest_results.json").is_file()
        else []
    )
    summary_rows: list[dict[str, Any]] = (
        json.loads((campaign_dir / "summaries" / "read_summary.json").read_text())
        if (campaign_dir / "summaries" / "read_summary.json").is_file()
        else []
    )
    round_rows: list[dict[str, Any]] = (
        json.loads((campaign_dir / "summaries" / "round_summary.json").read_text())
        if (campaign_dir / "summaries" / "round_summary.json").is_file()
        else []
    )
    resource_rows: dict[str, Any] = (
        json.loads((campaign_dir / "metadata" / "resource_observations.json").read_text())
        if (campaign_dir / "metadata" / "resource_observations.json").is_file()
        else {}
    )
    errors: list[dict[str, Any]] = (
        json.loads((campaign_dir / "errors.json").read_text())
        if (campaign_dir / "errors.json").is_file()
        else []
    )
    platform_failures: list[dict[str, Any]] = []
    oracle = CanonicalOracle.from_data_root(root / "data")
    fixture_lookup, fixture_buckets, fixture_starts = fixture_values(root / "data")
    fixture_sets = {
        "point_lookup": fixture_lookup,
        "filtered_lookup": fixture_buckets,
        "one_hop": fixture_starts,
        "two_hop": fixture_starts,
        "three_hop": fixture_starts,
        "aggregation": [None],
    }
    try:
        _stop_local_databases()
        for database in databases:
            adapter = None
            try:
                if database in LOCAL_DATABASES:
                    _compose(database, "up")
                    _wait_for_container(database)
                # prepare() is the existing clean, driver-batched measured ingest path.
                from graphbench.workflows import prepare

                status = prepare(database)
                metrics = dict(status["load_metrics"])
                metrics.update(
                    {
                        "campaign_id": campaign_id,
                        "configuration_fingerprint": fingerprint,
                        "schema_setup_seconds": None,
                        "schema_setup_excluded": True,
                        "node_batch_size": metrics.get("batch_size"),
                        "relationship_batch_size": metrics.get("batch_size"),
                    }
                )
                ingest_rows.append(metrics)
                _write_payload(campaign_dir / "ingest" / f"{database}.json", metrics)
                preflight_result = preflight(database)
                _write_payload(
                    campaign_dir / "metadata" / f"preflight_{database}.json", preflight_result
                )
                resource_rows[database] = {
                    "campaign_id": campaign_id,
                    "configuration_fingerprint": fingerprint,
                    "resource_configuration": status.get("resource_configuration"),
                    "preflight": preflight_result.get("resources"),
                    "post_load_metadata": status.get("metadata"),
                }
                adapter = create_adapter(database)
                adapter.connect()
                for workload in READ_WORKLOADS:
                    measured_samples: list[Any] = []
                    fixtures = fixture_sets[workload]
                    # Exactly the frozen warm-up count, excluded from all measured statistics.
                    for iteration in range(int(profile["warmup_iterations"])):
                        fixture = fixtures[iteration % len(fixtures)]
                        operation, result_count, fixture_id = _fixture_operation(
                            adapter, workload, fixture
                        )
                        sample = timed_operation(
                            database=database,
                            workload=workload,
                            round_number=0,
                            iteration=iteration,
                            fixture_id=fixture_id,
                            operation=operation,
                            result_count=result_count,
                            warmup=True,
                        )
                        _append_jsonl(
                            raw_path,
                            _sample_record(
                                sample,
                                campaign_id,
                                fingerprint,
                                _expected_result(oracle, workload, fixture),
                            ),
                        )
                    for round_id in (1, 2, 3):
                        round_samples: list[Any] = []
                        for iteration in range(int(profile["measured_iterations"])):
                            fixture = fixtures[iteration % len(fixtures)]
                            operation, result_count, fixture_id = _fixture_operation(
                                adapter, workload, fixture
                            )
                            sample = timed_operation(
                                database=database,
                                workload=workload,
                                round_number=round_id,
                                iteration=iteration,
                                fixture_id=fixture_id,
                                operation=operation,
                                result_count=result_count,
                            )
                            expected = _expected_result(oracle, workload, fixture)
                            record = _sample_record(sample, campaign_id, fingerprint, expected)
                            _append_jsonl(raw_path, record)
                            round_samples.append(sample)
                            measured_samples.append(sample)
                            if not sample.success:
                                errors.append(
                                    {
                                        key: record.get(key)
                                        for key in (
                                            "campaign_id",
                                            "database",
                                            "workload",
                                            "round",
                                            "iteration",
                                            "fixture_id",
                                            "error_type",
                                            "error_message",
                                        )
                                    }
                                )
                        if workload == "aggregation":
                            if dict(adapter.aggregation()) != oracle.aggregation():
                                raise RuntimeError(
                                    f"{database}/{workload} correctness drift after "
                                    f"round {round_id}"
                                )
                        elif any(
                            sample.success
                            and sample.result_count
                            != _expected_result(
                                oracle, workload, fixtures[sample.iteration % len(fixtures)]
                            )
                            for sample in round_samples
                        ):
                            raise RuntimeError(
                                f"{database}/{workload} correctness drift after round {round_id}"
                            )
                        statistics = latency_statistics(round_samples)
                        round_rows.append(
                            {
                                "campaign_id": campaign_id,
                                "configuration_fingerprint": fingerprint,
                                "database": database,
                                "workload": workload,
                                "round": round_id,
                                **statistics,
                            }
                        )
                    statistics = latency_statistics(measured_samples)
                    medians = [
                        row["p50_ms"]
                        for row in round_rows
                        if row["database"] == database
                        and row["workload"] == workload
                        and row["p50_ms"] is not None
                    ]
                    statistics.update(
                        {
                            "campaign_id": campaign_id,
                            "configuration_fingerprint": fingerprint,
                            "database": database,
                            "workload": workload,
                            "round_median_stddev_ms": pstdev(medians) if len(medians) > 1 else 0.0,
                            "round_median_cv": (pstdev(medians) / (sum(medians) / len(medians)))
                            if medians and sum(medians)
                            else 0.0,
                        }
                    )
                    summary_rows.append(statistics)
            except Exception as exc:
                failure = {
                    "database": database,
                    "error_type": type(exc).__name__,
                    "message": sanitize_text(str(exc)),
                    "timestamp": datetime.now().astimezone().isoformat(),
                }
                platform_failures.append(failure)
                errors.append(failure)
            finally:
                if adapter is not None:
                    adapter.close()
                if database in LOCAL_DATABASES:
                    try:
                        _compose(database, "down")
                    except Exception as exc:
                        errors.append(
                            {
                                "database": database,
                                "error_type": type(exc).__name__,
                                "message": sanitize_text(str(exc)),
                            }
                        )
    finally:
        try:
            _stop_local_databases()
        except Exception as exc:
            errors.append({"error_type": type(exc).__name__, "message": sanitize_text(str(exc))})
    _write_payload(campaign_dir / "ingest" / "ingest_results.json", ingest_rows)
    _write_csv(campaign_dir / "ingest" / "ingest_results.csv", ingest_rows)
    _write_payload(campaign_dir / "summaries" / "read_summary.json", summary_rows)
    _write_csv(campaign_dir / "summaries" / "read_summary.csv", summary_rows)
    _write_payload(campaign_dir / "summaries" / "round_summary.json", round_rows)
    _write_csv(campaign_dir / "summaries" / "round_summary.csv", round_rows)
    _write_payload(campaign_dir / "metadata" / "resource_observations.json", resource_rows)
    _write_payload(campaign_dir / "errors.json", errors)
    try:
        fairness = write_fairness_manifest()
        _write_payload(campaign_dir / "metadata" / "fairness_manifest.json", fairness)
    except Exception as exc:
        platform_failures.append(
            {"error_type": type(exc).__name__, "message": sanitize_text(str(exc))}
        )
    completeness = (
        validate_final_campaign(campaign_dir)
        if raw_path.is_file() and summary_rows
        else {"valid": False, "problems": ["no complete final summaries"], "raw_rows": 0}
    )
    status = (
        "complete_with_measured_failures"
        if not platform_failures and any(row["failure_count"] for row in summary_rows)
        else ("complete" if not platform_failures and completeness["valid"] else "incomplete")
    )
    manifest.update(
        {
            "finished_at": datetime.now().astimezone().isoformat(),
            "campaign_status": status,
            "platform_failures": platform_failures,
            "result_completeness": completeness,
        }
    )
    _write_payload(campaign_dir / "campaign_manifest.json", manifest)
    return {
        "campaign_id": campaign_id,
        "campaign_dir": str(campaign_dir),
        "manifest": manifest,
        "ingest": ingest_rows,
        "summaries": summary_rows,
        "rounds": round_rows,
        "errors": errors,
        "completeness": completeness,
    }
