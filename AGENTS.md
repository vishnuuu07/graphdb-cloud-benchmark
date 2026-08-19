# graphbench invariants

1. Never fabricate measurements or present unmeasured performance as a result.
2. Preserve identical logical workloads across all platforms.
3. Create fixtures once from the configured seed and replay the same values everywhere.
4. Publish failures and errors with raw measurements; do not silently discard them.
5. Keep credentials out of Git, logs, reports, and error messages.
6. Record resource mismatches and limitations as part of each benchmark run.
7. Do not tune, index, model, or batch-load only one database unfairly.
