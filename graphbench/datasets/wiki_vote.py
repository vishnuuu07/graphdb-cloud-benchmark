"""Authoritative SNAP wiki-Vote download, processing, and validation."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import random
import shutil
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from graphbench.models import DatasetMetadata

SOURCE_URL = "https://snap.stanford.edu/data/wiki-Vote.txt.gz"
SOURCE_NAME = "Stanford SNAP wiki-Vote directed graph"
DERIVED_PROPERTY_DESCRIPTION = (
    "bucket is a deterministic benchmark-only property calculated as id % 32; "
    "it supports identical filtered and group-by workloads and does not change graph topology."
)


class DatasetError(ValueError):
    """Raised for an invalid source, derived file, or metadata mismatch."""


def derive_bucket(user_id: int) -> int:
    return user_id % 32


def parse_snap_lines(lines: Iterable[str]) -> list[tuple[int, int]]:
    """Parse SNAP two-column edge lines while preserving their order and duplicates."""
    edges: list[tuple[int, int]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split()
        if len(columns) != 2:
            raise DatasetError(f"Malformed SNAP row {line_number}: expected two columns")
        try:
            source, target = (int(column) for column in columns)
        except ValueError as exc:
            raise DatasetError(f"Malformed SNAP row {line_number}: IDs must be integers") from exc
        if source < 0 or target < 0:
            raise DatasetError(f"Malformed SNAP row {line_number}: IDs must be non-negative")
        edges.append((source, target))
    if not edges:
        raise DatasetError("SNAP source contains no relationships")
    return edges


def node_ids_from_edges(edges: list[tuple[int, int]]) -> list[int]:
    return sorted({endpoint for edge in edges for endpoint in edge})


class WikiVoteDataset:
    """Owns only files beneath the project's data directory."""

    def __init__(self, root: Path, seed: int) -> None:
        self.root = root
        self.seed = seed
        self.raw_path = root / "raw" / "wiki-Vote.txt.gz"
        self.nodes_path = root / "processed" / "nodes.csv"
        self.relationships_path = root / "processed" / "relationships.csv"
        self.metadata_path = root / "metadata" / "wiki_vote.json"
        self.fixtures_dir = root / "fixtures"

    def prepare(self) -> DatasetMetadata:
        try:
            return self.verify()
        except DatasetError:
            pass
        if not self.raw_path.is_file():
            self._download()
        edges = self._read_source()
        nodes = node_ids_from_edges(edges)
        self._write_processed(nodes, edges)
        self._write_fixtures(nodes, edges)
        metadata = DatasetMetadata(
            source_url=SOURCE_URL,
            source_name=SOURCE_NAME,
            download_timestamp=datetime.now(UTC).isoformat(),
            source_checksum=_sha256(self.raw_path),
            node_count=len(nodes),
            relationship_count=len(edges),
            random_seed=self.seed,
            derived_property_description=DERIVED_PROPERTY_DESCRIPTION,
        )
        self._write_json(self.metadata_path, metadata.as_dict())
        return self.verify()

    def verify(self) -> DatasetMetadata:
        required = [self.nodes_path, self.relationships_path, self.metadata_path]
        required.extend(
            self.fixtures_dir / name
            for name in ("start_nodes.json", "lookup_ids.json", "buckets.json")
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise DatasetError(f"Dataset is incomplete; missing: {', '.join(missing)}")
        nodes = self._read_nodes()
        edges = self._read_relationships()
        if len(nodes) != len(set(nodes)):
            raise DatasetError("Canonical nodes do not have unique IDs")
        if any(bucket != derive_bucket(user_id) for user_id, bucket in nodes.items()):
            raise DatasetError("A node bucket does not equal id % 32")
        node_set = set(nodes)
        if any(source not in node_set or target not in node_set for source, target in edges):
            raise DatasetError("A relationship endpoint does not exist in nodes.csv")
        if len(edges) < 100_000:
            raise DatasetError("wiki-Vote must contain at least 100,000 relationships")
        payload = self._read_json(self.metadata_path)
        try:
            metadata = DatasetMetadata(**payload)
        except TypeError as exc:
            raise DatasetError(f"Invalid dataset metadata: {exc}") from exc
        if metadata.node_count != len(nodes) or metadata.relationship_count != len(edges):
            raise DatasetError("Dataset metadata counts do not agree with processed files")
        if metadata.random_seed != self.seed:
            raise DatasetError("Dataset metadata seed differs from benchmark configuration")
        if metadata.source_url != SOURCE_URL or metadata.source_name != SOURCE_NAME:
            raise DatasetError(
                "Dataset metadata source does not match the authoritative SNAP source"
            )
        if metadata.derived_property_description != DERIVED_PROPERTY_DESCRIPTION:
            raise DatasetError("Dataset metadata derived property description does not match")
        if self.raw_path.is_file() and metadata.source_checksum != _sha256(self.raw_path):
            raise DatasetError("Dataset metadata checksum does not match downloaded source")
        self._verify_fixtures(nodes, edges)
        return metadata

    def _download(self) -> None:
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            urlopen(SOURCE_URL, timeout=60) as response,
            tempfile.NamedTemporaryFile(dir=self.raw_path.parent, delete=False) as temporary,
        ):
            shutil.copyfileobj(response, temporary)
            temporary_path = Path(temporary.name)
        temporary_path.replace(self.raw_path)

    def _read_source(self) -> list[tuple[int, int]]:
        try:
            with gzip.open(self.raw_path, mode="rt", encoding="utf-8") as source:
                return parse_snap_lines(source)
        except OSError as exc:
            raise DatasetError(f"Unable to read compressed SNAP source: {exc}") from exc

    def _write_processed(self, nodes: list[int], edges: list[tuple[int, int]]) -> None:
        self.nodes_path.parent.mkdir(parents=True, exist_ok=True)
        with self.nodes_path.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=["id", "bucket"])
            writer.writeheader()
            writer.writerows({"id": user_id, "bucket": derive_bucket(user_id)} for user_id in nodes)
        with self.relationships_path.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=["source_id", "target_id"])
            writer.writeheader()
            writer.writerows({"source_id": source, "target_id": target} for source, target in edges)

    def _write_fixtures(self, nodes: list[int], edges: list[tuple[int, int]]) -> None:
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        generator = random.Random(self.seed)
        outgoing_nodes = sorted({source for source, _ in edges})
        self._write_json(
            self.fixtures_dir / "start_nodes.json",
            generator.sample(outgoing_nodes, min(100, len(outgoing_nodes))),
        )
        self._write_json(
            self.fixtures_dir / "lookup_ids.json", generator.sample(nodes, min(100, len(nodes)))
        )
        self._write_json(self.fixtures_dir / "buckets.json", list(range(32)))

    def _read_nodes(self) -> dict[int, int]:
        try:
            with self.nodes_path.open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames != ["id", "bucket"]:
                    raise DatasetError("nodes.csv must have id,bucket headers")
                records = [(int(row["id"]), int(row["bucket"])) for row in reader]
                nodes = dict(records)
                if len(nodes) != len(records):
                    raise DatasetError("Canonical nodes do not have unique IDs")
                return nodes
        except (OSError, ValueError, KeyError) as exc:
            raise DatasetError(f"Invalid nodes.csv: {exc}") from exc

    def _read_relationships(self) -> list[tuple[int, int]]:
        try:
            with self.relationships_path.open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames != ["source_id", "target_id"]:
                    raise DatasetError("relationships.csv must have source_id,target_id headers")
                return [(int(row["source_id"]), int(row["target_id"])) for row in reader]
        except (OSError, ValueError, KeyError) as exc:
            raise DatasetError(f"Invalid relationships.csv: {exc}") from exc

    def _verify_fixtures(self, nodes: dict[int, int], edges: list[tuple[int, int]]) -> None:
        start_nodes = self._read_json(self.fixtures_dir / "start_nodes.json")
        lookup_ids = self._read_json(self.fixtures_dir / "lookup_ids.json")
        buckets = self._read_json(self.fixtures_dir / "buckets.json")
        node_set = set(nodes)
        outgoing_nodes = {source for source, _ in edges}
        if not isinstance(start_nodes, list) or not set(start_nodes) <= outgoing_nodes:
            raise DatasetError("start_nodes fixture references non-traversable node IDs")
        if not isinstance(lookup_ids, list) or not set(lookup_ids) <= node_set:
            raise DatasetError("lookup_ids fixture references unknown node IDs")
        if buckets != list(range(32)):
            raise DatasetError("buckets fixture must be the deterministic sequence 0 through 31")

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetError(f"Invalid JSON file {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
