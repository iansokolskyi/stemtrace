"""CLI entry point for celery-flow server."""

from typing import Annotated

import typer

app = typer.Typer(
    name="celery-flow",
    help="Celery task flow visualizer",
    no_args_is_help=True,
)


@app.command()
def server(
    broker_url: Annotated[
        str,
        typer.Option(
            "--broker-url",
            "-b",
            envvar="CELERY_FLOW_BROKER_URL",
            help="Broker URL (default: redis://localhost:6379/0)",
        ),
    ] = "redis://localhost:6379/0",
    host: Annotated[
        str,
        typer.Option("--host", "-h", help="Host to bind to"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to bind to"),
    ] = 8000,
    reload: Annotated[
        bool,
        typer.Option("--reload", help="Enable auto-reload (development)"),
    ] = False,
) -> None:
    """Start the celery-flow web server with embedded consumer."""
    import uvicorn
    from fastapi import FastAPI

    from celery_flow.server.fastapi.extension import CeleryFlowExtension

    typer.echo(f"Starting celery-flow server on {host}:{port}")
    typer.echo(f"Broker: {broker_url}")

    from celery_flow.server.ui.static import get_static_router

    # Create extension without UI (we'll serve UI at root separately)
    extension = CeleryFlowExtension(broker_url=broker_url, serve_ui=False)
    fastapi_app = FastAPI(
        title="celery-flow",
        lifespan=extension.lifespan,
    )

    # API and WebSocket at /celery-flow (frontend expects this path)
    fastapi_app.include_router(extension.router, prefix="/celery-flow")

    # UI at root for standalone CLI
    ui_router = get_static_router()
    if ui_router is not None:
        fastapi_app.include_router(ui_router)

    uvicorn.run(fastapi_app, host=host, port=port, reload=reload)


@app.command()
def consume(
    broker_url: Annotated[
        str,
        typer.Option(
            "--broker-url",
            "-b",
            envvar="CELERY_FLOW_BROKER_URL",
            help="Broker URL (default: redis://localhost:6379/0)",
        ),
    ] = "redis://localhost:6379/0",
    prefix: Annotated[
        str,
        typer.Option("--prefix", help="Stream key prefix"),
    ] = "celery_flow",
    ttl: Annotated[
        int,
        typer.Option("--ttl", help="Event TTL in seconds"),
    ] = 86400,
) -> None:
    """Run the event consumer standalone (for external processing)."""
    import signal
    import sys

    from celery_flow.server.consumer import EventConsumer
    from celery_flow.server.store import GraphStore

    typer.echo("Starting celery-flow consumer (standalone mode)")
    typer.echo(f"Broker: {broker_url}")

    store = GraphStore()
    consumer = EventConsumer(broker_url, store, prefix=prefix, ttl=ttl)

    def handle_signal(_signum: int, _frame: object) -> None:
        typer.echo("\nShutting down consumer...")
        consumer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    consumer.start()
    typer.echo("Consumer running. Press Ctrl+C to stop.")

    try:
        signal.pause()
    except AttributeError:
        # Windows doesn't have signal.pause
        import time

        while consumer.is_running:
            time.sleep(1)


@app.command()
def version() -> None:
    """Show version information."""
    from celery_flow import __version__

    typer.echo(f"celery-flow {__version__}")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
