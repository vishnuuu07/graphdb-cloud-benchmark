"""Writing results belongs here once adapters produce real measurements."""

from __future__ import annotations

from pathlib import Path


def report_not_available(results_root: Path) -> str:
    return (
        "No report was generated: concrete database adapters and measured raw results are not "
        f"implemented yet (expected results directory: {results_root})."
    )
