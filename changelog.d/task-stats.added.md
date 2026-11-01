- `GET /api/stats/tasks` returns runs, failures, failure rate, runtime p50 and p95 (nearest
  rank) and the last run for each task name, with a rollup per task group. Group percentiles
  come from all its tasks' runtimes together. It takes `within`, `from_date` and `name`, and
  leaves out `celery.*` tasks.
