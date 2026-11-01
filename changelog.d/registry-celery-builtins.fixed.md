- The task registry no longer lists Celery built-in tasks (`celery.chord_unlock` and other
  `celery.*` names) as "Not registered". Workers never register these tasks, so the
  registry showed a false warning and counted them in the sidebar badge.
