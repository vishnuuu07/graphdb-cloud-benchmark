from pathlib import Path

import pytest

from graphbench.models import LoadResult
from graphbench.workflows import CountMismatchError, prepare


class _Adapter:
    database_name = "fake"
    batch_size = 2

    def __init__(self, relationship_load: int = 1) -> None:
        self.relationship_load = relationship_load

    def connect(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def reset(self) -> None:
        pass

    def create_schema(self) -> None:
        pass

    def load_nodes(self, rows) -> LoadResult:
        assert list(rows) == [{"id": 1, "bucket": 1}]
        return LoadResult("fake", "nodes", 1, 1, 1000.0)

    def load_relationships(self, rows) -> LoadResult:
        assert list(rows) == [{"source_id": 1, "target_id": 1}]
        return LoadResult("fake", "relationships", 1, self.relationship_load, 500.0)

    def verify_counts(self) -> tuple[int, int]:
        return 1, self.relationship_load

    def platform_metadata(self):
        return {"server": "fake"}

    def observe_resources(self):
        return None

    def close(self) -> None:
        pass


def _patch_workflow_inputs(monkeypatch, tmp_path: Path, adapter: _Adapter) -> None:
    monkeypatch.setattr("graphbench.workflows.create_adapter", lambda _: adapter)
    monkeypatch.setattr("graphbench.workflows.repository_root", lambda: tmp_path)
    monkeypatch.setattr("graphbench.workflows._git_commit", lambda: "test")
    monkeypatch.setattr("graphbench.workflows.expected_counts", lambda: (1, 1))
    monkeypatch.setattr("graphbench.workflows._nodes", lambda: [{"id": 1, "bucket": 1}])
    monkeypatch.setattr(
        "graphbench.workflows._relationships", lambda: [{"source_id": 1, "target_id": 1}]
    )


def test_prepare_calculates_load_metrics(monkeypatch, tmp_path: Path) -> None:
    _patch_workflow_inputs(monkeypatch, tmp_path, _Adapter())
    monkeypatch.setattr("graphbench.workflows.validate_adapter", lambda _: ["point_lookup"])
    result = prepare("fake")
    assert result["load_metrics"]["nodes_per_second"] == 1.0
    assert result["load_metrics"]["relationships_per_second"] == 2.0
    assert result["load_metrics"]["total_load_seconds"] == 1.5


def test_prepare_fails_count_mismatch(monkeypatch, tmp_path: Path) -> None:
    _patch_workflow_inputs(monkeypatch, tmp_path, _Adapter(relationship_load=0))
    with pytest.raises(CountMismatchError, match="driver load incomplete"):
        prepare("fake")
