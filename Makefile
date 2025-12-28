.PHONY: install check types lint format test coverage clean ui-install ui-dev ui-build

# Install all dependencies
install:
	uv sync --all-extras

# Full verification suite - run after every change
check: types lint test docstrings ui-check
	@echo "✅ All checks passed"

# Individual checks
types:
	uv run mypy src/ --strict

# Check for missing docstrings on public APIs
docstrings:
	@uv run python -c " \
import ast; \
from pathlib import Path; \
missing = []; \
[missing.extend([f'{py}:{n.lineno} - {n.name}' for n in ast.walk(ast.parse(py.read_text())) \
  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
  and not (n.name.startswith('_') and not n.name.startswith('__')) \
  and not ast.get_docstring(n)]) \
for py in Path('src/celery_flow').rglob('*.py') if '__pycache__' not in str(py)]; \
[print(m) for m in missing]; \
exit(1) if missing else print('✓ All public APIs have docstrings') \
"

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

test:
	uv run pytest --cov=celery_flow --cov-report=term-missing --cov-fail-under=80

# Run tests without coverage (faster iteration)
test-fast:
	uv run pytest -x -q

# Run only unit tests
test-unit:
	uv run pytest tests/unit/ -v

# Run integration tests
test-integration:
	uv run pytest -m integration -v

# Show coverage report
coverage:
	uv run pytest --cov=celery_flow --cov-report=html
	@echo "Open htmlcov/index.html to view coverage report"

# Clean build artifacts
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# =============================================================================
# Frontend (React UI)
# =============================================================================
FRONTEND_DIR := src/celery_flow/server/ui/frontend

# Install frontend dependencies
ui-install:
	cd $(FRONTEND_DIR) && npm install

# Run frontend dev server (with HMR)
ui-dev:
	cd $(FRONTEND_DIR) && npm run dev

# Build frontend for production
ui-build:
	cd $(FRONTEND_DIR) && npm run build

# Lint and type check frontend (Biome + tsc)
ui-check:
	cd $(FRONTEND_DIR) && npm run check && npm run typecheck

# Auto-fix frontend lint issues
ui-fix:
	cd $(FRONTEND_DIR) && npm run fix

# =============================================================================
# Versioning (bump-my-version)
# =============================================================================
# Show current version
version:
	@uv run bump-my-version show current_version

# Dry run - show what would happen
bump-dry:
	uv run bump-my-version bump patch --dry-run --verbose

# Bump patch version (0.1.0 -> 0.1.1)
bump-patch:
	uv run bump-my-version bump patch

# Bump minor version (0.1.0 -> 0.2.0)
bump-minor:
	uv run bump-my-version bump minor

# Bump major version (0.1.0 -> 1.0.0)
bump-major:
	uv run bump-my-version bump major
