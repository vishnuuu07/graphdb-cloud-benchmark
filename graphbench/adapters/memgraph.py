"""Memgraph adapter using its Bolt-compatible Cypher protocol."""

from __future__ import annotations

from graphbench.adapters.cypher import CypherGraphAdapter
from graphbench.adapters.docker import observe_docker_container
from graphbench.config import load_benchmark_config
from graphbench.environment import memgraph_connection_settings
from graphbench.models import ResourceObservation


class MemgraphAdapter(CypherGraphAdapter):
    """Keep the canonical Cypher workloads while using Memgraph DDL syntax."""

    database_name = "memgraph"
    container_name = "graphbench-memgraph"

    @classmethod
    def from_environment(cls) -> MemgraphAdapter:
        uri, user, password = memgraph_connection_settings()
        return cls(
            uri=uri,
            user=user,
            password=password,
            batch_size=load_benchmark_config().load_batch_size,
        )

    def create_schema(self) -> None:
        # Memgraph's label/property indexes are equivalent logical lookup indexes.
        self._write("CREATE INDEX ON :User(id)")
        self._write("CREATE INDEX ON :User(bucket)")

    def _observable_index_properties(self) -> set[str] | None:
        try:
            records = self._execute("SHOW INDEX INFO")
        except Exception:
            return None
        properties: set[str] = set()
        for record in records:
            label = record.get("label") or record.get("Label")
            property_name = record.get("property") or record.get("Property")
            if label != "User" or not property_name:
                continue
            if isinstance(property_name, list | tuple):
                properties.update(str(value) for value in property_name)
            else:
                properties.add(str(property_name))
        return properties

    def observe_resources(self) -> ResourceObservation | None:
        return observe_docker_container(self.database_name, self.container_name)
