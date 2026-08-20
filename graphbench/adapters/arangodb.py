"""ArangoDB adapter using native collections and parameterized AQL."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from graphbench.adapters.base import GraphDatabaseAdapter
from graphbench.adapters.cypher import AdapterError, batches
from graphbench.adapters.docker import observe_docker_container
from graphbench.config import load_benchmark_config
from graphbench.environment import arangodb_connection_settings, sanitize_text
from graphbench.models import LoadResult, ResourceObservation


class ArangoDBAdapter(GraphDatabaseAdapter):
    """Map canonical User identity to a normal `id` property, never to `_key`."""

    database_name = "arangodb"
    container_name = "graphbench-arangodb"
    database = "graphbench"
    graph_name = "graphbench_graph"
    users = "users"
    edges = "voted_for"
    load_method = "parameterized python-arango insert_many batches; schema time excluded"

    def __init__(self, *, url: str, user: str, password: str, batch_size: int) -> None:
        self.url = url
        self.user = user
        self._password = password
        self.batch_size = batch_size
        self._client: Any | None = None
        self._system: Any | None = None
        self._db: Any | None = None

    @classmethod
    def from_environment(cls) -> ArangoDBAdapter:
        url, user, password = arangodb_connection_settings()
        return cls(
            url=url,
            user=user,
            password=password,
            batch_size=load_benchmark_config().load_batch_size,
        )

    def connect(self) -> None:
        if self._system is not None:
            return
        try:
            from arango import ArangoClient
            from arango.http import DefaultHTTPClient

            self._client = ArangoClient(
                hosts=self.url,
                http_client=DefaultHTTPClient(pool_connections=40, pool_maxsize=40),
            )
            self._system = self._client.db("_system", username=self.user, password=self._password)
            self._system.version()
            # `smoke` includes a harmless logical lookup, so provision the isolated
            # benchmark database before selecting it. No graph data is created here.
            if not self._system.has_database(self.database):
                self._system.create_database(self.database)
            self._db = self._client.db(self.database, username=self.user, password=self._password)
        except Exception as exc:
            self.close()
            raise AdapterError(f"arangodb connection failed: {sanitize_text(str(exc))}") from None

    def close(self) -> None:
        client, self._client, self._system, self._db = self._client, None, None, None
        if client is not None:
            client.close()

    def _database(self) -> Any:
        if self._db is None:
            raise AdapterError("arangodb adapter is not connected")
        return self._db

    def _aql(self, query: str, bind_vars: Mapping[str, Any] | None = None) -> list[Any]:
        try:
            return list(self._database().aql.execute(query, bind_vars=dict(bind_vars or {})))
        except Exception as exc:
            raise AdapterError(
                f"arangodb AQL operation failed: {sanitize_text(str(exc))}"
            ) from None

    @staticmethod
    def _count(values: list[Any]) -> int:
        return int(values[0])

    def health_check(self) -> bool:
        if self._system is None:
            raise AdapterError("arangodb adapter is not connected")
        return bool(self._system.version())

    def reset(self) -> None:
        if self._system is None:
            raise AdapterError("arangodb adapter is not connected")
        if not self._system.has_database(self.database):
            return
        db = self._database()
        if db.has_collection(self.edges):
            db.collection(self.edges).truncate()
        if db.has_collection(self.users):
            db.collection(self.users).truncate()

    def create_schema(self) -> None:
        if self._system is None:
            raise AdapterError("arangodb adapter is not connected")
        if not self._system.has_database(self.database):
            self._system.create_database(self.database)
            self._db = self._client.db(self.database, username=self.user, password=self._password)
        db = self._database()
        if not db.has_graph(self.graph_name):
            db.create_graph(
                self.graph_name,
                edge_definitions=[
                    {
                        "edge_collection": self.edges,
                        "from_vertex_collections": [self.users],
                        "to_vertex_collections": [self.users],
                    }
                ],
            )
        users = db.collection(self.users)
        existing = {index.get("name") for index in users.indexes()}
        if "graphbench_user_id" not in existing:
            users.add_persistent_index(["id"], unique=True, name="graphbench_user_id")
        if "graphbench_user_bucket" not in existing:
            users.add_persistent_index(["bucket"], name="graphbench_user_bucket")

    def _load_documents(
        self, entity: str, rows: Iterable[Mapping[str, int]], collection: str, transform: Any
    ) -> LoadResult:
        attempted = loaded = 0
        started = time.perf_counter_ns()
        try:
            target = self._database().collection(collection)
            for group in batches(rows, self.batch_size):
                attempted += len(group)
                result = target.insert_many([transform(row) for row in group])
                failures = [
                    item for item in result if isinstance(item, Mapping) and item.get("error")
                ]
                if failures:
                    raise AdapterError(f"{len(failures)} {entity} documents rejected")
                loaded += len(group)
        except Exception as exc:
            return LoadResult(
                self.database_name,
                entity,
                attempted,
                loaded,
                (time.perf_counter_ns() - started) / 1_000_000,
                (sanitize_text(str(exc)),),
            )
        return LoadResult(
            self.database_name,
            entity,
            attempted,
            loaded,
            (time.perf_counter_ns() - started) / 1_000_000,
        )

    def load_nodes(self, nodes: Iterable[Mapping[str, int]]) -> LoadResult:
        return self._load_documents(
            "nodes",
            nodes,
            self.users,
            lambda row: {"_key": str(row["id"]), "id": row["id"], "bucket": row["bucket"]},
        )

    def load_relationships(self, relationships: Iterable[Mapping[str, int]]) -> LoadResult:
        return self._load_documents(
            "relationships",
            relationships,
            self.edges,
            lambda row: {
                "_from": f"{self.users}/{row['source_id']}",
                "_to": f"{self.users}/{row['target_id']}",
            },
        )

    def _observable_index_properties(self) -> set[str] | None:
        try:
            indexes = self._database().collection(self.users).indexes()
        except Exception:
            return None
        properties: set[str] = set()
        for index in indexes:
            if index.get("type") == "persistent":
                properties.update(str(value) for value in index.get("fields", ()))
        return properties

    def verify_counts(self) -> tuple[int, int]:
        nodes = self._count(self._aql("RETURN LENGTH(users)"))
        relationships = self._count(self._aql("RETURN LENGTH(voted_for)"))
        invalid = self._count(
            self._aql(
                "RETURN LENGTH(FOR u IN users FILTER u.id == null OR u.bucket == null RETURN 1)"
            )
        )
        if invalid:
            raise AdapterError(f"arangodb has {invalid} users missing canonical id or bucket")
        observed = self._observable_index_properties()
        if observed is None or {"id", "bucket"} - observed:
            raise AdapterError(
                "arangodb observable index check is missing required users properties"
            )
        return nodes, relationships

    def point_lookup(self, user_id: int) -> int:
        return self._count(
            self._aql("RETURN LENGTH(FOR u IN users FILTER u.id == @id RETURN 1)", {"id": user_id})
        )

    def filtered_lookup(self, bucket: int) -> int:
        return self._count(
            self._aql(
                "RETURN LENGTH(FOR u IN users FILTER u.bucket == @bucket RETURN 1)",
                {"bucket": bucket},
            )
        )

    def _hop(self, user_id: int, depth: int) -> int:
        # Cypher fixed patterns permit repeat vertices but not the same relationship twice.
        return self._count(
            self._aql(
                "RETURN LENGTH("
                "FOR start IN users FILTER start.id == @id "
                f"FOR vertex, edge, path IN {depth}..{depth} OUTBOUND start voted_for "
                'OPTIONS { uniqueVertices: "none", uniqueEdges: "path" } RETURN 1)',
                {"id": user_id},
            )
        )

    def one_hop(self, user_id: int) -> int:
        return self._hop(user_id, 1)

    def two_hop(self, user_id: int) -> int:
        return self._hop(user_id, 2)

    def three_hop(self, user_id: int) -> int:
        return self._hop(user_id, 3)

    def aggregation(self) -> Mapping[int, int]:
        rows = self._aql(
            "FOR u IN users COLLECT bucket = u.bucket WITH COUNT INTO count SORT bucket "
            "RETURN {bucket: bucket, count: count}"
        )
        return {int(row["bucket"]): int(row["count"]) for row in rows}

    def mixed_read(self, user_id: int) -> int:
        return self.point_lookup(user_id)

    def mixed_write(self, properties: Mapping[str, Any]) -> int:
        return self._count(
            self._aql(
                "RETURN LENGTH(FOR u IN users FILTER u.id == @id "
                "UPDATE u WITH {benchmark_counter: NOT_NULL(u.benchmark_counter, 0) + 1} "
                "IN users RETURN 1)",
                {"id": int(properties["id"])},
            )
        )

    def reset_write_state(self) -> None:
        self._aql(
            "FOR u IN users FILTER HAS(u, 'benchmark_counter') "
            "UPDATE u WITH {benchmark_counter: null} IN users OPTIONS {keepNull: false}"
        )

    def observe_resources(self) -> ResourceObservation | None:
        return observe_docker_container(self.database_name, self.container_name)

    def platform_metadata(self) -> Mapping[str, str]:
        if self._system is None:
            return {"server": "not connected", "indexed_properties": "not observable"}
        try:
            response = self._system.version()
            version = (
                response.get("version", "not observable")
                if isinstance(response, Mapping)
                else response
            )
        except Exception:
            version = "not observable"
        indexes = self._observable_index_properties()
        return {
            "server_version": str(version),
            "indexed_properties": ",".join(sorted(indexes or ())) or "not observable",
            "resource_limits": "see Docker inspect evidence",
        }
