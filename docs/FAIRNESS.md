# Fairness and reproducibility methodology

This document describes the frozen public campaign `final-20260820T022802Z`. It records what
was held constant, what could not be held constant, and which observations remain diagnostic
only. The root [README](../README.md) contains the result tables; this page is the methodology
record.

## Resource parity

The frozen final profile targets 0.5 CPU and 256 MiB RAM for every database. Local
limits are accepted only from `docker inspect`, not Compose source text. CognoDB's
0.5 burstable vCPU, 256 MiB RAM, and 1 GiB disk are recorded as advertised values from
the Wexa assignment specification, not as locally observed measurements.

`results/final/final-20260820T022802Z/metadata/fairness_manifest.json` records configured, observed, and unavailable
values separately. A local platform whose observed CPU or memory differs from the
frozen profile fails final benchmark preflight. Neo4j is the documented exception: it ran at
0.5 CPU and 384 MiB RAM. It could boot at 256 MiB but could not reliably ingest the complete
canonical dataset in bounded low-memory testing; this is a resource-parity deviation, not
strict parity.

## Dataset, fixture, and query parity

Every platform receives the same processed SNAP Wiki-Vote graph: 7,115 Users and
103,689 directed `VOTED_FOR` relationships. The source checksum, deterministic seed,
and fixture hashes are included in the configuration fingerprint. Fixtures are made
once and replayed in the same order across platforms.

The equivalent `id` and `bucket` indexes are verified before a run. Query semantics,
including exact outgoing traversal depth and relationship-path multiplicity, are
audited in [QUERY_EQUIVALENCE.md](QUERY_EQUIVALENCE.md). The machine-readable workload
contract is `results/metadata/workload_manifest.json`.

## Client and timing boundary

All runs originate from one captured benchmark-client environment. Per-operation
latency is client-observed monotonic wall time from immediately before the driver/API
call until its bounded result is consumed. It excludes fixture selection, connection
setup, startup, logging, and result serialization. Load timing separately records node
load, relationship load, and total driver-load wall time; dataset download, preprocessing,
container startup, and schema setup are excluded.

## Network and managed-service caveat

CognoDB is remote over TLS/Bolt; Neo4j, Memgraph, FalkorDB, and ArangoDB are local Docker
containers reached via loopback. Local systems therefore have a network-latency advantage.
Raw latency must not be presented as isolated database-engine latency or controlled
hardware/network comparison.

`results/metadata/transport_baseline.json` is a diagnostic-only cheapest-request measurement with warm-up and
100 measured requests. It is never subtracted from query latency, never used to normalize
results, and is not a benchmark workload. CognoDB's recorded diagnostic p50 is 246.776 ms
(about 247 ms). No artificial delay, proxy, `tc`, or netem is
introduced to imitate the remote path.

## Storage treatment

Docker Desktop could not reliably enforce 1 GiB per-volume quotas in this environment, so no
local database is claimed to have a 1 GiB storage allocation. The fairness manifest
records an observed volume footprint after load where available; footprint is usage, not
an allocated limit. CognoDB's 1 GiB value remains an advertised assignment-tier limit.
FalkorDB uses the official entrypoint's mounted RDB data path. Its RDB restore retains
graph data but requires the normal `prepare` schema/index rebuild before indexed query
validation; this is recorded in the fairness manifest rather than hidden.

## What this harness will not do

- It will not subtract network latency or normalize measurements to make platforms alike.
- It will not discard failures, throttling, or invalid-state evidence.
- It will not alter graph topology, fixtures, workload semantics, or indexes per platform.
- It will not use database-specific materialized caches or privileged loading paths merely
  to improve one platform's result.
- It will not execute a final benchmark when preflight or the frozen fingerprint fails.
