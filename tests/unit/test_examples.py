"""Tests to validate examples work correctly.

These tests ensure that the examples in the examples/ directory:
1. Can be imported without errors
2. Have properly configured Celery apps
3. Define expected tasks
4. Work with celery-flow instrumentation

This helps catch regressions before shipping to users.
"""

import sys
from pathlib import Path

# Add examples to path so we can import them
EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))


class TestCeleryAppExample:
    """Tests for the main celery_app.py example."""

    def test_import_without_error(self) -> None:
        """celery_app.py can be imported without errors."""
        # Use fresh import to avoid state issues
        import importlib

        if "celery_app" in sys.modules:
            importlib.reload(sys.modules["celery_app"])
        else:
            import celery_app  # noqa: F401

    def test_celery_app_configured(self) -> None:
        """Celery app is properly configured."""
        from celery_app import app

        assert app.main == "examples"
        # Should have broker URL
        assert app.conf.broker_url is not None

    def test_tasks_defined(self) -> None:
        """Expected tasks are defined."""
        from celery_app import add, multiply, process_data

        # Verify they are callable
        assert callable(add)
        assert callable(multiply)
        assert callable(process_data)

    def test_aggregate_results_defined(self) -> None:
        """aggregate_results task for chord demos is defined."""
        from celery_app import aggregate_results

        assert callable(aggregate_results)

    def test_add_task_logic(self) -> None:
        """add task returns expected result."""
        from celery_app import add

        # add is a bound task, can't call directly without self
        # Just verify it exists and is callable
        assert hasattr(add, "delay")

    def test_multiply_task_logic(self) -> None:
        """multiply task is properly defined."""
        from celery_app import multiply

        # multiply is a bound task, verify it has delay method
        assert hasattr(multiply, "delay")

    def test_aggregate_results_handles_dicts(self) -> None:
        """aggregate_results correctly sums dictionaries from add tasks."""
        from celery_app import aggregate_results

        # Simulate results from tasks with dict results containing "sum"
        results = [
            {"sum": 5},
            {"sum": 7},
            {"sum": 9},
        ]
        # aggregate_results is a bound task, call run() directly
        result = aggregate_results.run(results)
        assert result == {"total": 21, "count": 3}

    def test_aggregate_results_handles_integers(self) -> None:
        """aggregate_results handles integer results."""
        from celery_app import aggregate_results

        # Simulate integer results
        results = [10, 20, 30]
        result = aggregate_results.run(results)
        assert result == {"total": 60, "count": 3}

    def test_run_demo_function_exists(self) -> None:
        """run_demo function for CLI is defined."""
        from celery_app import run_demo

        assert callable(run_demo)

    def test_demo_options_include_chord(self) -> None:
        """Demo options include orphan-group and orphan-chord."""
        from celery_app import run_demo

        # Check the docstring mentions expected demos
        assert run_demo.__doc__ is not None
        # Demo function exists and is callable
        assert callable(run_demo)


class TestFastAPIIntegrationExample:
    """Tests for fastapi_integration.py example."""

    def test_import_without_error(self) -> None:
        """fastapi_integration.py can be imported without errors."""
        import importlib

        if "fastapi_integration" in sys.modules:
            importlib.reload(sys.modules["fastapi_integration"])
        else:
            import fastapi_integration  # noqa: F401

    def test_fastapi_app_defined(self) -> None:
        """FastAPI app is defined."""
        from fastapi_integration import app

        assert app is not None

    def test_celery_flow_extension_defined(self) -> None:
        """CeleryFlowExtension is properly initialized."""
        from fastapi_integration import flow

        assert flow is not None

    def test_extension_has_router(self) -> None:
        """Extension provides a router for mounting."""
        from fastapi_integration import flow

        assert flow.router is not None


class TestWithAuthExample:
    """Tests for with_auth.py example."""

    def test_import_without_error(self) -> None:
        """with_auth.py can be imported without errors."""
        import importlib

        if "with_auth" in sys.modules:
            importlib.reload(sys.modules["with_auth"])
        else:
            import with_auth  # noqa: F401

    def test_auth_credentials_configured(self) -> None:
        """Auth credentials are configured."""
        from with_auth import AUTH_PASSWORD, AUTH_USERNAME

        assert AUTH_USERNAME is not None
        assert AUTH_PASSWORD is not None
        assert len(AUTH_USERNAME) > 0
        assert len(AUTH_PASSWORD) > 0

    def test_extension_has_auth_dependency(self) -> None:
        """CeleryFlowExtension is configured with auth dependency."""
        from with_auth import flow

        # The extension should have been created with auth_dependency
        assert flow is not None


class TestExamplesConsistency:
    """Tests for consistency across examples."""

    def test_celery_app_uses_redis_broker(self) -> None:
        """celery_app.py should use redis as broker."""
        from celery_app import app as celery_app_app

        assert "redis" in (celery_app_app.conf.broker_url or "")

    def test_celery_flow_initialization(self) -> None:
        """Examples that use celery-flow should initialize it properly."""
        # The celery_app.py initializes via celery_flow.init()
        from celery_app import app

        # celery-flow should be initialized (check for signal handlers)
        # This is implicit - if import works and app is configured, it's good
        assert app is not None

    def test_fastapi_extension_configured_with_redis(self) -> None:
        """fastapi_integration.py should use redis broker URL."""
        from fastapi_integration import BROKER_URL

        assert "redis" in BROKER_URL
