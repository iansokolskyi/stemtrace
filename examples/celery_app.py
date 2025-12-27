#!/usr/bin/env python3
"""Example: Celery application with celery-flow instrumentation.

This example shows how to instrument a Celery application to emit
task events for visualization.

Usage:
    # Install dependencies
    pip install celery-flow[redis]
    
    # Start worker
    celery -A examples.celery_app worker --loglevel=info
    
    # Run some tasks
    python -c "from examples.celery_app import *; workflow_example.delay()"
"""

from celery import Celery, chain, group

from celery_flow import init

# Create Celery app
app = Celery(
    "examples",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

# Initialize celery-flow tracking
init(app)


# Example tasks
@app.task(bind=True)
def add(self, x: int, y: int) -> int:
    """Simple addition task."""
    return x + y


@app.task(bind=True)
def multiply(self, x: int, y: int) -> int:
    """Simple multiplication task."""
    return x * y


@app.task(bind=True)
def process_data(self, data: list[int]) -> dict[str, int]:
    """Process a list of numbers."""
    return {
        "sum": sum(data),
        "count": len(data),
        "avg": sum(data) // len(data) if data else 0,
    }


@app.task(bind=True)
def aggregate_results(self, results: list[dict[str, int]]) -> dict[str, int]:
    """Aggregate multiple results."""
    total = sum(r.get("sum", 0) for r in results)
    count = sum(r.get("count", 0) for r in results)
    return {"total": total, "count": count}


@app.task(bind=True)
def workflow_example(self) -> str:
    """Run a complex workflow to demonstrate task graphs.

    Creates a workflow with:
    - A group of parallel tasks
    - A chain of sequential tasks
    - Nested child tasks
    """
    # Create a workflow: parallel processing then aggregation
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


@app.task(bind=True, max_retries=3)
def flaky_task(self) -> str:
    """A task that might fail and retry."""
    import random

    if random.random() < 0.5:  # noqa: S311
        raise self.retry(countdown=1)
    return "Success!"


if __name__ == "__main__":
    # Run a sample workflow
    print("Starting workflow...")  # noqa: T201
    result = workflow_example.delay()
    print(f"Workflow started: {result.id}")  # noqa: T201

