- `GET /api/anomalies` now reports `unregistered` and `no_workers_online`. A PENDING task is
  unregistered when it has waited longer than `grace` and no online worker registers its name:
  the list has one item per name (`task_id` is `null`, `task_count` counts the waiting tasks),
  and each of those tasks gets `anomaly: "unregistered"`. When no worker is online, the list has
  one `no_workers_online` item instead, and no task gets the field. The server reports neither
  unless its worker registry is recent: the last `inspect` reply listed every answering worker's
  tasks within `authority_window`, or no broker URL is set and an online worker has registered
  tasks. With a broker URL but no working inspect, no task is reported unregistered. Two server
  settings tune it: `STEMTRACE_ANOMALY_PENDING_GRACE` (`5m`) and
  `STEMTRACE_ANOMALY_REGISTRY_WINDOW` (`10m`), also `--anomaly-pending-grace`,
  `--anomaly-registry-window` and the `anomaly_pending_grace` and `anomaly_registry_window`
  arguments of `init_app` and `StemtraceExtension`. See
  [Unregistered](docs/features.md#unregistered) for the rules and limits.
