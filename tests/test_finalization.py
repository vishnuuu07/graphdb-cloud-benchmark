from __future__ import annotations

import json

import pytest

from graphbench.finalization import (
    configuration_fingerprint,
    dry_run,
    preflight,
    transport_baseline,
    write_workload_manifest,
)
from graphbench.models import ResourceObservation


def test_final_profile_is_frozen_to_required_parameters() -> None:
    from graphbench.config import load_final_profile

    profile = load_final_profile()
    assert profile["profile"] == "final"
    assert profile["warmup_iterations"] == 30
    assert profile["measured_iterations"] == 200
    assert profile["concurrency_levels"] == [1, 5, 10, 20, 40]


def test_workload_manifest_write_is_hash_stable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("graphbench.finalization.repository_root", lambda: tmp_path)
    first_path, first_hash = write_workload_manifest()
    second_path, second_hash = write_workload_manifest()
    assert first_path == second_path
    assert first_hash == second_hash
    assert json.loads(first_path.read_text())["workloads"]["three_hop"]["hop_depth"] == 3


def test_configuration_fingerprint_is_stable_for_frozen_inputs() -> None:
    assert configuration_fingerprint() == configuration_fingerprint()


def test_preflight_rejects_resource_mismatch(monkeypatch) -> None:
    class Adapter:
        def connect(self):
            pass

        def close(self):
            pass

        def health_check(self):
            return True

        def verify_counts(self):
            return 1, 1

        def observe_resources(self):
            return ResourceObservation("neo4j", "now", cpu_percent=0.5, memory_bytes=320)

    monkeypatch.setattr("graphbench.finalization.create_adapter", lambda _: Adapter())
    monkeypatch.setattr("graphbench.finalization.expected_counts", lambda: (1, 1))
    monkeypatch.setattr(
        "graphbench.finalization.load_final_profile",
        lambda: {"target_cpu_cores": 0.5, "target_memory_bytes": 256, "profile": "final"},
    )
    with pytest.raises(RuntimeError, match="final resource preflight failed"):
        preflight("neo4j")


def test_dry_run_is_tagged_and_separate_from_final_results(monkeypatch, tmp_path) -> None:
    class Adapter:
        def connect(self):
            pass

        def close(self):
            pass

        def point_lookup(self, value):
            return int(value == 1)

    monkeypatch.setattr("graphbench.finalization.preflight", lambda _: {"connection": True})
    monkeypatch.setattr("graphbench.finalization.create_adapter", lambda _: Adapter())
    monkeypatch.setattr("graphbench.finalization.fixture_values", lambda _: ([1, 1, 1], [], []))
    monkeypatch.setattr("graphbench.finalization.configuration_fingerprint", lambda: "fixed")
    monkeypatch.setattr("graphbench.finalization.repository_root", lambda: tmp_path)
    result = dry_run("fake")
    assert result["run_type"] == "dry_run"
    assert (tmp_path / "results" / "dry_runs" / "fake_dry_run.json").is_file()


def test_transport_baseline_is_diagnostic_only(monkeypatch) -> None:
    class Adapter:
        def connect(self):
            pass

        def close(self):
            pass

        def health_check(self):
            return True

    monkeypatch.setattr("graphbench.finalization.create_adapter", lambda _: Adapter())
    result = transport_baseline("fake", warmup=1, measured=2)
    assert result["diagnostic_only"] is True
    assert result["not_subtracted_from_query_latency"] is True
    assert result["statistics"]["attempt_count"] == 2
