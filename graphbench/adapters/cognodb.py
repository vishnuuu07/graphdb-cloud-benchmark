"""CognoDB Cloud adapter using the shared Neo4j Bolt/Cypher driver implementation."""

from __future__ import annotations

import certifi
from neo4j import TrustCustomCAs

from graphbench.adapters.cypher import AdapterError, CypherGraphAdapter
from graphbench.config import load_benchmark_config
from graphbench.environment import connection_settings


class CognoDBAdapter(CypherGraphAdapter):
    database_name = "cognodb"

    def _driver_uri(self) -> str:
        """Use explicit verified TLS because the Windows system chain is stale on this host.

        The configured endpoint remains `bolt+s://`. The Neo4j driver treats `+s` and
        custom CA configuration as mutually exclusive, so its internal URI uses `bolt://`
        together with `encrypted=True` and Certifi's verified public CA bundle.
        """
        if not self.uri.startswith("bolt+s://"):
            raise AdapterError("CognoDB URI must use bolt+s://")
        return "bolt://" + self.uri.removeprefix("bolt+s://")

    def _driver_configuration(self) -> dict[str, object]:
        return {
            "encrypted": True,
            "trusted_certificates": TrustCustomCAs(certifi.where()),
        }

    @classmethod
    def from_environment(cls) -> CognoDBAdapter:
        uri, user, password = connection_settings("COGNODB", default_user="cognodb")
        return cls(
            uri=uri,
            user=user,
            password=password,
            batch_size=load_benchmark_config().load_batch_size,
        )
