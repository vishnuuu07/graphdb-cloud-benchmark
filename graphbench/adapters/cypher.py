"""Shared, parameterized Cypher implementation for all supported Bolt platforms."""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from neo4j import GraphDatabase

from graphbench.adapters.base import GraphDatabaseAdapter
from graphbench.environment import sanitize_text
from graphbench.models import LoadResult, ResourceObservation


class AdapterError(RuntimeError):
    """Driver failure with secrets stripped before it can reach the CLI or result files."""


def batches(records: Iterable[Mapping[str, int]], size: int) -> Iterator[list[Mapping[str, int]]]:
    """Yield non-empty fixed-size batches without materializing an input iterator."""
    if size < 1:
        raise ValueError("batch size must be positive")
    iterator = iter(records)
    while group := list(itertools.islice(iterator, size)):
        yield group


class CypherGraphAdapter(GraphDatabaseAdapter):
    """One pooled driver and identical logical Cypher for compatible graph services."""

    database_name = "cypher"
    load_method = "parameterized UNWIND transactional driver batches; schema time excluded"
    _driver: Any | None
    operation_timeout_seconds = 120

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        batch_size: int,
        max_connection_pool_size: int = 40,
    ) -> None:
        self.uri = uri
        self.user = user
        self._password = password
        self.batch_size = batch_size
        self.max_connection_pool_size = max_connection_pool_size
        self._driver = None

    def connect(self) -> None:
        if self._driver is not None:
            return
        try:
            self._driver = GraphDatabase.driver(
                self._driver_uri(),
                auth=(self.user, self._password) if self.user else None,
                max_connection_pool_size=self.max_connection_pool_size,
                connection_timeout=15,
                **self._driver_configuration(),
            )
            self._driver.verify_connectivity()
        except Exception as exc:
            self.close()
            raise AdapterError(
                f"{self.database_name} connection failed: {sanitize_text(str(exc))}"
            ) from None

    def _driver_uri(self) -> str:
        """Return the URI passed to the driver; subclasses may safely select TLS configuration."""
        return self.uri

    def _driver_configuration(self) -> dict[str, Any]:
        """Return driver options that do not affect the shared logical workloads."""
        return {}

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def _session(self) -> Any:
        if self._driver is None:
            raise AdapterError(f"{self.database_name} adapter is not connected")
        return self._driver.session()

    def _execute(
        self, query: str, parameters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        try:
            with self._session() as session:
                return [
                    record.data()
                    for record in session.run(
                        query,
                        parameters or {},
                        timeout=self.operation_timeout_seconds,
                    )
                ]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                f"{self.database_name} Cypher operation failed: {sanitize_text(str(exc))}"
            ) from None

    def _write(
        self, query: str, parameters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._execute(query, parameters)

    def health_check(self) -> bool:
        return self._execute("RETURN 1 AS ok")[0]["ok"] == 1

    def reset(self) -> None:
        # Scoped to this project's exact label and relationship type; no database-wide delete.
        self._write("MATCH (u:User) DETACH DELETE u")

    def create_schema(self) -> None:
        # Same logical unique id constraint and bucket index on every Cypher service.
        self._write(
            "CREATE CONSTRAINT graphbench_user_id_unique IF NOT EXISTS "
            "FOR (u:User) REQUIRE u.id IS UNIQUE"
        )
        self._write(
            "CREATE INDEX graphbench_user_bucket_index IF NOT EXISTS FOR (u:User) ON (u.bucket)"
        )
        self._wait_for_indexes()

    def _wait_for_indexes(self) -> None:
        # Neo4j-compatible servers expose this procedure. An unsupported observation is not guessed.
        try:
            self._execute("CALL db.awaitIndexes(300)")
        except AdapterError:
            # Creation remains authoritative; platform metadata records observability separately.
            return

    def load_nodes(self, nodes: Iterable[Mapping[str, int]]) -> LoadResult:
        attempted = loaded = 0
        started = time.perf_counter_ns()
        try:
            for group in batches(nodes, self.batch_size):
                attempted += len(group)
                self._write(
                    "UNWIND $rows AS row MERGE (u:User {id: row.id}) SET u.bucket = row.bucket",
                    {"rows": group},
                )
                loaded += len(group)
        except Exception as exc:
            return LoadResult(
                self.database_name,
                "nodes",
                attempted,
                loaded,
                (time.perf_counter_ns() - started) / 1_000_000,
                (sanitize_text(str(exc)),),
            )
        return LoadResult(
            self.database_name,
            "nodes",
            attempted,
            loaded,
            (time.perf_counter_ns() - started) / 1_000_000,
        )

    def load_relationships(self, relationships: Iterable[Mapping[str, int]]) -> LoadResult:
        attempted = loaded = 0
        started = time.perf_counter_ns()
        try:
            for group in batches(relationships, self.batch_size):
                attempted += len(group)
                self._write(
                    "UNWIND $rows AS row "
                    "MATCH (source:User {id: row.source_id}) "
                    "MATCH (target:User {id: row.target_id}) "
                    "CREATE (source)-[:VOTED_FOR]->(target)",
                    {"rows": group},
                )
                loaded += len(group)
        except Exception as exc:
            return LoadResult(
                self.database_name,
                "relationships",
                attempted,
                loaded,
                (time.perf_counter_ns() - started) / 1_000_000,
                (sanitize_text(str(exc)),),
            )
        return LoadResult(
            self.database_name,
            "relationships",
            attempted,
            loaded,
            (time.perf_counter_ns() - started) / 1_000_000,
        )

    def verify_counts(self) -> tuple[int, int]:
        nodes = int(self._execute("MATCH (u:User) RETURN count(u) AS count")[0]["count"])
        relationships = int(
            self._execute("MATCH (:User)-[r:VOTED_FOR]->(:User) RETURN count(r) AS count")[0][
                "count"
            ]
        )
        invalid_nodes = int(
            self._execute(
                "MATCH (u:User) WHERE u.id IS NULL OR u.bucket IS NULL RETURN count(u) AS count"
            )[0]["count"]
        )
        if invalid_nodes:
            raise AdapterError(
                f"{self.database_name} has {invalid_nodes} User nodes missing id or bucket"
            )
        self._verify_indexes()
        return nodes, relationships

    def _verify_indexes(self) -> None:
        """Fail if a server exposes index inspection and either required index is absent."""
        indexed_properties = self._observable_index_properties()
        if indexed_properties is None:
            return
        missing = {"id", "bucket"} - indexed_properties
        if missing:
            raise AdapterError(
                f"{self.database_name} observable index check is missing: "
                f"{', '.join(sorted(missing))}"
            )

    def _observable_index_properties(self) -> set[str] | None:
        """Read normal-index and unique-constraint properties across compatible SHOW formats."""
        try:
            index_records = self._execute("SHOW INDEXES YIELD *")
        except AdapterError:
            return None
        records = index_records
        try:
            records += self._execute("SHOW CONSTRAINTS YIELD *")
        except AdapterError:
            pass
        properties: set[str] = set()
        for record in records:
            labels = record.get("labelsOrTypes", record.get("label", ()))
            if isinstance(labels, str):
                labels = (labels,)
            if "User" not in (labels or ()) or record.get("state") not in {"ONLINE", None}:
                continue
            names = record.get("properties", ())
            if isinstance(names, str):
                names = (names,)
            properties.update(str(name) for name in (names or ()))
        return properties

    @staticmethod
    def _count(records: list[dict[str, Any]]) -> int:
        return int(records[0]["count"])

    def point_lookup(self, user_id: int) -> int:
        return self._count(
            self._execute("MATCH (u:User {id: $id}) RETURN count(u) AS count", {"id": user_id})
        )

    def filtered_lookup(self, bucket: int) -> int:
        return self._count(
            self._execute(
                "MATCH (u:User {bucket: $bucket}) RETURN count(u) AS count", {"bucket": bucket}
            )
        )

    def _hop(self, user_id: int, depth: int) -> int:
        pattern = "-[:VOTED_FOR]->()" * (depth - 1) + "-[:VOTED_FOR]->(:User)"
        return self._count(
            self._execute(
                f"MATCH (:User {{id: $id}}){pattern} RETURN count(*) AS count", {"id": user_id}
            )
        )

    def one_hop(self, user_id: int) -> int:
        return self._hop(user_id, 1)

    def two_hop(self, user_id: int) -> int:
        return self._hop(user_id, 2)

    def three_hop(self, user_id: int) -> int:
        return self._hop(user_id, 3)

    def aggregation(self) -> Mapping[int, int]:
        records = self._execute(
            "MATCH (u:User) RETURN u.bucket AS bucket, count(u) AS count ORDER BY bucket"
        )
        return {int(record["bucket"]): int(record["count"]) for record in records}

    def mixed_read(self, user_id: int) -> int:
        return self.point_lookup(user_id)

    def mixed_write(self, properties: Mapping[str, Any]) -> int:
        user_id = int(properties["id"])
        return self._count(
            self._write(
                "MATCH (u:User {id: $id}) "
                "SET u.benchmark_counter = coalesce(u.benchmark_counter, 0) + 1 "
                "RETURN count(u) AS count",
                {"id": user_id},
            )
        )

    def reset_write_state(self) -> None:
        self._write("MATCH (u:User) REMOVE u.benchmark_counter")

    def observe_resources(self) -> ResourceObservation | None:
        return None

    def platform_metadata(self) -> Mapping[str, str]:
        if self._driver is None:
            return {"server": "not connected"}
        try:
            info = self._driver.get_server_info()
            try:
                index_properties = self._observable_index_properties()
                indexes = ",".join(sorted(index_properties or ())) or "not observable"
            except AdapterError:
                indexes = "not observable"
            return {
                "server_agent": str(info.agent) if info.agent else "not observable",
                "protocol_version": str(info.protocol_version)
                if info.protocol_version
                else "not observable",
                "indexed_properties": indexes,
                "resource_limits": "not observable",
            }
        except Exception:
            return {"server": "not observable", "resource_limits": "not observable"}
