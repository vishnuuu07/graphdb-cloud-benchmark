from collections.abc import Mapping

import pytest

from graphbench.adapters import create_adapter
from graphbench.adapters.arangodb import ArangoDBAdapter
from graphbench.adapters.cognodb import CognoDBAdapter
from graphbench.adapters.cypher import CypherGraphAdapter, batches
from graphbench.adapters.falkordb import FalkorDBAdapter
from graphbench.adapters.memgraph import MemgraphAdapter
from graphbench.adapters.neo4j import Neo4jAdapter
from graphbench.oracle import CanonicalOracle
from graphbench.workflows import ValidationError, validate_adapter


def test_batches_preserve_all_rows_and_sizes() -> None:
    groups = list(batches(({"id": value} for value in range(5)), 2))
    assert groups == [[{"id": 0}, {"id": 1}], [{"id": 2}, {"id": 3}], [{"id": 4}]]


def test_adapter_factory_registration(monkeypatch) -> None:
    cognodb = object()
    neo4j = object()
    memgraph = object()
    falkordb = object()
    arangodb = object()
    monkeypatch.setattr(CognoDBAdapter, "from_environment", lambda: cognodb)
    monkeypatch.setattr(Neo4jAdapter, "from_environment", lambda: neo4j)
    monkeypatch.setattr(MemgraphAdapter, "from_environment", lambda: memgraph)
    monkeypatch.setattr(FalkorDBAdapter, "from_environment", lambda: falkordb)
    monkeypatch.setattr(ArangoDBAdapter, "from_environment", lambda: arangodb)
    assert create_adapter("cognodb") is cognodb
    assert create_adapter("neo4j") is neo4j
    assert create_adapter("memgraph") is memgraph
    assert create_adapter("falkordb") is falkordb
    assert create_adapter("arangodb") is arangodb


def test_memgraph_schema_uses_equivalent_label_property_indexes() -> None:
    adapter = object.__new__(MemgraphAdapter)
    seen: list[str] = []
    adapter._write = lambda query: seen.append(query)  # type: ignore[method-assign]
    adapter.create_schema()
    assert seen == ["CREATE INDEX ON :User(id)", "CREATE INDEX ON :User(bucket)"]


def test_falkordb_hops_are_fixed_outgoing_patterns() -> None:
    adapter = object.__new__(FalkorDBAdapter)
    seen: list[str] = []

    def execute(query: str, parameters: Mapping[str, int]) -> list[list[int]]:
        seen.append(query)
        assert parameters == {"id": 7}
        return [[3]]

    adapter._execute = execute  # type: ignore[method-assign]
    assert adapter._hop(7, 3) == 3
    assert "ID(r2) <> ID(r1)" in seen[0]
    assert "ID(r3) <> ID(r1)" in seen[0]


def test_arangodb_edge_mapping_preserves_canonical_direction() -> None:
    adapter = object.__new__(ArangoDBAdapter)
    adapter.users = "users"
    calls: list[tuple[str, list[dict[str, str]]]] = []

    class Collection:
        def insert_many(self, documents):
            calls.append(("voted_for", documents))
            return [{} for _ in documents]

    class Database:
        def collection(self, name: str):
            assert name == "voted_for"
            return Collection()

    adapter._database = lambda: Database()  # type: ignore[method-assign]
    adapter.batch_size = 2
    adapter.database_name = "arangodb"
    result = adapter.load_relationships([{"source_id": 4, "target_id": 9}])
    assert result.loaded == 1
    assert calls == [("voted_for", [{"_from": "users/4", "_to": "users/9"}])]


def test_arangodb_exact_depth_uses_path_edge_uniqueness() -> None:
    adapter = object.__new__(ArangoDBAdapter)
    seen: list[str] = []

    def aql(query: str, bind_vars: Mapping[str, int]) -> list[int]:
        seen.append(query)
        assert bind_vars == {"id": 11}
        return [5]

    adapter._aql = aql  # type: ignore[method-assign]
    assert adapter._hop(11, 3) == 5
    assert "3..3 OUTBOUND" in seen[0]
    assert 'uniqueVertices: "none"' in seen[0]
    assert 'uniqueEdges: "path"' in seen[0]


def test_cognodb_uses_verified_certifi_bundle_for_secure_endpoint() -> None:
    adapter = CognoDBAdapter(
        uri="bolt+s://example.databases.cognodb.cloud",
        user="cognodb",
        password="test-value",
        batch_size=1,
    )
    assert adapter._driver_uri() == "bolt://example.databases.cognodb.cloud"
    configuration = adapter._driver_configuration()
    assert configuration["encrypted"] is True
    assert type(configuration["trusted_certificates"]).__name__ == "TrustCustomCAs"


def test_index_observation_accepts_cognodb_show_format() -> None:
    adapter = object.__new__(CypherGraphAdapter)

    def execute(query: str):
        if query == "SHOW INDEXES YIELD *":
            return [{"label": "User", "properties": "bucket", "type": "RANGE"}]
        if query == "SHOW CONSTRAINTS YIELD *":
            return [{"label": "User", "properties": "id", "kind": "UNIQUE"}]
        raise AssertionError(query)

    adapter._execute = execute  # type: ignore[method-assign]
    assert adapter._observable_index_properties() == {"id", "bucket"}


def test_exact_hop_query_has_no_variable_length_pattern() -> None:
    adapter = object.__new__(CypherGraphAdapter)
    seen: list[str] = []

    def execute(query: str, parameters: Mapping[str, int]) -> list[dict[str, int]]:
        seen.append(query)
        assert parameters == {"id": 7}
        return [{"count": 3}]

    adapter._execute = execute  # type: ignore[method-assign]
    assert adapter._hop(7, 3) == 3
    assert seen == [
        "MATCH (:User {id: $id})-[:VOTED_FOR]->()-[:VOTED_FOR]->()-[:VOTED_FOR]->(:User) "
        "RETURN count(*) AS count"
    ]


def test_benchmark_write_and_reset_only_touch_counter() -> None:
    adapter = object.__new__(CypherGraphAdapter)
    seen: list[str] = []

    def write(query: str, parameters=None):
        seen.append(query)
        return [{"count": 1}]

    adapter._write = write  # type: ignore[method-assign]
    assert adapter.mixed_write({"id": 4}) == 1
    adapter.reset_write_state()
    assert "benchmark_counter" in seen[0]
    assert "id" not in seen[1]
    assert seen[1] == "MATCH (u:User) REMOVE u.benchmark_counter"


def test_oracle_counts_duplicate_paths_without_materializing_paths() -> None:
    oracle = CanonicalOracle(
        node_ids=frozenset({1, 2, 3}),
        bucket_counts={1: 2, 2: 1},
        adjacency={1: ((1, 2), (2, 2), (3, 3)), 2: ((4, 3),), 3: ()},
    )
    assert oracle.point_lookup(1) == 1
    assert oracle.filtered_lookup(1) == 2
    assert oracle.path_count(1, 1) == 3
    assert oracle.path_count(1, 2) == 2
    assert oracle.path_count(1, 3) == 0


def test_validator_reports_fixture_and_expected_actual(monkeypatch) -> None:
    class WrongAdapter:
        def point_lookup(self, user_id: int) -> int:
            return 0

        def __getattr__(self, name: str):
            return lambda *args: 0

    monkeypatch.setattr("graphbench.workflows.fixture_values", lambda _: ([1], [0], [1]))
    monkeypatch.setattr(
        "graphbench.workflows.CanonicalOracle.from_data_root",
        lambda _: CanonicalOracle(frozenset({1}), {0: 1}, {1: ()}),
    )
    with pytest.raises(ValidationError, match=r"point_lookup fixture=1: expected=1, actual=0"):
        validate_adapter(WrongAdapter())  # type: ignore[arg-type]
