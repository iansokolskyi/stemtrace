#!/usr/bin/env python3
"""Example: Integrate stemtrace into your FastAPI application.

This example shows how to mount stemtrace as a router in your existing
FastAPI app, with an embedded consumer for development.

Usage:
    pip install stemtrace[server]
    uvicorn examples.fastapi_integration:app --reload
"""

from fastapi import FastAPI

from stemtrace.server import StemtraceExtension

# Configuration
BROKER_URL = "redis://localhost:6379/0"

# Create the stemtrace extension
flow = StemtraceExtension(
    broker_url=BROKER_URL,
    embedded_consumer=True,  # Run consumer in background
    serve_ui=True,  # Serve the React UI
)

# Create FastAPI app with stemtrace lifespan
app = FastAPI(
    title="My App with stemtrace",
    lifespan=flow.lifespan,
)

# Mount stemtrace router
app.include_router(flow.router, prefix="/stemtrace")


# Your own routes
@app.get("/")
async def root() -> dict[str, str]:
    """Redirect to stemtrace UI."""
    return {"message": "Welcome! Visit /stemtrace for task monitoring."}


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "stemtrace_consumer": "running"
        if flow.consumer and flow.consumer.is_running
        else "stopped",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
