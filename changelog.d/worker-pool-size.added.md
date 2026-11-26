- `GET /api/workers` returns `pool` (for example `prefork`, `threads`, `solo`, `gevent` or
  `eventlet`) and `concurrency` (the pool size, or the `--autoscale` maximum) for each worker, read
  from Celery `stats()` replies. Both are `null` when the worker is known only from events or the
  reply doesn't carry them. Celery 5.2.2 doesn't send the pool class, so `pool` is `null` there.
