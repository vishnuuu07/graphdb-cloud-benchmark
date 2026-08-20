"""Independent canonical correctness oracle over the checked-in processed Wiki-Vote files."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CanonicalOracle:
    """Reference answers that preserve path multiplicity, including duplicate edges."""

    node_ids: frozenset[int]
    bucket_counts: dict[int, int]
    adjacency: dict[int, tuple[tuple[int, int], ...]]

    @classmethod
    def from_data_root(cls, data_root: Path) -> CanonicalOracle:
        node_ids: set[int] = set()
        bucket_counts: Counter[int] = Counter()
        with (data_root / "processed" / "nodes.csv").open(newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                node_ids.add(int(row["id"]))
                bucket_counts[int(row["bucket"])] += 1
        adjacency_lists: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
        with (data_root / "processed" / "relationships.csv").open(
            newline="", encoding="utf-8"
        ) as source:
            for relationship_id, row in enumerate(csv.DictReader(source)):
                adjacency_lists[int(row["source_id"])].append(
                    (relationship_id, int(row["target_id"]))
                )
        return cls(
            frozenset(node_ids),
            dict(bucket_counts),
            {key: tuple(value) for key, value in adjacency_lists.items()},
        )

    def point_lookup(self, user_id: int) -> int:
        return int(user_id in self.node_ids)

    def filtered_lookup(self, bucket: int) -> int:
        return self.bucket_counts.get(bucket, 0)

    def path_count(self, start_id: int, depth: int) -> int:
        if depth < 1:
            raise ValueError("depth must be positive")
        # Cypher's fixed-pattern semantics do not allow the same relationship instance to
        # occur twice in one matched path. Count directly rather than storing path tuples.
        if depth == 1:
            return len(self.adjacency.get(start_id, ()))
        total = 0
        for first_id, first_target in self.adjacency.get(start_id, ()):
            for second_id, second_target in self.adjacency.get(first_target, ()):
                if second_id == first_id:
                    continue
                if depth == 2:
                    total += 1
                    continue
                for third_id, _ in self.adjacency.get(second_target, ()):
                    if third_id != first_id and third_id != second_id:
                        total += 1
        return total

    def aggregation(self) -> dict[int, int]:
        return dict(sorted(self.bucket_counts.items()))


def fixture_values(data_root: Path) -> tuple[list[int], list[int], list[int]]:
    fixtures = data_root / "fixtures"
    return (
        [
            int(value)
            for value in json.loads((fixtures / "lookup_ids.json").read_text(encoding="utf-8"))
        ],
        [
            int(value)
            for value in json.loads((fixtures / "buckets.json").read_text(encoding="utf-8"))
        ],
        [
            int(value)
            for value in json.loads((fixtures / "start_nodes.json").read_text(encoding="utf-8"))
        ],
    )
