#!/usr/bin/env python3
"""Example: stemtrace with authentication.

This example shows how to protect stemtrace endpoints with
basic authentication or API key authentication.

Usage:
    pip install stemtrace[server]
    uvicorn examples.with_auth:app --reload
"""

from fastapi import FastAPI

import stemtrace
from stemtrace import require_basic_auth

# Configuration
BROKER_URL = "redis://localhost:6379/0"
AUTH_USERNAME = "admin"
AUTH_PASSWORD = "secret"  # - example only

app = FastAPI(
    title="stemtrace with Auth",
)

# Initialize stemtrace with authentication (convenience API)
stemtrace.init_app(
    app,
    broker_url=BROKER_URL,
    embedded_consumer=True,
    auth_dependency=require_basic_auth(AUTH_USERNAME, AUTH_PASSWORD),
)


@app.get("/")
async def root() -> dict[str, str]:
    """Public endpoint."""
    return {"message": "stemtrace is at /stemtrace (requires auth)"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
