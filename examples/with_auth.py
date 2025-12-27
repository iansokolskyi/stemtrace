#!/usr/bin/env python3
"""Example: celery-flow with authentication.

This example shows how to protect the celery-flow endpoints with
basic authentication or API key authentication.

Usage:
    pip install celery-flow[server]
    uvicorn examples.with_auth:app --reload
"""

from fastapi import FastAPI

from celery_flow.server import CeleryFlowExtension, require_basic_auth

# Configuration
BROKER_URL = "redis://localhost:6379/0"
AUTH_USERNAME = "admin"
AUTH_PASSWORD = "secret"  # noqa: S105 - example only

# Create extension with authentication
flow = CeleryFlowExtension(
    broker_url=BROKER_URL,
    embedded_consumer=True,
    auth_dependency=require_basic_auth(AUTH_USERNAME, AUTH_PASSWORD),
)

app = FastAPI(
    title="celery-flow with Auth",
    lifespan=flow.lifespan,
)

app.include_router(flow.router, prefix="/celery-flow")


@app.get("/")
async def root() -> dict[str, str]:
    """Public endpoint."""
    return {"message": "celery-flow is at /celery-flow (requires auth)"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

