"""Command-line interface for safe local preparation and diagnostics."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from graphbench.config import (
    ConfigurationError,
    load_benchmark_config,
    load_platform_configs,
    repository_root,
)
from graphbench.datasets.wiki_vote import DatasetError, WikiVoteDataset
from graphbench.reporting import report_not_available

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
    "ARANGO_URL",
    "ARANGO_USER",
    "ARANGO_PASSWORD",
)


def _dataset() -> WikiVoteDataset:
    config = load_benchmark_config()
    return WikiVoteDataset(repository_root() / "data", config.seed)


def _status(level: str, message: str) -> None:
    print(f"{level:<7} {message}")


def doctor() -> int:
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
    missing = [key for key in ENVIRONMENT_KEYS if not os.environ.get(key)]
    if missing:
        _status("WARNING", f"database environment settings absent: {', '.join(missing)}")
    else:
        _status("OK", "all database environment settings are present")
    for command in ("docker", "git"):
        if shutil.which(command):
            _status("OK", f"{command} command is available")
        else:
            _status("WARNING", f"{command} command is not available")
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
        "concrete database adapters are not implemented yet; "
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
    dataset_parser = subcommands.add_parser(
        "dataset", help="prepare or validate the SNAP wiki-Vote dataset"
    )
    dataset_commands = dataset_parser.add_subparsers(dest="dataset_command", required=True)
    dataset_commands.add_parser("prepare").set_defaults(dataset_action="prepare")
    dataset_commands.add_parser("verify").set_defaults(dataset_action="verify")
    for command in ("smoke", "benchmark", "report"):
        subcommands.add_parser(command, help=f"reserved until adapters are implemented ({command})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "dataset":
            return dataset_prepare() if args.dataset_action == "prepare" else dataset_verify()
        if args.command == "report":
            print(report_not_available(Path("results")), file=sys.stderr)
            return 2
        return unavailable(args.command)
    except (ConfigurationError, DatasetError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
