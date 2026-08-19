"""Small practical interface shared by all graph database implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from graphbench.models import LoadResult, ResourceObservation


class GraphDatabaseAdapter(ABC):
    """A concrete adapter must parameterize its queries and retain operation errors."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def create_schema(self) -> None: ...

    @abstractmethod
    def load_nodes(self, nodes: Iterable[Mapping[str, int]]) -> LoadResult: ...

    @abstractmethod
    def load_relationships(self, relationships: Iterable[Mapping[str, int]]) -> LoadResult: ...

    @abstractmethod
    def verify_counts(self) -> tuple[int, int]: ...

    @abstractmethod
    def point_lookup(self, user_id: int) -> int: ...

    @abstractmethod
    def filtered_lookup(self, bucket: int) -> int: ...

    @abstractmethod
    def one_hop(self, user_id: int) -> int: ...

    @abstractmethod
    def two_hop(self, user_id: int) -> int: ...

    @abstractmethod
    def three_hop(self, user_id: int) -> int: ...

    @abstractmethod
    def aggregation(self, bucket: int) -> int: ...

    @abstractmethod
    def mixed_read(self, user_id: int) -> int: ...

    @abstractmethod
    def mixed_write(self, properties: Mapping[str, Any]) -> int: ...

    @abstractmethod
    def observe_resources(self) -> ResourceObservation | None: ...

    @abstractmethod
    def platform_metadata(self) -> Mapping[str, str]: ...
