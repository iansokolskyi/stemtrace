- `GraphStore.get_task_execution_count()` and `GraphStore.get_last_execution_time()` are
  removed. They scanned the whole store for one task name. To get every name's run count
  and last run in one pass, call `GraphStore.get_task_run_summaries()`.
