- The UI has an Anomalies page (`/anomalies`) that lists stuck, interrupted and unregistered tasks
  with an explanation and links to the task, the worker, the Registry and the matching runs. When
  no worker is online and tasks are waiting, one banner at the top replaces a row per task. The
  sidebar shows the number of active anomalies, the Executions table has an Anomaly column
  (when a loaded row is flagged), and a flagged task page opens with a callout that says what is
  wrong.
