- `GET /api/anomalies` now reports `unregistered` and `no_workers_online`. A PENDING task is
  unregistered when it has waited longer than `grace` and no live worker registers its name:
  the list has one item per name (`task_id` is `null`, `task_count` counts the waiting tasks),
  and each of those tasks gets `anomaly: "unregistered"`. When some task has waited past `grace` and no worker has been seen within 3 minutes, the list has
  one `no_workers_online` item instead, for the whole outage, with no broker URL needed, and no
  task gets the field. A name counts as registered while its worker is live, until it reports a
  stop or the 30-minute cleanup removes it. Per-name items need a recent worker registry: the
  last `inspect` reply listed every answering worker's tasks within `authority_window`, or no
  broker URL is set and a live worker has registered tasks. With a broker URL but no working
  inspect, no task is reported unregistered. Two server
  settings tune it: `STEMTRACE_ANOMALY_PENDING_GRACE` (`5m`) and
  `STEMTRACE_ANOMALY_REGISTRY_WINDOW` (`10m`), also `--anomaly-pending-grace`,
  `--anomaly-registry-window` and the `anomaly_pending_grace` and `anomaly_registry_window`
  arguments of `init_app` and `StemtraceExtension`. See
  [Unregistered](docs/features.md#unregistered) for the rules and limits.
