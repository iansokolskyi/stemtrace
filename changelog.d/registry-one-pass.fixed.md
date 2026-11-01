- Fixed the task registry (`GET /api/tasks/registry`) being slow on large stores. It now reads
  the store once per request instead of once per task name, so the response time no longer
  grows with the number of task names times the number of stored tasks. The response is
  unchanged.
