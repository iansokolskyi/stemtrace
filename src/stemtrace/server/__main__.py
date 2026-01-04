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
            envvar="STEMTRACE_BROKER_URL",
            help="Celery broker URL for on-demand inspection (default: redis://localhost:6379/0)",
        ),
    ] = "redis://localhost:6379/0",
    transport_url: Annotated[
        str | None,
        typer.Option(
            "--transport-url",
            "-t",
            envvar="STEMTRACE_TRANSPORT_URL",
            help="Event transport URL for consuming stemtrace events (default: broker_url)",
        ),
    ] = None,
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
    login_username: Annotated[
        str | None,
        typer.Option(
            "--login-username",
            envvar="STEMTRACE_LOGIN_USERNAME",
            help="Enable form login: allowed username (requires --login-password).",
        ),
    ] = None,
    login_password: Annotated[
        str | None,
        typer.Option(
            "--login-password",
            envvar="STEMTRACE_LOGIN_PASSWORD",
            help="Enable form login: password for --login-username.",
        ),
    ] = None,
    login_secret: Annotated[
        str | None,
        typer.Option(
            "--login-secret",
            envvar="STEMTRACE_LOGIN_SECRET",
            help="Secret used to sign session cookies (recommended for production).",
        ),
    ] = None,
    login_ttl: Annotated[
        int,
        typer.Option(
            "--login-ttl",
            envvar="STEMTRACE_LOGIN_TTL_SECONDS",
            help="Login session TTL in seconds.",
        ),
    ] = 86400,
) -> None:
    """Start the stemtrace web server with embedded consumer."""
    import secrets
    import urllib.parse
    from collections.abc import Awaitable, Callable

    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from starlette.responses import Response

    from stemtrace.server.fastapi.extension import StemtraceExtension
    from stemtrace.server.fastapi.form_auth import (
        FormAuthConfig,
        is_authenticated_cookie,
    )
    from stemtrace.server.fastapi.login_routes import create_login_router
    from stemtrace.server.ui.static import get_static_router_with_base

    typer.echo(f"Starting stemtrace server on {host}:{port}")
    typer.echo(f"Broker: {broker_url}")
    typer.echo(f"Transport: {transport_url or broker_url}")

    form_auth_config: FormAuthConfig | None = None
    if (login_username is None) != (login_password is None):
        raise typer.BadParameter(
            "Both --login-username and --login-password must be provided to enable login."
        )
    if login_username and login_password:
        # Cookie must cover both UI at '/' and API at '/stemtrace'.
        form_auth_config = FormAuthConfig(
            username=login_username,
            password=login_password,
            secret=login_secret or secrets.token_urlsafe(32),
            ttl_seconds=login_ttl,
            cookie_name="stemtrace_session",
            cookie_path="/",
        )

    # Create extension without UI (we'll serve UI at root separately)
    extension = StemtraceExtension(
        broker_url=broker_url,
        transport_url=transport_url,
        serve_ui=False,
        form_auth_config=form_auth_config,
    )
    fastapi_app = FastAPI(
        title="stemtrace",
        lifespan=extension.lifespan,
    )

    if form_auth_config is not None:
        # Login routes must be registered before the UI SPA catch-all.
        login_router = create_login_router(form_auth_config, default_next_path="/")
        fastapi_app.include_router(login_router)

        login_path = "/login"
        logout_path = "/logout"

        @fastapi_app.middleware("http")
        async def _form_auth_middleware(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            path = request.url.path
            if path in (login_path, logout_path):
                return await call_next(request)

            cookie_value = request.cookies.get(form_auth_config.cookie_name)
            if is_authenticated_cookie(
                cookie_value,
                secret=form_auth_config.secret,
                expected_username=form_auth_config.username,
            ):
                return await call_next(request)

            if path.startswith("/stemtrace/api") or path.startswith("/assets"):
                return JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                )

            next_target = path
            if request.url.query:
                next_target = f"{path}?{request.url.query}"
            next_qs = urllib.parse.urlencode({"next": next_target})
            return RedirectResponse(url=f"/login?{next_qs}", status_code=303)

    # API and WebSocket at /stemtrace (frontend expects this path)
    fastapi_app.include_router(extension.router, prefix="/stemtrace")

    # UI at root with explicit base path pointing to API
    ui_router = get_static_router_with_base(
        "/stemtrace",
        show_logout=form_auth_config is not None,
        logout_path="/logout",
    )
    if ui_router is not None:
        fastapi_app.include_router(ui_router)

    uvicorn.run(fastapi_app, host=host, port=port, reload=reload)


@app.command()
def consume(
    transport_url: Annotated[
        str,
        typer.Option(
            "--transport-url",
            "-t",
            envvar="STEMTRACE_TRANSPORT_URL",
            help="Event transport URL (default: redis://localhost:6379/0)",
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
    typer.echo(f"Transport: {transport_url}")

    store = GraphStore()
    consumer = EventConsumer(transport_url, store, prefix=prefix, ttl=ttl)

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
