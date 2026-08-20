.PHONY: setup lint test doctor dataset smoke prepare validate benchmark report benchmark-final benchmark-dry-run environment-capture fairness-freeze neo4j-up neo4j-down neo4j-reset memgraph-up memgraph-down memgraph-reset falkordb-up falkordb-down falkordb-reset arangodb-up arangodb-down arangodb-reset

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
	graphbench smoke --database neo4j

prepare:
	graphbench prepare --database neo4j

validate:
	graphbench validate --database neo4j

benchmark:
	graphbench benchmark

report:
	graphbench report --campaign-dir results/final/final-20260820T022802Z --output-dir charts

environment-capture:
	python -m graphbench environment capture

fairness-freeze:
	python -m graphbench fairness freeze

# Canonical final command. It refuses to collect results until a separate release decision.
benchmark-final:
	python -m graphbench benchmark --all --profile final

benchmark-dry-run:
	python -m graphbench benchmark --all --profile final --dry-run

neo4j-up:
	docker compose -f docker/neo4j-compose.yaml up -d

neo4j-down:
	docker compose -f docker/neo4j-compose.yaml down

neo4j-reset:
	docker compose -f docker/neo4j-compose.yaml down -v

memgraph-up:
	docker compose -f docker/memgraph-compose.yaml up -d

memgraph-down:
	docker compose -f docker/memgraph-compose.yaml down

memgraph-reset:
	docker compose -f docker/memgraph-compose.yaml down -v

falkordb-up:
	docker compose -f docker/falkordb-compose.yaml up -d

falkordb-down:
	docker compose -f docker/falkordb-compose.yaml down

falkordb-reset:
	docker compose -f docker/falkordb-compose.yaml down -v

arangodb-up:
	docker compose -f docker/arangodb-compose.yaml up -d

arangodb-down:
	docker compose -f docker/arangodb-compose.yaml down

arangodb-reset:
	docker compose -f docker/arangodb-compose.yaml down -v
