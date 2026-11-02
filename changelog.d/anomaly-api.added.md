- `GET /api/anomalies` lists tasks that are stuck or interrupted, with a count for every kind.
  A task is stuck when it is STARTED and the time since its last progress exceeds the larger of
  `factor × p95` and the floor, or the larger of the ceiling and the floor when its name has
  fewer than `min_samples` finished runs. A task is interrupted when its worker stopped or
  restarted while it ran. `anomaly` on tasks and graph nodes and `anomaly_detail` on
  `GET /api/tasks/{id}` carry the same result. Four server settings tune when a task is stuck:
  `STEMTRACE_ANOMALY_STUCK_FACTOR`, `_FLOOR`, `_CEILING` and `_MIN_SAMPLES` (also
  `--anomaly-stuck-*` and `init_app` arguments). `StemtraceExtension.worker_timeline` exposes
  the worker event history for a custom consumer. See
  [Stuck and interrupted tasks](docs/features.md#stuck-and-interrupted-tasks) for the rules and
  limits.
