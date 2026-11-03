- `GET /api/anomalies` and `anomaly_detail` add `similar_name` to unregistered items: a live task
  in the same module whose name is a close match (the last segment, ignoring a trailing `_v<N>`,
  scores at least 0.75 and no other live task ties), such as `sync_invoices` for
  `sync_invoices_v2`. It is `null` otherwise. The hint can be wrong for different tasks with near
  names. See [Unregistered](docs/features.md#unregistered).
