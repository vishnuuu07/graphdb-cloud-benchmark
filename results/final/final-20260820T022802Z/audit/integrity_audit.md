# Final campaign integrity audit

- Campaign: `final-20260820T022802Z`
- Fingerprint: `bf5c21d0bce4043d0c453d41497d7c375e685b28ee254714521e4a9c2879b162`
- Publication status: **PASSED — RESULTS FROZEN**
- Read measured / warm-ups: 18000 / 900
- Mixed measured operations: 1559631
- Critical issues: none

## Limitations retained

- CognoDB is a managed remote TLS/Bolt service; raw latency is end-to-end WAN/TLS client-observed latency, not engine-isolated latency.
- Neo4j was measured at 0.5 CPU and 384 MiB RAM. This is a documented resource-parity deviation from the 256 MiB target.
- Docker Desktop volume quotas were unavailable; observed storage footprint is not an enforced 1 GiB allocation.
- The benchmark client and local Docker databases share a Windows host; background activity, Docker Desktop/WSL, and scheduling may contribute noise.
- FalkorDB required a recorded preflight schema/data repair before mixed measurement and a recorded post-workload repair after an initial filtered-lookup validation failure. Its immediately preceding preflight and final post-repair canonical validations pass; the repair history must remain disclosed.

The JSON artifact contains the raw-to-summary reconciliation and detailed evidence.
