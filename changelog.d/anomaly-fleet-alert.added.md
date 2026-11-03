- `GET /api/anomalies` now reports `no_workers_online`: one item when tasks wait past `grace`
  and no worker is left to run them. It replaces the per-name `unregistered` items for that
  time. The server reports it when it has seen a worker since it started and every worker it
  knows is offline: stopped, replaced by a newer process of the same nodename, or silent for
  more than 3 minutes while an `inspect` call that completed after the worker's last sign of
  life didn't reach it. Silence counts only after some worker has answered an `inspect` call, so workers with
  remote control off never look silent. Without a broker URL, or while the broker is
  unreachable, silence isn't evidence, so only a reported stop counts. The item needs neither `authority_window` nor a
  trusted registry. A task event from a worker that never sent `worker_ready` now counts as a
  sign of life for this rule only, and the Workers page doesn't list that worker. See
  [No workers online](docs/features.md#no-workers-online) for the rules and limits.
