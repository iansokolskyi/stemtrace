"""CLI entry point for stemtrace server."""

from typing import Annotated

import typer

app = typer.Typer(
    name="stemtrace",
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
    """Start the stemtrace web server with embedded consumer."""
    import uvicorn
    from fastapi import FastAPI

    from stemtrace.server.fastapi.extension import StemtraceExtension

    typer.echo(f"Starting stemtrace server on {host}:{port}")
    typer.echo(f"Broker: {broker_url}")

    from stemtrace.server.ui.static import get_static_router

    # Create extension without UI (we'll serve UI at root separately)
    extension = StemtraceExtension(broker_url=broker_url, serve_ui=False)
    fastapi_app = FastAPI(
        title="stemtrace",
        lifespan=extension.lifespan,
    )

    # API and WebSocket at /stemtrace (frontend expects this path)
    fastapi_app.include_router(extension.router, prefix="/stemtrace")

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
    ] = "stemtrace",
    ttl: Annotated[
        int,
        typer.Option("--ttl", help="Event TTL in seconds"),
    ] = 86400,
) -> None:
    """Run the event consumer standalone (for external processing)."""
    import signal
    import sys

    from stemtrace.server.consumer import EventConsumer
    from stemtrace.server.store import GraphStore

    typer.echo("Starting stemtrace consumer (standalone mode)")
    typer.echo(f"Broker: {broker_url}")

    store = GraphStore()
    consumer = EventConsumer(broker_url, store, prefix=prefix, ttl=ttl)

    def handle_signal(_signum: int, _frame: object) -> None:
        """Handle shutdown signals gracefully."""
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
    from stemtrace import __version__

    typer.echo(f"stemtrace {__version__}")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
