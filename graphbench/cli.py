"""Command-line interface for safe local preparation and diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from graphbench.config import (
    ConfigurationError,
    load_benchmark_config,
    load_platform_configs,
    repository_root,
)
from graphbench.datasets.wiki_vote import DatasetError, WikiVoteDataset
from graphbench.environment import load_dotenv, sanitize_text
from graphbench.finalization import (
    DATABASES,
    capture_environment,
    dry_run,
    run_final_campaign,
    write_fairness_manifest,
    write_transport_baselines,
    write_workload_manifest,
)
from graphbench.mixed import run_final_mixed_campaign, validate_final_mixed_campaign
from graphbench.reporting import generate_charts
from graphbench.workflows import prepare, smoke, validate_adapter

ENVIRONMENT_KEYS = (
    "COGNODB_URI",
    "COGNODB_USER",
    "COGNODB_PASSWORD",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "MEMGRAPH_URI",
    "MEMGRAPH_USER",
    "MEMGRAPH_PASSWORD",
    "FALKORDB_HOST",
    "FALKORDB_PORT",
    "ARANGODB_URL",
    "ARANGODB_USER",
    "ARANGODB_PASSWORD",
)

LOCAL_CONTAINERS = (
    ("Neo4j", "neo4j", "graphbench-neo4j", "NEO4J", ("URI", "USER", "PASSWORD")),
    ("Memgraph", "memgraph", "graphbench-memgraph", "MEMGRAPH", ("URI",)),
    ("FalkorDB", "falkordb", "graphbench-falkordb", "FALKORDB", ("HOST", "PORT")),
    ("ArangoDB", "arangodb", "graphbench-arangodb", "ARANGODB", ("URL", "PASSWORD")),
)


def _dataset() -> WikiVoteDataset:
    config = load_benchmark_config()
    return WikiVoteDataset(repository_root() / "data", config.seed)


def _status(level: str, message: str) -> None:
    print(f"{level:<7} {message}")


def doctor() -> int:
    load_dotenv()
    errors = 0
    if (sys.version_info.major, sys.version_info.minor) >= (3, 11):
        _status("OK", f"Python {sys.version.split()[0]}")
    else:
        _status("ERROR", "Python 3.11 or newer is required")
        errors += 1
    try:
        load_benchmark_config()
        load_platform_configs()
        _status("OK", "benchmark and platform configurations are valid")
    except ConfigurationError as exc:
        _status("ERROR", str(exc))
        errors += 1
    try:
        metadata = _dataset().verify()
        counts = f"{metadata.node_count} nodes, {metadata.relationship_count} relationships"
        _status(
            "OK",
            f"dataset verified ({counts})",
        )
    except (ConfigurationError, DatasetError) as exc:
        _status("WARNING", f"dataset not ready: {exc}")
    for database, prefix in (("CognoDB", "COGNODB"),):
        fields = ("URI", "USER", "PASSWORD")
        configured = all(os.environ.get(f"{prefix}_{field}") for field in fields)
        _status(
            "OK" if configured else "WARNING",
            f"{database} credentials: {'configured' if configured else 'absent'}",
        )
    for command in ("docker", "git"):
        if shutil.which(command):
            _status("OK", "Docker: OK" if command == "docker" else "git command is available")
        else:
            _status(
                "WARNING",
                "Docker: unavailable" if command == "docker" else "git command unavailable",
            )
    for display, adapter_name, container_name, prefix, fields in LOCAL_CONTAINERS:
        configured = (
            True
            if adapter_name in {"memgraph", "falkordb"}
            else all(os.environ.get(f"{prefix}_{field}") for field in fields)
        )
        _status(
            "OK" if configured else "WARNING",
            f"{display} credentials/configuration: {'configured' if configured else 'absent'}",
        )
        if not shutil.which("docker"):
            continue
        container = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        state = (
            "running"
            if container.returncode == 0 and container.stdout.strip() == "true"
            else "not running"
        )
        _status("OK" if state == "running" else "WARNING", f"{display} container: {state}")
        if state != "running":
            continue
        try:
            from graphbench.adapters import create_adapter

            adapter = create_adapter(adapter_name)
            try:
                adapter.connect()
                _status("OK" if adapter.health_check() else "ERROR", f"{display} connectivity: OK")
            finally:
                adapter.close()
        except Exception as exc:
            _status("WARNING", f"{display} connectivity: unavailable ({sanitize_text(str(exc))})")
    if os.environ.get("COGNODB_URI") and os.environ.get("COGNODB_PASSWORD"):
        try:
            from graphbench.adapters import create_adapter

            adapter = create_adapter("cognodb")
            try:
                adapter.connect()
                _status("OK" if adapter.health_check() else "ERROR", "CognoDB connectivity: OK")
            finally:
                adapter.close()
        except Exception as exc:
            _status("WARNING", f"CognoDB connectivity: unavailable ({sanitize_text(str(exc))})")
    return 1 if errors else 0


def dataset_prepare() -> int:
    metadata = _dataset().prepare()
    counts = f"{metadata.node_count} nodes, {metadata.relationship_count} relationships"
    print(f"Prepared wiki-Vote: {counts}")
    return 0


def dataset_verify() -> int:
    metadata = _dataset().verify()
    counts = f"{metadata.node_count} nodes, {metadata.relationship_count} relationships"
    print(f"Verified wiki-Vote: {counts}")
    return 0


def unavailable(command: str) -> int:
    print(
        f"{command} is intentionally unavailable: "
        "this command has not been implemented yet; "
        "no benchmark result has been fabricated.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphbench", description="Reproducible graph database benchmark"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="check local prerequisites without revealing credentials")
    environment_parser = subcommands.add_parser(
        "environment", help="capture safe benchmark-client environment metadata"
    )
    environment_parser.add_subparsers(dest="environment_command", required=True).add_parser(
        "capture"
    )
    fairness_parser = subcommands.add_parser(
        "fairness", help="write frozen fairness and workload manifests"
    )
    fairness_parser.add_subparsers(dest="fairness_command", required=True).add_parser("freeze")
    baseline_parser = subcommands.add_parser(
        "transport-baseline", help="diagnostic-only cheapest-request transport measurements"
    )
    baseline_parser.add_argument("--database", choices=DATABASES, action="append")
    dataset_parser = subcommands.add_parser(
        "dataset", help="prepare or validate the SNAP wiki-Vote dataset"
    )
    dataset_commands = dataset_parser.add_subparsers(dest="dataset_command", required=True)
    dataset_commands.add_parser("prepare").set_defaults(dataset_action="prepare")
    dataset_commands.add_parser("verify").set_defaults(dataset_action="verify")
    for command in ("smoke", "prepare", "validate"):
        command_parser = subcommands.add_parser(command, help=f"{command} a configured database")
        command_parser.add_argument(
            "--database",
            choices=("cognodb", "neo4j", "memgraph", "falkordb", "arangodb"),
            required=True,
        )
    benchmark_parser = subcommands.add_parser(
        "benchmark",
        help="frozen final benchmark entry point; full execution remains explicitly gated",
    )
    benchmark_group = benchmark_parser.add_mutually_exclusive_group(required=True)
    benchmark_group.add_argument("--database", choices=DATABASES)
    benchmark_group.add_argument("--all", action="store_true")
    benchmark_parser.add_argument("--profile", choices=("final",), required=True)
    benchmark_parser.add_argument("--dry-run", action="store_true")
    mixed_parser = subcommands.add_parser(
        "mixed", help="run the frozen mixed read/write stage in the existing final campaign"
    )
    mixed_parser.add_argument("--profile", choices=("final",), required=True)
    mixed_parser.add_argument(
        "--campaign-dir",
        type=Path,
        default=repository_root() / "results" / "final" / "final-20260820T022802Z",
    )
    mixed_parser.add_argument("--database", choices=DATABASES, action="append")
    subcommands.add_parser(
        "mixed-validate", help="run post-workload canonical checks without measuring"
    ).add_argument(
        "--campaign-dir",
        type=Path,
        default=repository_root() / "results" / "final" / "final-20260820T022802Z",
    )
    report_parser = subcommands.add_parser(
        "report", help="generate charts from the frozen final campaign without rerunning it"
    )
    report_parser.add_argument(
        "--campaign-dir",
        type=Path,
        default=repository_root() / "results" / "final" / "final-20260820T022802Z",
    )
    report_parser.add_argument(
        "--output-dir", type=Path, default=repository_root() / "charts"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "environment":
            payload = capture_environment()
            print(f"environment captured: {payload['configuration_fingerprint']}")
            return 0
        if args.command == "fairness":
            workload_path, workload_hash = write_workload_manifest()
            payload = write_fairness_manifest()
            print(f"fairness manifest frozen: {payload['configuration_fingerprint']}")
            print(f"workload manifest: {workload_path.name} ({workload_hash})")
            return 0
        if args.command == "transport-baseline":
            payload = write_transport_baselines(tuple(args.database or DATABASES))
            print(f"transport baseline recorded for: {', '.join(payload['results'])}")
            return 0
        if args.command == "dataset":
            return dataset_prepare() if args.dataset_action == "prepare" else dataset_verify()
        if args.command == "smoke":
            result = smoke(args.database)
            print(f"{args.database} smoke: {'OK' if result['connection_success'] else 'FAILED'}")
            print(f"metadata: {result['metadata']}")
            return 0 if result["connection_success"] else 1
        if args.command == "prepare":
            result = prepare(args.database)
            print(
                f"{args.database} prepare: OK "
                f"({result['actual_node_count']} nodes, "
                f"{result['actual_relationship_count']} relationships)"
            )
            return 0
        if args.command == "validate":
            from graphbench.adapters import create_adapter

            adapter = create_adapter(args.database)
            try:
                adapter.connect()
                tested = validate_adapter(adapter)
            finally:
                adapter.close()
            print(f"{args.database} validation: OK ({', '.join(tested)})")
            return 0
        if args.command == "benchmark":
            if not args.dry_run:
                if not args.all:
                    return unavailable("single-platform final benchmark")
                result = run_final_campaign()
                print(f"final campaign: {result['campaign_id']}")
                print(f"status: {result['manifest']['campaign_status']}")
                return 0 if result["manifest"]["campaign_status"] != "incomplete" else 1
            databases = DATABASES if args.all else (args.database,)
            for database in databases:
                result = dry_run(database)
                print(f"{database} dry-run: OK ({result['configuration_fingerprint']})")
            return 0
        if args.command == "mixed":
            result = run_final_mixed_campaign(
                args.campaign_dir,
                tuple(args.database) if args.database else DATABASES,
            )
            print(f"mixed campaign: {result['campaign_id']}")
            print(f"status: {result['manifest']['campaign_status']}")
            return 0 if result["completeness"]["valid"] else 1
        if args.command == "mixed-validate":
            result = validate_final_mixed_campaign(args.campaign_dir)
            print(json.dumps(result, sort_keys=True))
            return 0 if all(item.get("status") == "PASS" for item in result.values()) else 1
        if args.command == "report":
            paths = generate_charts(args.campaign_dir, args.output_dir)
            for path in paths:
                print(f"generated: {path}")
            return 0
        return unavailable(args.command)
    except Exception as exc:
        print(f"ERROR: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
