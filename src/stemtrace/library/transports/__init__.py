"""Deprecated alias of :mod:`stemtrace.transports`.

The transports moved to ``stemtrace.transports`` so the Celery integration
(``stemtrace.library``) and the server (``stemtrace.server``) can share them
without importing each other. This package and its submodules (``redis``,
``rabbitmq``, ``memory``, ``breaker``) re-export the same objects, so existing
imports keep working, but importing any of them emits one DeprecationWarning.
They will be removed in stemtrace 1.0.
"""

import warnings

from stemtrace.transports import get_transport

warnings.warn(
    "stemtrace.library.transports is deprecated and will be removed in "
    "stemtrace 1.0; import from stemtrace.transports instead (for example "
    "stemtrace.transports.redis)",
    DeprecationWarning,
    # Points at the importing module: warnings skips importlib's frames.
    stacklevel=2,
)

__all__ = ["get_transport"]
