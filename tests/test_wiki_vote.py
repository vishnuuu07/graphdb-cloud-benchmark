import csv
import json
from pathlib import Path

import pytest

from graphbench.datasets.wiki_vote import (
    DERIVED_PROPERTY_DESCRIPTION,
    DatasetError,
    WikiVoteDataset,
    derive_bucket,
    node_ids_from_edges,
    parse_snap_lines,
)
from graphbench.models import DatasetMetadata


def test_parse_snap_preserves_topology_and_ignores_comments() -> None:
    edges = parse_snap_lines(["# source target\n", "2 1\n", "1 2\n", "2 1\n"])
    assert edges == [(2, 1), (1, 2), (2, 1)]
    assert node_ids_from_edges(edges) == [1, 2]


@pytest.mark.parametrize("row", ["1\n", "a 2\n", "1 2 3\n", "-1 2\n"])
def test_parse_snap_rejects_malformed_rows(row: str) -> None:
    with pytest.raises(DatasetError, match="Malformed SNAP row"):
        parse_snap_lines([row])


def test_bucket_derivation() -> None:
    assert derive_bucket(65) == 1


def test_fixture_generation_is_seeded_and_repeatable(tmp_path: Path) -> None:
    first = WikiVoteDataset(tmp_path / "first", seed=12)
    second = WikiVoteDataset(tmp_path / "second", seed=12)
    nodes = list(range(150))
    edges = [(node, (node + 1) % 150) for node in nodes]
    first._write_fixtures(nodes, edges)
    second._write_fixtures(nodes, edges)
    for fixture_name in ("start_nodes.json", "lookup_ids.json", "buckets.json"):
        assert (first.fixtures_dir / fixture_name).read_text() == (
            second.fixtures_dir / fixture_name
        ).read_text()


def test_dataset_validation_and_deterministic_fixtures(tmp_path: Path) -> None:
    dataset = WikiVoteDataset(tmp_path, seed=7)
    dataset.nodes_path.parent.mkdir(parents=True)
    with dataset.nodes_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "bucket"])
        writer.writeheader()
        writer.writerows({"id": item, "bucket": item % 32} for item in range(4))
    with dataset.relationships_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_id", "target_id"])
        writer.writeheader()
        writer.writerows({"source_id": 0, "target_id": 1} for _ in range(100_000))
    dataset.fixtures_dir.mkdir()
    (dataset.fixtures_dir / "start_nodes.json").write_text("[0]", encoding="utf-8")
    (dataset.fixtures_dir / "lookup_ids.json").write_text("[0, 1]", encoding="utf-8")
    (dataset.fixtures_dir / "buckets.json").write_text(
        json.dumps(list(range(32))), encoding="utf-8"
    )
    metadata = DatasetMetadata(
        source_url="https://snap.stanford.edu/data/wiki-Vote.txt.gz",
        source_name="Stanford SNAP wiki-Vote directed graph",
        download_timestamp="2026-01-01T00:00:00+00:00",
        source_checksum="not-checked-without-raw-file",
        node_count=4,
        relationship_count=100_000,
        random_seed=7,
        derived_property_description=DERIVED_PROPERTY_DESCRIPTION,
    )
    dataset.metadata_path.parent.mkdir()
    dataset.metadata_path.write_text(json.dumps(metadata.as_dict()), encoding="utf-8")
    assert dataset.verify().relationship_count == 100_000
    (dataset.fixtures_dir / "lookup_ids.json").write_text("[99]", encoding="utf-8")
    with pytest.raises(DatasetError, match="unknown"):
        dataset.verify()
