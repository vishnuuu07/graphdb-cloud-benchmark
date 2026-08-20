"""Docker-inspect resource evidence shared by local database adapters."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime

from graphbench.models import ResourceObservation


def observe_docker_container(database: str, container_name: str) -> ResourceObservation:
    """Return Docker's observed configuration, never values copied from Compose."""
    observed_at = datetime.now().astimezone().isoformat()
    if not shutil.which("docker"):
        return ResourceObservation(database, observed_at, notes="Docker unavailable")
    result = subprocess.run(
        ["docker", "inspect", container_name], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return ResourceObservation(database, observed_at, notes="container not observable")
    try:
        inspected = json.loads(result.stdout)[0]
        host = inspected["HostConfig"]
        state = inspected.get("State", {})
        nano_cpus = int(host.get("NanoCpus") or 0)
        memory = int(host.get("Memory") or 0)
        return ResourceObservation(
            database=database,
            observed_at=observed_at,
            cpu_percent=nano_cpus / 1_000_000_000 if nano_cpus else None,
            memory_bytes=memory or None,
            image=str(inspected.get("Config", {}).get("Image") or "not observable"),
            container_status=str(state.get("Status") or "not observable"),
            notes="Docker configured limits; CPU value is cores, not a utilization percentage.",
        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ResourceObservation(database, observed_at, notes="limits not observable")
