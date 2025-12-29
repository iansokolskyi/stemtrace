#!/usr/bin/env python3
"""Example: Celery application with stemtrace instrumentation.

This example shows how to instrument a Celery application to emit
task events for visualization, including:
- Task arguments and results capture
- Sensitive data scrubbing (passwords, API keys, etc.)
- Exception and traceback capture for retries/failures
- Group/chord visualization with synthetic GROUP nodes

Usage:
    # Install dependencies
    pip install stemtrace[redis]

    # Start Redis
    docker run -d -p 6379:6379 redis:alpine

    # Start worker
    celery -A examples.celery_app worker --loglevel=info

    # Start stemtrace server (in another terminal)
    stemtrace server

    # Run demo tasks
    python examples/celery_app.py workflow  # Complex workflow (chain + group)
    python examples/celery_app.py group     # Parallel group (synthetic GROUP node)
    python examples/celery_app.py retry     # Retry with exceptions
    python examples/celery_app.py scrub     # Sensitive data scrubbing
    python examples/celery_app.py fail      # Permanent failure
"""

from __future__ import annotations

import random
import sys
from typing import Any

from celery import Celery, chain, chord, group

import stemtrace

# Create Celery app
app = Celery(
    "examples",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

# Initialize stemtrace tracking
stemtrace.init(app)


# =============================================================================
# Basic Tasks
# =============================================================================


@app.task(bind=True, name="examples.celery_app.add")
def add(self, x: int, y: int) -> int:
    """Simple addition task."""
    return x + y


@app.task(bind=True, name="examples.celery_app.multiply")
def multiply(self, x: int, y: int) -> int:
    """Simple multiplication task."""
    return x * y


@app.task(bind=True, name="examples.celery_app.process_data")
def process_data(self, data: list[int]) -> dict[str, int]:
    """Process a list of numbers.

    Args:
        data: List of integers to process.

    Returns:
        Dictionary with sum, count, and average.
    """
    return {
        "sum": sum(data),
        "count": len(data),
        "avg": sum(data) // len(data) if data else 0,
    }


@app.task(bind=True, name="examples.celery_app.aggregate_results")
def aggregate_results(self, results: list[int | dict[str, int]]) -> dict[str, int]:
    """Aggregate multiple results.

    Handles both int results (from add tasks) and dict results (from process_data).
    """
    total = 0
    count = len(results)
    for r in results:
        if isinstance(r, dict):
            total += r.get("sum", 0)
        else:
            total += r
    return {"total": total, "count": count}


# =============================================================================
# Workflow Demo
# =============================================================================


@app.task(bind=True, name="examples.celery_app.workflow_example")
def workflow_example(self) -> str:
    """Run a complex workflow to demonstrate task graphs.

    Creates a workflow with:
    - A group of parallel tasks
    - A chain of sequential tasks
    - Nested child tasks
    """
    workflow = chain(
        group(
            process_data.s([1, 2, 3]),
            process_data.s([4, 5, 6]),
            process_data.s([7, 8, 9]),
        ),
        aggregate_results.s(),
    )

    result = workflow.apply_async()
    return f"Started workflow: {result.id}"


# =============================================================================
# Group Demo - Shows synthetic GROUP node visualization
# =============================================================================


@app.task(bind=True, name="examples.celery_app.batch_processor")
def batch_processor(self) -> str:
    """Process items in parallel using a group.

    Creates a synthetic GROUP node in the graph with 3 child add tasks.
    The GROUP becomes a child of this task, showing the parent→GROUP→tasks hierarchy.
    """
    result = group(add.s(1, 2), add.s(3, 4), add.s(5, 6)).apply_async()
    return f"Started batch processing: {result.id}"


# =============================================================================
# Retry Demo - Shows exception capture on retry
# =============================================================================


@app.task(
    bind=True,
    name="examples.celery_app.fetch_api_data",
    max_retries=3,
    autoretry_for=(ConnectionError,),
    retry_backoff=True,
    retry_backoff_max=10,
)
def fetch_api_data(self, url: str, api_key: str) -> dict[str, Any]:
    """Fetch data from external API.

    Demonstrates:
    - Sensitive data scrubbing (api_key -> [Filtered])
    - Real exception on retry (ConnectionError)
    - Result capture on success

    Args:
        url: API endpoint URL.
        api_key: API authentication key (will be scrubbed in UI).

    Returns:
        Mock API response data.
    """
    # Simulate random connection failures (70% chance)
    if random.random() < 0.7:
        raise ConnectionError(f"Failed to connect to {url}")

    return {"data": [1, 2, 3], "source": url, "status": "success"}


# =============================================================================
# Sensitive Data Scrubbing Demo
# =============================================================================


@app.task(bind=True, name="examples.celery_app.process_user_data")
def process_user_data(
    self,
    user_id: int,
    email: str,
    password: str,
    credit_card: str,
) -> dict[str, str]:
    """Process user registration.

    Demonstrates sensitive data scrubbing - password and credit_card
    will appear as [Filtered] in the stemtrace UI.

    Args:
        user_id: User identifier.
        email: User email (not sensitive).
        password: User password (SENSITIVE - will be scrubbed).
        credit_card: Payment info (SENSITIVE - will be scrubbed).

    Returns:
        Registration status.
    """
    # In real code, you'd hash the password and process payment
    return {
        "user_id": str(user_id),
        "email": email,
        "status": "created",
    }


# =============================================================================
# Failure Demo - Shows exception and traceback capture
# =============================================================================


@app.task(bind=True, name="examples.celery_app.always_fails", max_retries=1)
def always_fails(self, message: str) -> None:
    """A task that always fails.

    Demonstrates FAILURE state with full traceback visible in the UI.

    Args:
        message: Error message to include.

    Raises:
        ValueError: Always raised to demonstrate failure capture.
    """
    raise ValueError(f"Intentional failure: {message}")


# =============================================================================
# Legacy flaky task (kept for backwards compatibility)
# =============================================================================


@app.task(bind=True, name="examples.celery_app.flaky_task", max_retries=3)
def flaky_task(self) -> str:
    """A task that might fail and retry."""
    if random.random() < 0.5:
        raise self.retry(countdown=1)
    return "Success!"


# =============================================================================
# Demo Runner
# =============================================================================


def run_demo(demo_name: str) -> None:
    """Run a specific demo by name."""
    demos = {
        "workflow": lambda: workflow_example.delay(),
        "group": lambda: batch_processor.delay(),
        "standalone-group": lambda: group(
            add.s(1, 1), add.s(2, 2), add.s(3, 3)
        ).apply_async(),
        "standalone-chord": lambda: chord(
            group(add.s(10, 10), add.s(20, 20), add.s(30, 30)),
            aggregate_results.s(),
        ).apply_async(),
        "retry": lambda: fetch_api_data.delay(
            "https://api.example.com/data",
            api_key="sk-secret-key-12345",  # gitleaks:allow - demo credential
        ),
        "scrub": lambda: process_user_data.delay(
            user_id=42,
            email="alice@example.com",
            password="hunter2",
            credit_card="4111-1111-1111-1111",
        ),
        "fail": lambda: always_fails.delay("testing failure state"),
        "add": lambda: add.delay(2, 3),
        "multiply": lambda: multiply.delay(4, 5),
    }

    if demo_name not in demos:
        print(f"Unknown demo: {demo_name}")
        print(f"Available demos: {', '.join(demos.keys())}")
        sys.exit(1)

    result = demos[demo_name]()
    print(f"Started '{demo_name}' demo: {result.id}")
    print("View at: http://localhost:8000/stemtrace/")


if __name__ == "__main__":
    demo = sys.argv[1] if len(sys.argv) > 1 else "workflow"
    run_demo(demo)
