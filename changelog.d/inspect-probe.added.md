- The server runs Celery inspect in the background when it starts and when it learns a worker
  nodename it didn't have (a `worker_ready`, the first task event from an unregistered nodename, or
  an inspect reply that names a new node), so the Workers and Registry data fills without a page
  load. It uses the same limits as a page load (at most once every 2 seconds), coalesces triggers
  that arrive during an inspect into one follow-up, and never runs on the consumer thread or the
  event loop. With `create_router()` and no `StemtraceExtension`, the app's startup event starts
  the first inspect. See [Workers and registry tabs](docs/features.md#workers-and-registry-tabs).
