from pathlib import Path

import pytest

from graphbench.config import ConfigurationError, load_benchmark_config, load_platform_configs


def test_loads_repository_benchmark_configuration() -> None:
    config = load_benchmark_config()
    assert config.measured_iterations >= 100
    assert config.mixed_read_ratio + config.mixed_write_ratio == 1.0


def test_rejects_missing_configuration(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.yaml"
    path.write_text("seed: 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Missing benchmark settings"):
        load_benchmark_config(path)


def test_loads_all_comparison_platforms() -> None:
    assert [platform.name for platform in load_platform_configs()] == [
        "cognodb_cloud",
        "neo4j",
        "memgraph",
        "falkordb",
        "arangodb",
    ]
