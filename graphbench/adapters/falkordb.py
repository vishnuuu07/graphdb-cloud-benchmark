"""FalkorDB adapter over its supported Redis-protocol Python client."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from graphbench.adapters.base import GraphDatabaseAdapter
from graphbench.adapters.cypher import AdapterError, batches
from graphbench.adapters.docker import observe_docker_container
from graphbench.config import load_benchmark_config
from graphbench.environment import load_dotenv, sanitize_text
from graphbench.models import LoadResult, ResourceObservation


class FalkorDBAdapter(GraphDatabaseAdapter):
    """Parameterized Cypher queries over FalkorDB's native client transport."""

    database_name = "falkordb"
    container_name = "graphbench-falkordb"
    graph_name = "graphbench"
    load_method = "parameterized FalkorDB Cypher transactional batches; schema time excluded"

    def __init__(self, *, host: str, port: int, password: str, batch_size: int) -> None:
        self.host = host
        self.port = port
        self._password = password
        self.batch_size = batch_size
        self._client: Any | None = None
        self._graph: Any | None = None

    @classmethod
    def from_environment(cls) -> FalkorDBAdapter:
        load_dotenv()
        import os

        try:
            port = int(os.environ.get("FALKORDB_PORT", "6379"))
        except ValueError as exc:
            raise AdapterError("FALKORDB_PORT must be an integer") from exc
        return cls(
            host=os.environ.get("FALKORDB_HOST", "localhost"),
            port=port,
            password=os.environ.get("FALKORDB_PASSWORD", ""),
            batch_size=load_benchmark_config().load_batch_size,
        )

    def connect(self) -> None:
        if self._graph is not None:
            return
        try:
            from falkordb import FalkorDB

            options: dict[str, Any] = {
                "host": self.host,
                "port": self.port,
                "max_connections": 40,
            }
            if self._password:
                options["password"] = self._password
            self._client = FalkorDB(**options)
            self._graph = self._client.select_graph(self.graph_name)
            self.health_check()
        except Exception as exc:
            self.close()
            raise AdapterError(f"falkordb connection failed: {sanitize_text(str(exc))}") from None

    def close(self) -> None:
        client, self._client, self._graph = self._client, None, None
        if client is not None:
            try:
                client.close()
            except AttributeError:
                pass

    def _execute(self, query: str, parameters: Mapping[str, Any] | None = None) -> list[list[Any]]:
        if self._graph is None:
            raise AdapterError("falkordb adapter is not connected")
        try:
            result = self._graph.query(query, params=dict(parameters or {}))
            return list(result.result_set)
        except Exception as exc:
            raise AdapterError(
                f"falkordb Cypher operation failed: {sanitize_text(str(exc))}"
            ) from None

    @staticmethod
    def _count(rows: list[list[Any]]) -> int:
        return int(rows[0][0])

    def health_check(self) -> bool:
        return self._count(self._execute("RETURN 1")) == 1

    def reset(self) -> None:
        # Relationship deletion is explicit because this client lacks Cypher DETACH DELETE.
        self._execute("MATCH (:User)-[r:VOTED_FOR]->(:User) DELETE r")
        self._execute("MATCH (u:User) DELETE u")

    def create_schema(self) -> None:
        existing = self._observable_index_properties()
        # FalkorDB's RDB graph restore retains index definitions but not their usable
        # contents. Prepare always runs after reset, so rebuild the two equivalent
        # schema indexes before load rather than accepting an invalid restored index.
        if existing is not None and "id" in existing:
            self._execute("DROP INDEX ON :User(id)")
        if existing is not None and "bucket" in existing:
            self._execute("DROP INDEX ON :User(bucket)")
        self._execute("CREATE INDEX FOR (u:User) ON (u.id)")
        self._execute("CREATE INDEX FOR (u:User) ON (u.bucket)")

    def _load(self, entity: str, rows: Iterable[Mapping[str, int]], query: str) -> LoadResult:
        attempted = loaded = 0
        started = time.perf_counter_ns()
        try:
            for group in batches(rows, self.batch_size):
                attempted += len(group)
                self._execute(query, {"rows": group})
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
        return self._load(
            "nodes",
            nodes,
            "UNWIND $rows AS row CREATE (:User {id: row.id, bucket: row.bucket})",
        )

    def load_relationships(self, relationships: Iterable[Mapping[str, int]]) -> LoadResult:
        return self._load(
            "relationships",
            relationships,
            "UNWIND $rows AS row "
            "MATCH (source:User {id: row.source_id}) "
            "MATCH (target:User {id: row.target_id}) "
            "CREATE (source)-[:VOTED_FOR]->(target)",
        )

    def _observable_index_properties(self) -> set[str] | None:
        try:
            rows = self._execute("CALL db.indexes()")
        except AdapterError:
            return None
        properties: set[str] = set()
        for row in rows:
            text = " ".join(str(value) for value in row)
            if "User" in text:
                for property_name in ("id", "bucket"):
                    if property_name in text:
                        properties.add(property_name)
        return properties

    def verify_counts(self) -> tuple[int, int]:
        nodes = self._count(self._execute("MATCH (u:User) RETURN count(u)"))
        relationships = self._count(
            self._execute("MATCH (:User)-[r:VOTED_FOR]->(:User) RETURN count(r)")
        )
        invalid = self._count(
            self._execute("MATCH (u:User) WHERE u.id IS NULL OR u.bucket IS NULL RETURN count(u)")
        )
        if invalid:
            raise AdapterError(f"falkordb has {invalid} User nodes missing id or bucket")
        observed = self._observable_index_properties()
        if observed is not None and {"id", "bucket"} - observed:
            raise AdapterError(
                "falkordb observable index check is missing required User properties"
            )
        return nodes, relationships

    def point_lookup(self, user_id: int) -> int:
        return self._count(
            self._execute("MATCH (u:User {id: $id}) RETURN count(u)", {"id": user_id})
        )

    def filtered_lookup(self, bucket: int) -> int:
        return self._count(
            self._execute("MATCH (u:User {bucket: $bucket}) RETURN count(u)", {"bucket": bucket})
        )

    def _hop(self, user_id: int, depth: int) -> int:
        query = "MATCH (start:User {id: $id})-[r1:VOTED_FOR]->(hop1)"
        if depth == 1:
            return self._count(self._execute(f"{query} RETURN count(*)", {"id": user_id}))
        # FalkorDB does not apply Cypher's relationship uniqueness to separated MATCH
        # clauses, so retain relationship variables and state the canonical rule.
        query += " WITH r1, hop1 MATCH (hop1)-[r2:VOTED_FOR]->(hop2) WHERE ID(r2) <> ID(r1)"
        if depth == 2:
            return self._count(self._execute(f"{query} RETURN count(*)", {"id": user_id}))
        query += (
            " WITH r1, r2, hop2 MATCH (hop2)-[r3:VOTED_FOR]->(:User) "
            "WHERE ID(r3) <> ID(r1) AND ID(r3) <> ID(r2) RETURN count(*)"
        )
        return self._count(self._execute(query, {"id": user_id}))

    def one_hop(self, user_id: int) -> int:
        return self._hop(user_id, 1)

    def two_hop(self, user_id: int) -> int:
        return self._hop(user_id, 2)

    def three_hop(self, user_id: int) -> int:
        return self._hop(user_id, 3)

    def aggregation(self) -> Mapping[int, int]:
        rows = self._execute("MATCH (u:User) RETURN u.bucket, count(u) ORDER BY u.bucket")
        return {int(row[0]): int(row[1]) for row in rows}

    def mixed_read(self, user_id: int) -> int:
        return self.point_lookup(user_id)

    def mixed_write(self, properties: Mapping[str, Any]) -> int:
        return self._count(
            self._execute(
                "MATCH (u:User {id: $id}) "
                "SET u.benchmark_counter = coalesce(u.benchmark_counter, 0) + 1 RETURN count(u)",
                {"id": int(properties["id"])},
            )
        )

    def reset_write_state(self) -> None:
        self._execute("MATCH (u:User) REMOVE u.benchmark_counter")

    def observe_resources(self) -> ResourceObservation | None:
        return observe_docker_container(self.database_name, self.container_name)

    def platform_metadata(self) -> Mapping[str, str]:
        if self._graph is None:
            return {"server": "not connected", "indexed_properties": "not observable"}
        indexes = self._observable_index_properties()
        return {
            "server": "FalkorDB via supported Python client",
            "indexed_properties": ",".join(sorted(indexes or ())) or "not observable",
            "resource_limits": "see Docker inspect evidence",
        }
