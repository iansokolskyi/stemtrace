- The task registry no longer lists Celery built-in tasks (`celery.chord_unlock` and other
  `celery.*` names) as "Not registered". Workers never register these tasks, so the
  registry showed them with a false "No worker has this task registered" warning.
