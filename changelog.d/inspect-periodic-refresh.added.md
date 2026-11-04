- The server keeps its worker registry trusted without requests: when the last inspect reply that
  listed every worker's tasks is older than half of `anomaly_registry_window` (5 minutes by default,
  at least 10 seconds) and a worker is online, it asks the workers again, so a `refresh=false`
  request still reports unregistered tasks on an idle fleet. An ask that doesn't stamp (a failure, a
  missing `registered()` reply) is retried on the 2, 4, 8 ... 60 s wait, for page loads too. A worker
  that restarts under the same nodename triggers the ask at once. One server process asks at a time;
  several processes each ask on their own.
