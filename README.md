# graphbench

`graphbench` is a public, reproducible benchmark harness for CognoDB Cloud, Neo4j,
Memgraph, FalkorDB, and ArangoDB. It is intentionally adapter-based so every platform
receives the same graph topology, fixture values, logical workloads, warm-up treatment,
and failure accounting.

The dataset is the directed [Stanford SNAP wiki-Vote graph](https://snap.stanford.edu/data/wiki-Vote.html).
Nodes are modeled as `(:User {id: integer, bucket: integer})` and relationships as
`(:User)-[:VOTED_FOR]->(:User)`. `bucket = id % 32` is synthetic and benchmark-only: it
exists solely to provide identical filtered/indexed lookup and group-by workloads. It
never changes the source graph's topology.

## Status

This initial baseline prepares and validates the public dataset, creates deterministic
fixtures, models raw observations, calculates failure-aware latency statistics, and
defines the database adapter contract. Concrete database adapters and benchmark results
do **not** exist yet. No performance figures are included or implied.

## Quick start

```bash
make setup
graphbench doctor
graphbench dataset prepare
graphbench dataset verify
make test
```

Configuration is in `configs/benchmark.yaml`; its fixed seed creates fixture values once
under `data/fixtures/` for replay against every database. The processed files are
`data/processed/nodes.csv` and `data/processed/relationships.csv`. Metadata retains the
source URL, checksum, timestamp, counts, seed, and derived-property statement.

Future benchmark runs will publish raw measurements (including failures), resource-limit
notes and observations where available, and reports. Connection setup is excluded from
per-query timing; warm-up samples are retained but excluded from percentiles.
