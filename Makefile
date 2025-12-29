.PHONY: install check types lint format test coverage clean ui-install ui-dev ui-build e2e e2e-api e2e-playwright build release-check version bump-dry bump-patch bump-minor bump-major release

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
for py in Path('src/stemtrace').rglob('*.py') if '__pycache__' not in str(py)]; \
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
	uv run pytest --cov=stemtrace --cov-report=term-missing --cov-fail-under=80

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
	uv run pytest --cov=stemtrace --cov-report=html
	@echo "Open htmlcov/index.html to view coverage report"

# Clean build artifacts
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# =============================================================================
# Frontend (React UI)
# =============================================================================
FRONTEND_DIR := src/stemtrace/server/ui/frontend

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
# Build & Release
# =============================================================================
# Build package locally (verify before release)
build:
	rm -rf dist/
	uv run python -m build
	uv run twine check dist/*
	@echo "✅ Build successful. Files in dist/"
	@ls -la dist/

# Full pre-release checklist
release-check:
	@echo "=== Pre-release Checklist ==="
	@echo "1. Running all checks..."
	$(MAKE) check
	@echo ""
	@echo "2. Building package..."
	$(MAKE) build
	@echo ""
	@echo "✅ Ready to release!"
	@echo ""
	@echo "Next: make release"

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
	@NEW_VER=$$(uv run bump-my-version show current_version); \
	echo "✅ Version bumped to $$NEW_VER"; \
	echo ""; \
	echo "Next: git add -A && git commit -m 'chore: release v$$NEW_VER' && make release"

# Bump minor version (0.1.0 -> 0.2.0)
bump-minor:
	uv run bump-my-version bump minor
	@NEW_VER=$$(uv run bump-my-version show current_version); \
	echo "✅ Version bumped to $$NEW_VER"; \
	echo ""; \
	echo "Next: git add -A && git commit -m 'chore: release v$$NEW_VER' && make release"

# Bump major version (0.1.0 -> 1.0.0)
bump-major:
	uv run bump-my-version bump major
	@NEW_VER=$$(uv run bump-my-version show current_version); \
	echo "✅ Version bumped to $$NEW_VER"; \
	echo ""; \
	echo "Next: git add -A && git commit -m 'chore: release v$$NEW_VER' && make release"

# Tag and push to trigger release workflow
release:
	@VERSION=$$(uv run bump-my-version show current_version); \
	TAG="v$$VERSION"; \
	echo "Tagging $$TAG..."; \
	git tag -a "$$TAG" -m "Release $$TAG"; \
	echo "Pushing to origin..."; \
	git push origin main; \
	git push origin "$$TAG"; \
	echo ""; \
	echo "✅ Released $$TAG"; \
	echo "   → GitHub Actions will publish to PyPI and Docker"

# =============================================================================
# E2E Testing
# =============================================================================

# Start E2E test environment
e2e-up:
	docker compose -f docker-compose.e2e.yml up -d --wait
	@echo "Waiting for services..."
	@timeout 60 bash -c 'until curl -sf http://localhost:8000/stemtrace/api/health -o /dev/null; do sleep 2; done' || true
	@echo "✅ E2E environment ready at http://localhost:8000"

# Stop E2E test environment
e2e-down:
	docker compose -f docker-compose.e2e.yml down -v

# Run API E2E tests (requires e2e-up first)
e2e-api:
	uv run pytest tests/e2e/ -m e2e -v

# Run Playwright E2E tests (requires e2e-up first)
e2e-playwright:
	cd $(FRONTEND_DIR) && npm test

# Run all E2E tests (ensures cleanup on failure)
e2e:
	$(MAKE) e2e-up
	$(MAKE) e2e-api && $(MAKE) e2e-playwright; \
	status=$$?; \
	$(MAKE) e2e-down; \
	exit $$status
