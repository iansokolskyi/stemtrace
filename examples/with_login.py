#!/usr/bin/env python3
"""Example: stemtrace with built-in form login.

This example shows how to protect the stemtrace UI with a pretty login page.

Usage:
    pip install stemtrace[server]
    uvicorn examples.with_login:app --reload
"""

from fastapi import FastAPI

import stemtrace

# Configuration
BROKER_URL = "redis://localhost:6379/0"
LOGIN_USERNAME = "admin"
LOGIN_PASSWORD = "secret"  # NOSONAR - example credential for local demo only
LOGIN_SECRET = "change-me"  # recommended for production

app = FastAPI(title="stemtrace with Login")

stemtrace.init_app(
    app,
    broker_url=BROKER_URL,
    embedded_consumer=True,
    serve_ui=True,
    login_username=LOGIN_USERNAME,
    login_password=LOGIN_PASSWORD,
    login_secret=LOGIN_SECRET,
)


@app.get("/")
async def root() -> dict[str, str]:
    """Public endpoint."""
    return {"message": "Visit /stemtrace (login required)"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
