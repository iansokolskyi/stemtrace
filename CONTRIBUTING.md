# Contributing to stemtrace

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (required)
- Redis (for integration tests)
- Docker (for e2e tests)

### Quick Start

```bash
# Clone the repo
git clone https://github.com/iansokolskyi/stemtrace.git
cd stemtrace

# Install dependencies (creates venv automatically)
uv sync --extra dev

# Install pre-commit hooks (the dev extra skips pre-commit on Python 3.10; there, run
# `uvx pre-commit install` instead)
uv run pre-commit install

# Run checks
make check
```

## Development Workflow

### Running Checks

```bash
# All checks (lint, type check, test)
make check

# Individual checks
make lint       # ruff check + format + import contracts
make types      # mypy --strict
make test       # pytest with coverage, in parallel (pytest-xdist)
```

### Code Style

We use [ruff](https://github.com/astral-sh/ruff) for linting and formatting:

```bash
# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Format code
uv run ruff format src/ tests/
```

### Lint Rules and Import Contracts

Besides style, ruff enforces design limits: McCabe complexity 10 (`C90`), at most
12 branches, 6 returns, 50 statements and 5 positional arguments per function
(`PLR09xx`; make extra parameters keyword-only), no blind `except Exception`
(`BLE`), no commented-out code (`ERA`), plus `RET` and `PT` style rules. A broad
except is accepted only at a fire-and-forget boundary, with
`# noqa: BLE001  <reason>` on that line.

[import-linter](https://import-linter.readthedocs.io/) checks the architecture
boundaries (`uv run lint-imports`, part of `make lint` and CI):

- `stemtrace.core` imports nothing from `stemtrace.library`, `stemtrace.server` or
  `stemtrace.transports`, and no broker, Celery or web library (`celery`, `kombu`,
  `amqp`, `redis`, `fastapi`, `starlette`, `uvicorn`, `typer`, `httpx`)
- `stemtrace.transports` imports nothing from `stemtrace.library` or
  `stemtrace.server` (both of them use it)
- `stemtrace.library` does not import `stemtrace.server`
- `stemtrace.server` does not import `stemtrace.library`

**Baseline policy.** Code that predates these rules carries a per-line
`# noqa: <codes>` comment with no reason (added with `ruff check --add-noqa`).
Known import violations, if any, are listed under `ignore_imports` in
`pyproject.toml` (there are none at the moment). The baseline only shrinks:

- New code follows the rules. If a suppression is really needed, it names the
  code and gives a reason: `# noqa: BLE001  fire-and-forget: <why>`. Never add a
  bare `# noqa`.
- When you fix a baselined line, delete its `# noqa`. Ruff's `RUF100` fails on a
  `# noqa` that no longer suppresses anything, and import-linter fails on an
  `ignore_imports` entry that no longer matches, so stale entries can't linger.

### Type Checking

All code must pass `mypy --strict`:

```bash
uv run mypy src/ --strict
```

- Use type hints on all functions and methods
- Avoid `Any` unless absolutely necessary
- No `# type: ignore` without explanation

### Testing

```bash
# Unit tests only (fast)
uv run pytest tests/unit/

# All tests (add `-n auto --dist worksteal` to run in parallel, as `make test` does)
uv run pytest

# With coverage
uv run pytest --cov=stemtrace --cov-report=term-missing
```

Parallel runs give each xdist worker its own Redis database (`REDIS_URL`'s database minus the
worker number), so use at most 16 workers against a default Redis (`make test` caps at 8). The run stops with a usage error if `REDIS_URL` has too low a database number for the worker count.
A worker that dies before it finishes its session stops the run with a failing exit code instead of being replaced, because a replacement would need another database. To allow restarts, pass `--max-worker-restart N` and leave room for N more databases.

**Coverage requirement:** 95% minimum

### Updating Dependencies

```bash
# Update lock file after changing pyproject.toml
uv lock

# Upgrade all dependencies to latest
uv lock --upgrade
```

## Pull Request Process

1. **Fork & Branch**
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Make Changes**
   - Follow the code style guidelines
   - Add tests for new functionality
   - Update documentation if needed
   - For a user-visible change, add a changelog fragment in `changelog.d/` instead of editing
     `CHANGELOG.md` (see [`changelog.d/README.md`](changelog.d/README.md))

3. **Run Checks**
   ```bash
   make check  # Must pass before submitting
   ```

4. **Commit**
   - Use [conventional commits](https://www.conventionalcommits.org/):
     - `feat:` New features
     - `fix:` Bug fixes
     - `docs:` Documentation changes
     - `refactor:` Code refactoring
     - `test:` Test changes
     - `chore:` Build/tooling changes

5. **Push & Create PR**
   - Fill out the PR template
   - Link related issues
   - Wait for CI to pass

### Key Principles

- **core/** - Pure Python, no external dependencies
- **transports/** - Broker adapters (Redis, RabbitMQ, memory), imports only from core/
- **library/** - Celery integration, imports only from core/ and transports/
- **server/** - Web server, imports only from core/ and transports/
- Never import sideways (library ↔ server)

### Code Quality

- Google-style docstrings on all public APIs
- Pydantic models: immutable with `model_config = ConfigDict(frozen=True)`, mutable with `validate_assignment=True`
- Fire-and-forget pattern for publishers (never block)
- Fakes over mocks in tests

## Releasing (maintainers)

Every release goes through a pull request into `main` and is published by CI from a tag on `main`.

1. Check that the changelog fragments in `changelog.d/` cover the release. Put any
   **Highlights:** block under `## [Unreleased]` in `CHANGELOG.md`.
2. `make release-pr BUMP=patch|minor|major` runs the checks, creates `release/vX.Y.Z`, merges the
   fragments into `## [Unreleased]` (and deletes them), bumps the version and opens a PR whose
   description is the changelog section.
3. When CI is green, merge the PR with a merge commit (not squash or rebase).
4. `make release-tag` tags the merge on `main`. The tag triggers the Release workflow, which
   publishes to PyPI and GHCR and creates the GitHub Release from the changelog section. It
   refuses to publish if the section is empty or the tag isn't on `main`.

## Questions?

- Open an issue for bugs or feature requests
- Start a discussion for questions

Thank you for contributing! 🙏
