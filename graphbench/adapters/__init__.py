"""Database adapters registered by their stable CLI names."""

from graphbench.adapters.arangodb import ArangoDBAdapter
from graphbench.adapters.base import GraphDatabaseAdapter
from graphbench.adapters.cognodb import CognoDBAdapter
from graphbench.adapters.falkordb import FalkorDBAdapter
from graphbench.adapters.memgraph import MemgraphAdapter
from graphbench.adapters.neo4j import Neo4jAdapter


def create_adapter(name: str) -> GraphDatabaseAdapter:
    """Build a supported adapter from environment configuration, without connecting."""
    normalized = name.lower()
    if normalized in {"cognodb", "cognodb_cloud"}:
        return CognoDBAdapter.from_environment()
    if normalized == "neo4j":
        return Neo4jAdapter.from_environment()
    if normalized == "memgraph":
        return MemgraphAdapter.from_environment()
    if normalized == "falkordb":
        return FalkorDBAdapter.from_environment()
    if normalized == "arangodb":
        return ArangoDBAdapter.from_environment()
    raise ValueError(f"Unsupported database adapter: {name}")


__all__ = [
    "ArangoDBAdapter",
    "CognoDBAdapter",
    "FalkorDBAdapter",
    "GraphDatabaseAdapter",
    "MemgraphAdapter",
    "Neo4jAdapter",
    "create_adapter",
]
