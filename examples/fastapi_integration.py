#!/usr/bin/env python3
"""Example: Integrate stemtrace into your FastAPI application.

This example shows how to mount stemtrace as a router in your existing
FastAPI app, with an embedded consumer for development.

Usage:
    pip install stemtrace
    uvicorn examples.fastapi_integration:app --reload
"""

from fastapi import FastAPI

import stemtrace

# Configuration
#
# `broker_url` is Celery's broker (used for on-demand worker inspection).
# `transport_url` is where stemtrace consumes events from (defaults to broker_url).
BROKER_URL = "redis://localhost:6379/0"
TRANSPORT_URL = None  # e.g. "redis://localhost:6379/0"

# Create FastAPI app
app = FastAPI(
    title="My App with stemtrace",
)

# Initialize stemtrace in one line
stemtrace.init_app(
    app,
    broker_url=BROKER_URL,
    transport_url=TRANSPORT_URL,
)


# Your own routes
@app.get("/")
async def root() -> dict[str, str]:
    """Redirect to stemtrace UI."""
    return {"message": "Welcome! Visit /stemtrace for task monitoring."}


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    #
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
