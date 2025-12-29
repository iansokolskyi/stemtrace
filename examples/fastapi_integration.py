#!/usr/bin/env python3
"""Example: Integrate celery-flow into your FastAPI application.

This example shows how to mount celery-flow as a router in your existing
FastAPI app, with an embedded consumer for development.

Usage:
    pip install celery-flow[server]
    uvicorn examples.fastapi_integration:app --reload
"""

from fastapi import FastAPI

from celery_flow.server import CeleryFlowExtension

# Configuration
BROKER_URL = "redis://localhost:6379/0"

# Create the celery-flow extension
flow = CeleryFlowExtension(
    broker_url=BROKER_URL,
    embedded_consumer=True,  # Run consumer in background
    serve_ui=True,  # Serve the React UI
)

# Create FastAPI app with celery-flow lifespan
app = FastAPI(
    title="My App with celery-flow",
    lifespan=flow.lifespan,
)

# Mount celery-flow router
app.include_router(flow.router, prefix="/celery-flow")


# Your own routes
@app.get("/")
async def root() -> dict[str, str]:
    """Redirect to celery-flow UI."""
    return {"message": "Welcome! Visit /celery-flow for task monitoring."}


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "celery_flow_consumer": "running"
        if flow.consumer and flow.consumer.is_running
        else "stopped",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
