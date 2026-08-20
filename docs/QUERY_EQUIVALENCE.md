# Query equivalence audit

This is the canonical workload contract used by the frozen final campaign
`final-20260820T022802Z`. It describes logical operations and adapter translations; it does not
reinterpret or normalize measured latency.

The Python `CanonicalOracle` reads the one processed Wiki-Vote fixture set and is the
independent correctness authority for every adapter. User identity is always the
integer `User.id`; `bucket = id % 32`. No adapter changes topology, fixture values,
or the directed `VOTED_FOR` relation.

The fixed Cypher patterns count relationship-distinct paths (not distinct destination
vertices). A relationship instance cannot occur twice in one path; vertices may repeat.
ArangoDB explicitly uses `uniqueVertices: "none"` and `uniqueEdges: "path"`. FalkorDB
uses explicit relationship identity predicates across its separated `MATCH` clauses.
All traversal directions are outgoing.

| Workload | Logical operation / canonical output | Neo4j and CognoDB Cypher | Memgraph | FalkorDB | ArangoDB AQL | Required index / direction |
| --- | --- | --- | --- | --- | --- | --- |
| Point lookup | Count `User` where `id == $id` (0 or 1) | `MATCH (u:User {id:$id}) RETURN count(u)` | Same query through Bolt | Same parameterized Cypher through FalkorDB client | `FOR u IN users FILTER u.id == @id RETURN 1`, counted | `User.id` / `users.id` |
| Filtered lookup | Count users where `bucket == $bucket` | `MATCH (u:User {bucket:$bucket}) RETURN count(u)` | Same | Same | `FOR u IN users FILTER u.bucket == @bucket RETURN 1`, counted | `User.bucket` / `users.bucket` |
| 1 hop | Count all exact one-edge outgoing paths | Fixed `-[:VOTED_FOR]->(:User)` + `count(*)` | Same | Same | `1..1 OUTBOUND start voted_for`, counted | start lookup uses `id`; outbound |
| 2 hop | Count all exact two-edge outgoing paths | Two fixed outgoing patterns + `count(*)` | Same | `WITH` plus `ID(r2) <> ID(r1)` | `2..2 OUTBOUND start voted_for`, `uniqueEdges:"path"` | start lookup uses `id`; outbound |
| 3 hop | Count all exact three-edge outgoing paths | Three fixed outgoing patterns + `count(*)` | Same | Explicit `ID(r1/r2/r3)` inequality predicates | `3..3 OUTBOUND start voted_for`, `uniqueEdges:"path"` | start lookup uses `id`; outbound |
| Aggregation | Complete `{bucket: user_count}` map ordered by bucket | `RETURN u.bucket, count(u) ORDER BY u.bucket` | Same | Same | `COLLECT bucket = u.bucket WITH COUNT INTO count SORT bucket` | bucket index is equivalent schema support; no direction |
| Write primitive | Increment only `benchmark_counter` for one canonical `id`; return affected count | `SET u.benchmark_counter = coalesce(...,0)+1` | Same | Same | `UPDATE u WITH {benchmark_counter: NOT_NULL(...,0)+1}` | `id`; no topology or canonical property mutation |

## Loading and identity notes

Neo4j, CognoDB, Memgraph, and FalkorDB use parameterized `UNWIND` driver batches.
ArangoDB uses parameterized native `insert_many` batches into `users` and `voted_for`.
For ArangoDB, `_key` is a stable storage key derived from the integer ID only to form
native edge endpoints; the canonical identity remains the separately stored integer
`users.id`, and every logical lookup uses that property. `_from` is
`users/<source_id>` and `_to` is `users/<target_id>`, preserving source-to-target
direction. Edges are inserted without an application edge key so duplicate canonical
edges remain distinct relationship instances.

FalkorDB uses its maintained client rather than Bolt. Its Cypher parameter syntax is
encapsulated by the adapter's client call. Memgraph uses its Bolt-compatible protocol
but its label/property index DDL rather than Neo4j constraint DDL. These transport and
DDL differences do not alter workload semantics.
