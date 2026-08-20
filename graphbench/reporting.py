"""Deterministic publication charts generated from a frozen campaign."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATABASES = ("cognodb", "neo4j", "memgraph", "falkordb", "arangodb")
DISPLAY_NAMES = {
    "cognodb": "CognoDB Cloud",
    "neo4j": "Neo4j",
    "memgraph": "Memgraph",
    "falkordb": "FalkorDB",
    "arangodb": "ArangoDB",
}
COLORS = {
    "cognodb": "#0f766e",
    "neo4j": "#2563eb",
    "memgraph": "#dc2626",
    "falkordb": "#7c3aed",
    "arangodb": "#d97706",
}
WORKLOADS = (
    ("point_lookup", "Point"),
    ("filtered_lookup", "Filtered"),
    ("one_hop", "1-hop"),
    ("two_hop", "2-hop"),
    ("three_hop", "3-hop"),
    ("aggregation", "Aggregation"),
)
CONCURRENCY = (1, 5, 10, 20, 40)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _check_frozen_campaign(campaign_dir: Path) -> dict[str, Any]:
    manifest = _load_json(campaign_dir / "campaign_manifest.json")
    if not manifest.get("results_frozen") or manifest.get("integrity_audit") != "passed":
        raise ValueError("reporting requires a completed campaign with a passed integrity audit")
    return manifest


def _configure_matplotlib() -> Any:
    try:
        import matplotlib
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on installation extras
        raise RuntimeError(
            "matplotlib is required for report generation; install the dev dependencies"
        ) from exc

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.titlesize": 13,
            "axes.labelsize": 10,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 9,
            "figure.dpi": 120,
            "savefig.dpi": 180,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linewidth": 0.7,
        }
    )
    return plt


def _save(fig: Any, path: Path) -> None:
    fig.savefig(
        path,
        dpi=180,
        bbox_inches="tight",
        metadata={"Software": "graphbench", "Title": path.stem},
    )
    fig.clf()


def _read_rows(campaign_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = _load_json(campaign_dir / "summaries" / "read_summary.json")
    return {(row["database"], row["workload"]): row for row in rows}


def _mixed_rows(campaign_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    rows = _load_json(campaign_dir / "summaries" / "mixed_summary.json")
    return {(row["database"], row["concurrency"]): row for row in rows}


def _plot_read(
    plt: Any, read: dict[tuple[str, str], dict[str, Any]], metric: str, path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.8), constrained_layout=True)
    x = list(range(len(WORKLOADS)))
    for database in DATABASES:
        values = [read[(database, workload)][metric] for workload, _ in WORKLOADS]
        ax.plot(
            x,
            values,
            marker="o",
            linewidth=2,
            label=DISPLAY_NAMES[database],
            color=COLORS[database],
        )
    ax.set_xticks(x, [label for _, label in WORKLOADS])
    ax.set_ylabel("Latency (ms)")
    ax.set_title(f"Read {metric.removesuffix('_ms').upper()} latency by workload")
    ax.set_yscale("log")
    ax.set_axisbelow(True)
    ax.legend(ncols=3, loc="upper left", frameon=False)
    _save(fig, path)


def generate_charts(campaign_dir: Path, output_dir: Path) -> list[Path]:
    """Generate publication charts from one audited, frozen campaign."""

    _check_frozen_campaign(campaign_dir)
    ingest = _load_json(campaign_dir / "ingest" / "ingest_results.json")
    read = _read_rows(campaign_dir)
    mixed = _mixed_rows(campaign_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plt = _configure_matplotlib()
    paths: list[Path] = []

    fig, ax = plt.subplots(figsize=(9.5, 5.5), constrained_layout=True)
    values = {row["database"]: row["relationships_per_second"] for row in ingest}
    ax.bar(
        [DISPLAY_NAMES[database] for database in DATABASES],
        [values[database] for database in DATABASES],
        color=[COLORS[database] for database in DATABASES],
    )
    ax.set_ylabel("Relationships per second")
    ax.set_title("Relationship ingest throughput")
    ax.tick_params(axis="x", rotation=18)
    path = output_dir / "ingest-relationship-throughput.png"
    _save(fig, path)
    paths.append(path)

    for metric, filename in (
        ("p50_ms", "read-p50-latency.png"),
        ("p95_ms", "read-p95-latency.png"),
    ):
        path = output_dir / filename
        _plot_read(plt, read, metric, path)
        paths.append(path)

    fig, ax = plt.subplots(figsize=(9.5, 5.5), constrained_layout=True)
    for database in DATABASES:
        ax.plot(
            CONCURRENCY,
            [mixed[(database, level)]["successful_qps"] for level in CONCURRENCY],
            marker="o",
            linewidth=2,
            label=DISPLAY_NAMES[database],
            color=COLORS[database],
        )
    ax.set_xlabel("Concurrent workers")
    ax.set_ylabel("Successful throughput (QPS)")
    ax.set_title("Mixed workload throughput (80% reads / 20% writes)")
    ax.set_yscale("log")
    ax.set_xticks(CONCURRENCY)
    ax.legend(ncols=3, loc="upper left", frameon=False)
    path = output_dir / "mixed-throughput-qps.png"
    _save(fig, path)
    paths.append(path)

    fig, ax = plt.subplots(figsize=(9.5, 5.5), constrained_layout=True)
    for database in DATABASES:
        ax.plot(
            CONCURRENCY,
            [mixed[(database, level)]["p95_ms"] for level in CONCURRENCY],
            marker="o",
            linewidth=2,
            label=DISPLAY_NAMES[database],
            color=COLORS[database],
        )
    ax.set_xlabel("Concurrent workers")
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title("Mixed workload p95 latency")
    ax.set_yscale("log")
    ax.set_xticks(CONCURRENCY)
    ax.legend(ncols=3, loc="upper left", frameon=False)
    path = output_dir / "mixed-p95-latency.png"
    _save(fig, path)
    paths.append(path)
    return paths


def report_not_available(results_root: Path) -> str:
    """Compatibility message retained for callers from the initial scaffold."""

    return f"No report was generated (expected results directory: {results_root})."
