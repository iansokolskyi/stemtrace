- `GET /api/anomalies` lists tasks that are stuck (running much longer than their name's p95)
  or interrupted (their worker stopped or restarted while they ran), with a count for every kind.
  `anomaly` on tasks and graph nodes and `anomaly_detail` on the task page carry the same
  result. Four server settings tune when a task is stuck: `STEMTRACE_ANOMALY_STUCK_FACTOR`,
  `_FLOOR`, `_CEILING` and `_MIN_SAMPLES` (also `--anomaly-stuck-*` and `init_app` arguments).
  See [Stuck and interrupted tasks](docs/features.md#stuck-and-interrupted-tasks) for the rules
  and limits.
