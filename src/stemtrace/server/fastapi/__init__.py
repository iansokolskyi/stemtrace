"""FastAPI integration module."""

from stemtrace.server.fastapi.auth import no_auth, require_api_key, require_basic_auth
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.fastapi.router import create_router

__all__ = [
    "StemtraceExtension",
    "create_router",
    "no_auth",
    "require_api_key",
    "require_basic_auth",
]
