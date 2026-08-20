"""Validated, explicit loading for repository configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from graphbench.models import BenchmarkConfig, PlatformConfig


class ConfigurationError(ValueError):
    """Raised when user-controlled benchmark configuration is invalid."""


REQUIRED_BENCHMARK_KEYS = {
    "seed",
    "warmup_iterations",
    "measured_iterations",
    "benchmark_rounds",
    "load_batch_size",
    "concurrency_levels",
    "mixed_read_ratio",
    "mixed_write_ratio",
    "mixed_warmup_seconds",
    "mixed_measurement_seconds",
}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"Configuration file does not exist: {path}")
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"Configuration must be a mapping: {path}")
    return loaded


def load_benchmark_config(path: Path | None = None) -> BenchmarkConfig:
    source = path or repository_root() / "configs" / "benchmark.yaml"
    values = _load_yaml(source)
    missing = REQUIRED_BENCHMARK_KEYS - values.keys()
    if missing:
        raise ConfigurationError(f"Missing benchmark settings: {', '.join(sorted(missing))}")
    try:
        config = BenchmarkConfig(
            seed=int(values["seed"]),
            warmup_iterations=int(values["warmup_iterations"]),
            measured_iterations=int(values["measured_iterations"]),
            benchmark_rounds=int(values["benchmark_rounds"]),
            load_batch_size=int(values["load_batch_size"]),
            concurrency_levels=tuple(int(value) for value in values["concurrency_levels"]),
            mixed_read_ratio=float(values["mixed_read_ratio"]),
            mixed_write_ratio=float(values["mixed_write_ratio"]),
            mixed_warmup_seconds=int(values["mixed_warmup_seconds"]),
            mixed_measurement_seconds=int(values["mixed_measurement_seconds"]),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid benchmark setting in {source}: {exc}") from exc
    if config.measured_iterations < 100:
        raise ConfigurationError("measured_iterations must be at least 100")
    if not config.concurrency_levels or any(level < 1 for level in config.concurrency_levels):
        raise ConfigurationError("concurrency_levels must contain positive integers")
    if abs(config.mixed_read_ratio + config.mixed_write_ratio - 1.0) > 1e-9:
        raise ConfigurationError("mixed read/write ratios must sum to 1.0")
    return config


def load_platform_configs(path: Path | None = None) -> tuple[PlatformConfig, ...]:
    source = path or repository_root() / "configs" / "platforms.yaml"
    values = _load_yaml(source).get("platforms")
    if not isinstance(values, list) or not values:
        raise ConfigurationError("platforms.yaml must contain a non-empty platforms list")
    try:
        platforms = tuple(
            PlatformConfig(
                name=str(item["name"]),
                adapter=str(item["adapter"]),
                resource_limits=str(item["resource_limits"]),
            )
            for item in values
        )
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"Invalid platform configuration: {exc}") from exc
    if len({platform.name for platform in platforms}) != len(platforms):
        raise ConfigurationError("Platform names must be unique")
    return platforms


FINAL_PROFILE_KEYS = {
    *REQUIRED_BENCHMARK_KEYS,
    "profile",
    "target_cpu_cores",
    "target_memory_bytes",
    "target_storage_bytes",
    "expected_node_count",
    "expected_relationship_count",
    "benchmark_client_region",
}


def final_profile_path() -> Path:
    return repository_root() / "configs" / "benchmark-final.yaml"


def load_final_profile(path: Path | None = None) -> dict[str, Any]:
    """Load frozen final settings without changing the default development profile."""
    profile = _load_yaml(path or final_profile_path())
    missing = FINAL_PROFILE_KEYS - profile.keys()
    if missing:
        raise ConfigurationError(f"Missing final profile settings: {', '.join(sorted(missing))}")
    if profile["profile"] != "final":
        raise ConfigurationError("Final profile must declare profile: final")
    benchmark = load_benchmark_config(path or final_profile_path())
    if benchmark.warmup_iterations != 30 or benchmark.measured_iterations != 200:
        raise ConfigurationError(
            "Final profile must use 30 warm-ups and 200 measured read iterations"
        )
    if benchmark.benchmark_rounds != 3:
        raise ConfigurationError("Final profile must use exactly 3 benchmark rounds")
    if benchmark.concurrency_levels != (1, 5, 10, 20, 40):
        raise ConfigurationError("Final profile concurrency levels must be 1, 5, 10, 20, 40")
    if benchmark.mixed_warmup_seconds != 15 or benchmark.mixed_measurement_seconds != 60:
        raise ConfigurationError(
            "Final profile mixed durations must be 15s warm-up and 60s measured"
        )
    if int(profile["target_memory_bytes"]) != 256 * 1024 * 1024:
        raise ConfigurationError("Final profile target memory must be 256 MiB")
    if float(profile["target_cpu_cores"]) != 0.5:
        raise ConfigurationError("Final profile target CPU must be 0.5 cores")
    return profile
