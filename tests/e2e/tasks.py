"""Celery application for E2E testing.

This module defines a minimal Celery app with test tasks for E2E tests.
Tasks include simple operations, workflows (chain/group), and failure scenarios.
"""

from __future__ import annotations

import os
import time

from celery import Celery, chain, group

import celery_flow

# Create Celery app with Redis broker
# Use port 16379 for E2E tests (Docker exposes Redis on alternate port)
app = Celery(
    "e2e_tasks",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:16379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:16379/1"),
)

# Configure Celery
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_send_sent_event=True,
)

# Initialize celery-flow tracking
celery_flow.init(app)


# =============================================================================
# Simple Tasks
# =============================================================================


@app.task(bind=True, name="e2e.add")
def add(self, x: int, y: int) -> int:
    """Add two numbers."""
    return x + y


@app.task(bind=True, name="e2e.multiply")
def multiply(self, x: int, y: int) -> int:
    """Multiply two numbers."""
    return x * y


@app.task(bind=True, name="e2e.slow_task")
def slow_task(self, duration: float = 1.0) -> str:
    """Task that takes a configurable amount of time."""
    time.sleep(duration)
    return f"Completed after {duration}s"


# =============================================================================
# Workflow Tasks
# =============================================================================


@app.task(bind=True, name="e2e.process_item")
def process_item(self, item: int) -> dict:
    """Process a single item."""
    return {"item": item, "processed": True, "result": item * 2}


@app.task(bind=True, name="e2e.aggregate")
def aggregate(self, results: list) -> dict:
    """Aggregate results from multiple tasks."""
    total = sum(r.get("result", 0) for r in results if isinstance(r, dict))
    return {"count": len(results), "total": total}


def create_workflow(items: list[int]):
    """Create a chain with a group: process items in parallel, then aggregate."""
    return chain(
        group(process_item.s(item) for item in items),
        aggregate.s(),
    )


# =============================================================================
# Failure Tasks
# =============================================================================


@app.task(bind=True, name="e2e.always_fails")
def always_fails(self, message: str = "Intentional failure") -> None:
    """Task that always raises an exception."""
    raise ValueError(message)


@app.task(
    bind=True,
    name="e2e.flaky_task",
    autoretry_for=(RuntimeError,),
    max_retries=2,
    default_retry_delay=1,  # Fast retries for E2E tests
    retry_backoff=False,
    retry_jitter=False,
)
def flaky_task(self, fail_count: int = 1) -> str:
    """Task that fails a specified number of times before succeeding.

    Args:
        fail_count: Number of times to fail before succeeding.
    """
    current_retry = self.request.retries or 0
    if current_retry < fail_count:
        raise RuntimeError(f"Failing on attempt {current_retry + 1}")
    return f"Succeeded on attempt {current_retry + 1}"
