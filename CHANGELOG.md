# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.9] - 2026-10-10

### Added
- Task output: with `init_worker(app, capture_output=True)`, what each task attempt prints and logs is shown on its task page, one block per attempt (latest first). Off by default because output isn't scrubbed; each attempt keeps the last `max_output_size` bytes (default 16 KB). Works with prefork, thread and gevent pools (thanks @JonatanMartens for the original implementation — #49; requested by @ohadmata — #47).

## [0.3.8] - 2026-10-10

### Fixed
- `node_alias_from_arguments` can now be set on the standalone server and Docker image: `stemtrace server --node-alias-from-arguments <key>` or `STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS=<key>` (a kwarg name or a positional index). `init_app()` reads the same env var when the argument isn't passed (#69).
- RabbitMQ: publishing from workers no longer blocks tasks while the broker is down, stalled or in a memory/disk alarm. Each publish tries every host of the URL once with a 2s timeout; repeated or slow failures open a circuit breaker that drops events (instead of queueing them) until the broker answers again, and the worker logs at most four warnings a minute with counts of dropped events. The README's broker section has the details and limits.
- RabbitMQ: the server's consumer now reconnects after the broker restarts or drops the connection; it used to retry forever on a dead channel.
- RabbitMQ: a broker outage no longer floods the server log with one ERROR traceback per second; reconnects back off from 1s to 30s and log one line per attempt, plus one when the connection is back (thanks @ohadmata — #70, #71).
- RabbitMQ: a message the consumer can't decode is dropped with a warning instead of blocking the queue forever.
- RabbitMQ: stopping the server no longer waits out the reconnect delay or hangs on a frozen broker.

### Added
- Health endpoint: `consumer_connected` is now reported for RabbitMQ, so `/api/health` shows `degraded` while the server can't read from the broker.

### Deprecated
- `init_worker(node_alias_from_arguments=...)` has no effect (node names are resolved by the server) and now logs a deprecation warning; it will be removed in a future major release.

## [0.3.7] - 2026-10-10

### Changed
- Docker: the image's `HEALTHCHECK` now calls the new `<prefix>/api/health/live` endpoint instead of `/stemtrace/api/health`, which returns 401 under form login (so containers with login enabled reported unhealthy). The liveness endpoint needs no session and checks only that the process is serving: it always answers 200, with an informational `{"status": "ok"}`, or `{"status": "degraded"}` by the same rule as `/api/health` (the event consumer stopped unexpectedly or can't read from the broker; a consumer stopped on purpose or whose event source finished is not degraded).
- Docker image: updated the locked runtime dependencies `fastapi` 0.141.1 → 0.142.2 (which now pulls in `opentelemetry-api` 1.45.1; stemtrace doesn't configure tracing, so it stays a no-op), `pydantic` 2.12.5 → 2.13.5 (`pydantic-core` 2.41.5 → 2.46.5), `celery` 5.6.0 → 5.6.3, `typer` 0.21.0 → 0.27.3, `typing-extensions` 4.15.0 → 4.16.0 and `websockets` 15.0.1 → 16.1.1 (used by uvicorn's WebSocket support). Only the lockfile changed: the package's dependency ranges are the same, so pip installs are unaffected. With typer 0.27, `--help` shows option types as `<str>`/`<int>` instead of `TEXT`/`INTEGER`.
- Docker image: building the image from a local checkout no longer copies the frontend's `node_modules/`, `dist/` or test artifacts into the build context, so stale local UI assets can't end up in the image.
- UI: updated `@tanstack/react-query` 5.94.5 → 5.104.1 in the bundled frontend.

### Fixed
- Form login: clear warnings and an in-app explanation when sessions are rejected because workers/replicas don't share `STEMTRACE_LOGIN_SECRET`. Without an explicit secret each process signs cookies with its own random secret, so under `uvicorn --workers N`, gunicorn or several replicas a cookie issued by one process is rejected by the others (random 401s, unstyled UI, login loops). stemtrace now logs a startup warning when form login runs without a secret (mentioning `WEB_CONCURRENCY` > 1 or gunicorn when detected). In that mode, when a well-formed, unexpired session cookie fails signature verification and the issue time it carries (`iat`, now included in the signed session payload; unverified in this case and used only to classify the rejection, never to grant access) is after the process started (so it most likely came from a sibling worker or replica), it also logs a rate-limited warning (also for rejected WebSocket handshakes) and returns `"reason": "session_secret_mismatch"` in API 401 responses, and HTML routes redirect to the login page with `reason=session_mismatch`, which shows an explanation. Cookies from before a restart, and any badly signed cookie when a secret is configured, get a plain 401 / login redirect.
- Form login: the static UI bundle under `/assets/` (the public open-source JS/CSS build, no data) is now served without a session, so the UI always loads and is styled instead of rendering a blank or unstyled page when a bundle request is rejected. The UI routes, API and WebSocket stay protected, and paths containing `.` or `..` segments (including percent-encoded variants), or an encoded `?` or `#` (`%3F`, `%23`), are rejected with 404 before any exemption applies. Exemptions are decided on the decoded path that routing matches, so e.g. `/api/health/live%3Fx` can't pass as the liveness probe. Paths under `assets/` that aren't real bundle files return 404 instead of the app shell.
- UI: the Logout control is now a button in the header instead of a floating overlay injected by the server, which covered the connection status. On narrow screens the header wraps onto extra rows instead of overflowing, and the status tooltip stays inside the viewport.
- UI: a 401 from the API (missing or expired session) now redirects to the login page with `next` set to the current page, instead of showing "Failed to load ..." errors. A 401 with `reason: "session_secret_mismatch"` shows an explanation instead, since signing in again wouldn't help. Redirects happen at most once per page and not again within a few seconds of the last one, which prevents redirect loops. The WebSocket reconnect now backs off exponentially (3s up to 60s) instead of retrying every 3s forever.
- Task state no longer regresses when events arrive out of order. The sender-side `PENDING` event (from `task_sent`, which fires after publish) could reach the stream after the worker's `STARTED`/`SUCCESS`, and the task showed as `PENDING` again. Node state now comes from the most advanced event, and the result is the same for every arrival order. A terminal state (`SUCCESS`/`FAILURE`/`REVOKED`/`REJECTED`) always beats a non-terminal one, and between terminals the later one wins. Among other states, a newer attempt (higher `retries`) wins, then lifecycle order (`PENDING` < `RECEIVED` < `STARTED`/`RETRY`), with `STARTED` vs `RETRY` in the same attempt decided by time (a redelivered run after a connection-loss retry shows as `STARTED`). A task's events are kept in lifecycle order: the first attempt's `PENDING` then `RECEIVED` first (even when stamped after the task was received or finished), then everything else by timestamp, with a fixed order for identical timestamps. `first_seen`, `last_updated`, `duration_ms`, date filters, sorting and eviction follow that order instead of arrival order. Node aliases from task arguments (`node_alias_from_arguments`) are read from the first event that carries arguments, so a leading `RECEIVED` (which has none) no longer hides them. Task lists, task details and graphs are read from a single snapshot taken under the store lock, so a concurrent event can't make a response mix old and new data (e.g. a `?state=STARTED` filter returning a task that just finished). Naive event timestamps are treated as UTC. Exact duplicate events are ignored and not re-broadcast over the WebSocket.
- UI: the task timeline and event history follow the same order as the API. They used to sort by retry count first, so a failure raised in the worker main process (hard time limit, lost worker), which Celery reports with `retries=0`, appeared before later attempts and produced negative durations. A late-stamped `PENDING` is shown first, with its time clamped to the first run.
- The signal handler no longer raises `TypeError` when Celery cancels a running task (lost broker connection, cold shutdown): Celery sends `task_retry` without a `reason` on that path. The `RETRY` event is now created, with the exception text "cancelled by Celery". During a broker outage it still can't be delivered if stemtrace publishes through that same broker.

## [0.3.6] - 2026-10-10

### Security
- Updated UI bundle dependencies with known advisories (seroval, TanStack Router). The runtime (shipped) dependencies that changed are `@tanstack/react-router` 1.168.1 → 1.170.41, with its `@tanstack/router-core` 1.168.1 → 1.171.34, `@tanstack/history` 1.161.6 → 1.162.4, `@tanstack/react-store`/`@tanstack/store` 0.9.2 → 0.11.2 and `cookie-es` 2.0.0 → 3.1.1; and `seroval`/`seroval-plugins` 1.4.2 → 1.6.8, which fixes GHSA-3rxj-6cgf-8cfw, GHSA-hj76-42vx-jwp4, GHSA-mv8w-475r-vwqw, GHSA-p6vx-979v-rg4c and related denial-of-service advisories. seroval is only used by the router's server-side rendering code, which stemtrace doesn't use, and none of it ends up in the built UI bundle. `@tanstack/react-query` also moved 5.90.16 → 5.94.5 (no advisory). Build-time-only packages (Vite, Rollup, PostCSS and others) were updated too.

### Added
- Health endpoint: `consumer_connected` reports whether the event consumer's last broker read succeeded (`true`), is failing (`false`), or is unknown (`null`). Only the Redis transport reports it: it detects refused or reset connections, connect timeouts and repeated read timeouts. On redis-py < 8, whose default socket timeout is unbounded, a half-open or hung connection may go unnoticed unless the broker URL sets `socket_timeout` or `health_check_interval`. With RabbitMQ it is always `null`.
- UI: "Sprout" brand mark in the header and as the favicon (dashboard and login page). The favicon is an inline SVG data URI that follows the OS light/dark theme, so it needs no extra request, is never redirected by form login, and works under any mount prefix. Brand assets live in `docs/brand/`.

### Changed
- Minimum FastAPI is now 0.112.1 and minimum Starlette 0.38.6: the lowest versions verified by the test suite at release time. FastAPI 0.100–0.104 never worked with current Pydantic (building the app fails with `'FieldInfo' object has no attribute 'in_'`). Starlette is now a declared dependency because stemtrace imports it directly.
- Minimum redis-py is now 4.1.0, the lowest version verified by the test suite at release time; 4.0.x imports `distutils`, which was removed in Python 3.12.
- Docker image: Python dependencies are now installed from the project's lockfile, so each image ships the exact versions audited for that release instead of whatever was newest at build time. Base images are pinned by digest, and the UI is built with Node 22.
- Examples now bind to `127.0.0.1` instead of `0.0.0.0`.
- Docker image: stemtrace now runs from a virtual environment at `/opt/venv` (it's first on `PATH`), and the base image's unused system `pip` is removed. To add packages in a derived image, use `RUN python -m ensurepip && python -m pip install <package>`, which installs into `/opt/venv`.
- Docker image: the built-in `HEALTHCHECK` only checks the HTTP status, so a container whose event consumer is degraded (e.g. the broker is unreachable) still reports healthy. Probe `/stemtrace/api/health` and check its `status` field if you need to alert on that.
- Building from source: the UI build hook now runs `npm ci --ignore-scripts` (or `npm install --ignore-scripts` without a lockfile) instead of `npm install`. Set `STEMTRACE_SKIP_UI_BUILD=1` to skip the UI build entirely; a wheel built that way contains no UI.
- Health endpoint: `status` is now `"degraded"` instead of `"ok"` when the event consumer died unexpectedly, its loop keeps restarting after errors (such as `WRONGTYPE` or `NOPERM`), or `consumer_connected` is `false`. With RabbitMQ only consumer loop failures are detected. A consumer stopped on purpose, or whose event source finished normally (for example `memory://`), is not degraded. The response is still HTTP 200, so status-code-only health checks behave as before; check the `status` field to alert on a degraded consumer. See the README's "Health Endpoint" section.

### Fixed
- Redis transport: the server stopped receiving events after about 5 seconds of quiet when used with redis-py 8. redis-py 8 defaults `socket_timeout` to 5 seconds, the same as the `XREAD` block window, so the first idle read timed out and the consumer thread exited for good. The `XREAD` block window is now half of the client's effective socket timeout (2.5 seconds with redis-py 8 defaults, 5 seconds when the timeout is unbounded), so idle reads return normally on a single connection. This includes clients passed in directly, such as `redis.Redis.from_url(url)`. The client's own timeouts are unchanged, so publishing from tasks never blocks longer than before.
- Redis transport: losing the connection (for example a Redis restart, an unreachable host, or a connect timeout) no longer stops event consumption. The consumer logs one warning with a traceback, then one line per attempt, and retries with exponential backoff (1 s doubling to a 30 s cap, reset on success), resuming from the last stream entry it read. Repeated read timeouts are logged as warnings at most once a minute.
- Server: the event consumer thread restarts with the same backoff after an unexpected error instead of exiting silently, and `stop()` interrupts a pending reconnect wait instead of leaving the thread behind.
- UI: the favicon no longer 404s (the dashboard referenced a non-existent `/vite.svg`).
- UI: JS and CSS assets no longer 404 on FastAPI 0.137+ when stemtrace is installed with per-file symlinks (e.g. `uv pip install --link-mode symlink`). The `/assets` mount now follows symlinks inside the bundle; request paths are still confined to the assets directory.

## [0.3.5] - 2026-10-09

### Security
- UI: fixed a path traversal in the static file handler. Crafted URLs could read any file the server process can access, not just the bundled frontend. When the UI is exposed without authentication (no form login or `auth_dependency`, which is the default for `stemtrace server` and the Docker image), no credentials were needed. All users should upgrade. If you can't upgrade right away, put the UI behind authentication or a reverse proxy that blocks it from untrusted networks.

## [0.3.4] - 2026-06-25

### Fixed
- WebSocket: deny unauthenticated connections during the handshake instead of accepting and then closing them. The accept-then-close path half-opened the connection, so when an unauthenticated client (e.g. a port scanner) aborted mid-handshake, uvicorn's legacy `websockets` implementation ran `close()` on a protocol whose `transfer_data_task` was never set, raising `AttributeError: 'WebSocketProtocol' object has no attribute 'transfer_data_task'`. Closing before `accept()` rejects the handshake (HTTP 403) without opening the protocol object.
- Form login: build the login form action and redirects as relative paths instead of absolute URLs, and derive the session cookie's `Secure` flag from `X-Forwarded-Proto`. Behind a TLS-terminating proxy that doesn't forward the scheme to the ASGI scope (e.g. uvicorn without `--forwarded-allow-ips`), absolute URLs were downgraded to `http://`, so the browser posted the login form over http, the proxy answered `301 -> https`, the POST body was dropped, and login silently failed (with a mixed-content warning). stemtrace now works behind such a proxy without requiring forwarded-headers configuration.

## [0.3.3] - 2026-03-20

### Added
- Node alias from arguments: derive graph node display name from task args/kwargs via `node_alias_from_arguments` config option (thanks @ohadmata for the idea — #46, #48)
- Docker: multi-architecture image builds (amd64 + arm64) (thanks @ohadmata — #51)

### Fixed
- Graph: order task nodes before synthetic container nodes (GROUP/CHORD) for correct rendering (thanks @codyburrito — #52)

## [0.3.2] - 2026-02-28

### Fixed
- Redis transport: normalize `ssl_cert_reqs` URL parameter from `CERT_REQUIRED`/`CERT_NONE`/`CERT_OPTIONAL` to lowercase values expected by redis-py

## [0.3.1] - 2026-01-04

### Added
- UI: optional built-in form login flow for protecting the stemtrace UI

### Changed
- Config: split server broker URL from worker event transport URL (`broker_url` vs `transport_url`)
- API: avoid blocking worker/registry refresh operations
- Docs: clarify optional configuration and retention behavior

### Fixed
- Workers/Registry: refresh via Celery inspect to reduce stale worker/task registry data

## [0.3.0] - 2026-01-04

### Added
- RabbitMQ event transport

### Changed
- Tests: improved RabbitMQ transport coverage and reduced duplication

### Fixed
- UI: harden derived prefix sanitization
- CI/E2E: avoid artifact name conflicts

## [0.2.2] - 2026-01-03

### Added
- README: add release version badge

### Changed
- Packaging/docs: simplify badges and metadata
- README: pin PyPI badge to release version
- Tests: make version assertion dynamic

### Fixed
- Version bump: sync fix

## [0.2.1] - 2026-01-03

### Added
- Task Registry now includes task definition metadata from workers (module, signature, docstring, bound)

### Changed
- README: updated PyPI badge styling
- CI: upload Codecov coverage on `main`

## [0.2.0] - 2026-01-03

### Added
- Workers API endpoints: `GET /api/workers` and `GET /api/workers/{hostname}`
- Worker lifecycle tracking via `worker_ready` / `worker_shutdown` events to maintain a live worker registry
- Workers UI page for inspecting online/offline workers and their registered tasks
- Task Registry now shows which workers registered each task (`registered_by`)

## [0.1.1] - 2025-12-31

### Added
- Python 3.14 support in CI and project classifiers
- Mock-based E2E testing mode (no Docker required for local dev)
- Codecov integration for coverage reporting

### Changed
- Upgraded React and React-DOM to v19
- Upgraded @tanstack/react-query to latest
- Updated GitHub Actions to v6 (checkout, setup-python, setup-node, upload/download-artifact)
- Improved README with clearer Quick Start and FastAPI embedding docs

## [0.1.0] - 2025-12-27

### Added
- **Core domain models**: `TaskEvent`, `TaskState`, `TaskNode`, `TaskGraph`
- **Protocol definitions**: `EventTransport`, `TaskRepository`, `AsyncEventConsumer`
- **Event transports**: Redis Streams (`RedisTransport`), in-memory (`MemoryTransport`)
- **Celery signal integration**: Automatic event capture via `stemtrace.init_worker(app)`
- **Server components**:
  - `GraphStore` — Thread-safe in-memory graph storage with LRU eviction
  - `EventConsumer` / `AsyncEventConsumer` — Background event processing
  - `WebSocketManager` — Real-time event broadcasting
- **REST API**: `/api/tasks`, `/api/graphs`, `/api/health` endpoints
- **FastAPI integration**:
  - `StemtraceExtension` — Full extension with lifespan management
  - `create_router()` — Minimal router for custom setups
  - Auth helpers: `require_basic_auth`, `require_api_key`, `no_auth`
- **React UI**: Task list, graph visualization (react-flow), timeline view
- **CLI commands**: `stemtrace server`, `stemtrace consume`
- **Docker support**: Multi-stage Dockerfile, docker-compose.yml for local dev
- **E2E test suite**: Docker API tests + Playwright browser tests
- **Comprehensive test suite**: 350+ Python tests, 90%+ coverage

[unreleased]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.9...HEAD
[0.3.9]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/iansokolskyi/stemtrace/releases/tag/v0.1.0
