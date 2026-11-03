- `GET /api/anomalies` now reports `unregistered`. A PENDING task is unregistered when it has
  waited longer than `grace` and no live worker registers its name: the list has one item per
  name (`task_id` is `null`, `task_count` counts the waiting tasks), and each of those tasks gets
  `anomaly: "unregistered"`. A worker is live until it reports a stop or a newer process of the
  same nodename replaces it, so a missed `inspect` call never drops its names. The server
  reports it only while some worker is live (a fleet that is down raises no flag) and its
  worker registry is recent: the last `inspect` reply listed every
  answering worker's tasks within `authority_window`, or no broker URL is set and a live worker
  has registered tasks. With a broker URL but no working `inspect`, no task is reported
  unregistered. Two server settings tune it: `STEMTRACE_ANOMALY_PENDING_GRACE` (`5m`) and
  `STEMTRACE_ANOMALY_REGISTRY_WINDOW` (`10m`), also `--anomaly-pending-grace`,
  `--anomaly-registry-window` and the `anomaly_pending_grace` and `anomaly_registry_window`
  arguments of `init_app` and `StemtraceExtension`. See
  [Unregistered](docs/features.md#unregistered) for the rules and limits.
