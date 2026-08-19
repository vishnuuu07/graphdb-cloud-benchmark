"""Database-independent values written by benchmark runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PlatformConfig:
    name: str
    adapter: str
    resource_limits: str


@dataclass(frozen=True)
class DatasetMetadata:
    source_url: str
    source_name: str
    download_timestamp: str
    source_checksum: str
    node_count: int
    relationship_count: int
    random_seed: int
    derived_property_description: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkConfig:
    seed: int
    warmup_iterations: int
    measured_iterations: int
    benchmark_rounds: int
    load_batch_size: int
    concurrency_levels: tuple[int, ...]
    mixed_read_ratio: float
    mixed_write_ratio: float
    mixed_warmup_seconds: int
    mixed_measurement_seconds: int


@dataclass(frozen=True)
class LoadResult:
    database: str
    entity: str
    attempted: int
    loaded: int
    duration_ms: float
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawLatencySample:
    database: str
    workload: str
    round: int
    iteration: int
    fixture_id: str
    duration_ms: float | None
    success: bool
    result_count: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    warmup: bool = False


@dataclass(frozen=True)
class WorkloadResult:
    database: str
    workload: str
    round: int
    statistics: dict[str, float | int | None]


@dataclass(frozen=True)
class BenchmarkRound:
    database: str
    round: int
    started_at: str
    workloads: tuple[WorkloadResult, ...]


@dataclass(frozen=True)
class ResourceObservation:
    database: str
    observed_at: str
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ErrorRecord:
    database: str
    workload: str
    timestamp: str
    error_type: str
    message: str
    fixture_id: str | None = None


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    started_at: str
    benchmark_config: BenchmarkConfig
    dataset: DatasetMetadata
    platforms: tuple[PlatformConfig, ...]
    notes: str | None = None

    @classmethod
    def started(
        cls,
        run_id: str,
        benchmark_config: BenchmarkConfig,
        dataset: DatasetMetadata,
        platforms: tuple[PlatformConfig, ...],
    ) -> RunMetadata:
        return cls(
            run_id, datetime.now().astimezone().isoformat(), benchmark_config, dataset, platforms
        )
