"""Minimal, dependency-free dotenv loading and safe connection configuration."""

from __future__ import annotations

import os
import re
from pathlib import Path

from graphbench.config import repository_root

_URI_USERINFO = re.compile(r"(://)([^/@\s]+)@")
_SENSITIVE_ASSIGNMENT = re.compile(r"(?i)\b(password|token|secret|api[_-]?key)\s*=\s*([^\s,;]+)")


class EnvironmentConfigurationError(ValueError):
    """A required environment setting is absent; values are intentionally never included."""


def sanitize_text(value: str) -> str:
    """Remove passwords and URI userinfo before a value reaches output or an artifact."""
    redacted = _URI_USERINFO.sub(r"\1[REDACTED]@", value)
    return _SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", redacted)


def load_dotenv(root: Path | None = None) -> None:
    """Load `.env` then `.env.local`, preserving explicitly supplied process values."""
    directory = root or repository_root()
    for path in (directory / ".env", directory / ".env.local"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            if not key or not key.replace("_", "").isalnum():
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


def connection_settings(prefix: str, *, default_user: str | None = None) -> tuple[str, str, str]:
    """Return URI/user/password only in memory, never interpolated into error text."""
    load_dotenv()
    uri = os.environ.get(f"{prefix}_URI", "").strip()
    user = os.environ.get(f"{prefix}_USER", default_user or "").strip()
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    missing = [
        name for name, value in (("URI", uri), ("USER", user), ("PASSWORD", password)) if not value
    ]
    if missing:
        raise EnvironmentConfigurationError(f"{prefix} settings missing: {', '.join(missing)}")
    return uri, user, password


def memgraph_connection_settings() -> tuple[str, str, str]:
    """Memgraph's local container permits unauthenticated Bolt by default."""
    load_dotenv()
    uri = os.environ.get("MEMGRAPH_URI", "bolt://localhost:7688").strip()
    return uri, os.environ.get("MEMGRAPH_USER", "").strip(), os.environ.get("MEMGRAPH_PASSWORD", "")


def arangodb_connection_settings() -> tuple[str, str, str]:
    """Read the documented ARANGODB names while accepting the earlier ARANGO aliases."""
    load_dotenv()
    uri = (os.environ.get("ARANGODB_URL") or os.environ.get("ARANGO_URL") or "").strip()
    user = (os.environ.get("ARANGODB_USER") or os.environ.get("ARANGO_USER") or "root").strip()
    password = os.environ.get("ARANGODB_PASSWORD") or os.environ.get("ARANGO_PASSWORD") or ""
    missing = [name for name, value in (("URL", uri), ("PASSWORD", password)) if not value]
    if missing:
        raise EnvironmentConfigurationError(f"ARANGODB settings missing: {', '.join(missing)}")
    return uri, user, password
