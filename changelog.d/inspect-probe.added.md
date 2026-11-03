- The server runs Celery inspect in the background when it starts and when it learns a worker
  nodename it didn't have (a `worker_ready`, the first task event from an unregistered nodename, or
  an inspect reply that names a new node), so the Workers and Registry data fills without a page
  load. A background inspect starts at least 2 seconds after the last successful one (page load or
  background), triggers that arrive during an inspect coalesce into one follow-up, and it never
  runs on the consumer thread or the event loop. A new nodename that doesn't answer yet, such as a
  worker held in a slow `worker_ready` receiver, is asked again after 2, 4, 8, 16 and 30 seconds
  and then dropped. With `create_router()` and no `StemtraceExtension`, the app's startup event
  starts the first inspect. See [Workers and registry tabs](docs/features.md#workers-and-registry-tabs).
