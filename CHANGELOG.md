# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[unreleased]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/iansokolskyi/stemtrace/releases/tag/v0.1.0
