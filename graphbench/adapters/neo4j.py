"""Locally deployed Neo4j adapter using the shared Bolt/Cypher workload implementation."""

from __future__ import annotations

from graphbench.adapters.cypher import CypherGraphAdapter
from graphbench.adapters.docker import observe_docker_container
from graphbench.config import load_benchmark_config
from graphbench.environment import connection_settings
from graphbench.models import ResourceObservation


class Neo4jAdapter(CypherGraphAdapter):
    database_name = "neo4j"
    container_name = "graphbench-neo4j"

    @classmethod
    def from_environment(cls) -> Neo4jAdapter:
        uri, user, password = connection_settings("NEO4J", default_user="neo4j")
        return cls(
            uri=uri,
            user=user,
            password=password,
            batch_size=load_benchmark_config().load_batch_size,
        )

    def observe_resources(self) -> ResourceObservation | None:
        return observe_docker_container(self.database_name, self.container_name)
