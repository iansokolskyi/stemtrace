# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[unreleased]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/iansokolskyi/stemtrace/releases/tag/v0.1.0
