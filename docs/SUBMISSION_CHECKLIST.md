# Submission checklist

Evidence refers to the frozen campaign `final-20260820T022802Z` and its passed integrity audit.

## Required item register

- [x] CognoDB + four competitors — PASS
- [x] public dataset >=100k relationships — PASS
- [x] exact dataset counts — PASS
- [x] identical logical workloads — PASS
- [x] resource specs documented — PASS
- [x] resource deviations documented — PASS WITH CAVEAT
- [x] node ingest throughput — PASS
- [x] relationship ingest throughput — PASS
- [x] total ingest time — PASS
- [x] point lookup p50/p95 — PASS
- [x] indexed lookup p50/p95 — PASS
- [x] 1-hop p50/p95 — PASS
- [x] 2-hop p50/p95 — PASS
- [x] 3-hop p50/p95 — PASS
- [x] aggregation p50/p95 — PASS
- [x] mixed workload QPS — PASS
- [x] concurrency documented — PASS
- [x] read/write ratio documented — PASS
- [x] measured failures retained — PASS
- [x] footprint/resource observations — PASS WITH CAVEAT
- [ ] complete raw data committed — PARTIAL: read raw measurements are committed; the high-volume
      mixed-operation JSONL is retained locally and intentionally excluded from the repository
- [x] results audit — PASS
- [x] README results matrix — PASS
- [x] charts — PASS
- [x] reproducible instructions — PASS WITH CAVEAT
- [x] methodology caveats — PASS
- [x] no secrets — PASS
- [x] LICENSE — PASS

| Requirement | Status | Evidence |
| --- | --- | --- |
| CognoDB + four competitors | PASS | README; `adapters/`; campaign manifest |
| Public dataset >=100k relationships | PASS | SNAP Wiki-Vote metadata; 103,689 relationships |
| Exact dataset counts | PASS | `data/metadata/wiki_vote.json`; ingest results |
| Identical logical workloads | PASS | `docs/QUERY_EQUIVALENCE.md`; workload manifest |
| Resource specs documented | PASS | README resources table; fairness manifest |
| Resource deviations documented | PASS WITH CAVEAT | README and `docs/FAIRNESS.md` disclose remote CognoDB, Neo4j 384 MiB, and storage limits |
| Node ingest throughput | PASS | `ingest/ingest_results.json` |
| Relationship ingest throughput | PASS | `ingest/ingest_results.json` |
| Total ingest time | PASS | `ingest/ingest_results.json` |
| Point lookup p50/p95 | PASS | `summaries/read_summary.json` |
| Indexed lookup p50/p95 | PASS | `summaries/read_summary.json`; query equivalence audit |
| 1-hop p50/p95 | PASS | `summaries/read_summary.json` |
| 2-hop p50/p95 | PASS | `summaries/read_summary.json` |
| 3-hop p50/p95 | PASS | `summaries/read_summary.json` |
| Aggregation p50/p95 | PASS | `summaries/read_summary.json` |
| Mixed workload QPS | PASS | `summaries/mixed_summary.json` |
| Concurrency documented | PASS | README; final profile |
| Read/write ratio documented | PASS | README; final profile |
| Measured failures retained | PASS | README; `mixed_errors.json`; raw mixed rows |
| Footprint/resource observations | PASS WITH CAVEAT | fairness manifest; runtime cloud resources and local quotas are not observable/enforceable |
| Raw data committed | PARTIAL | Frozen read JSONL is included; high-volume mixed-operation raw JSONL is retained locally and excluded from Git |
| Results audit | PASS | `audit/integrity_audit.md` |
| README results matrix | PASS | README |
| Charts | PASS | Five PNG files in `charts/` |
| Reproducible instructions | PASS WITH CAVEAT | README commands; a new final run requires configured database services and credentials |
| Methodology caveats | PASS | README; `docs/FAIRNESS.md` |
| No secrets | PASS | Audit-time working-tree, tracked-file, and history checks |
| LICENSE | PASS | `LICENSE` |
