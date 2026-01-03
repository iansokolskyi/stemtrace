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

# Install pre-commit hooks
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
make lint       # ruff check + format
make types      # mypy --strict
make test       # pytest with coverage
```

### Code Style

We use [ruff](https://github.com/astral-sh/ruff) for linting and formatting:

```bash
# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Format code
uv run ruff format src/ tests/
```

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

# All tests
uv run pytest

# With coverage
uv run pytest --cov=stemtrace --cov-report=term-missing
```

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
- **library/** - Celery integration, imports only from core/
- **server/** - Web server, imports only from core/
- Never import sideways (library ↔ server)

### Code Quality

- Google-style docstrings on all public APIs
- Pydantic models: immutable with `model_config = ConfigDict(frozen=True)`, mutable with `validate_assignment=True`
- Fire-and-forget pattern for publishers (never block)
- Fakes over mocks in tests

## Questions?

- Open an issue for bugs or feature requests
- Start a discussion for questions

Thank you for contributing! 🙏
