.PHONY: setup lint test doctor dataset smoke benchmark report

setup:
	python -m pip install -e ".[dev]"

lint:
	ruff check .

test:
	python -m pytest

doctor:
	graphbench doctor

dataset:
	graphbench dataset prepare

smoke:
	graphbench smoke

benchmark:
	graphbench benchmark

report:
	graphbench report
