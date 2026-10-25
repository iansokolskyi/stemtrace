.PHONY: install check types lint format test coverage clean ui-install ui-dev ui-build e2e e2e-api e2e-playwright build release-check version bump-dry bump-patch bump-minor bump-major release release-pr release-tag

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
	uv run python scripts/check_docstrings.py

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/
	uv run lint-imports
	uv run python scripts/check_docstring_length.py

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

test:
	uv run pytest -n auto --maxprocesses 8 --dist worksteal --cov=stemtrace --cov-report=term-missing --cov-fail-under=95

# Run tests without coverage (faster iteration)
test-fast:
	uv run pytest -n auto --maxprocesses 8 --dist worksteal -x -q

# Run only unit tests
test-unit:
	uv run pytest tests/unit/ -v

# Pinned, hashed requirements for CI's test-lowest job: pyproject.toml's declared
# floors (lowest-direct) and the test tools constrained to them. --exclude-newer
# keeps the resolution reproducible; move the date forward to pick up newer
# transitive releases. CI runs lowest-pins-check, so edit floors here, not by hand.
LOWEST_EXCLUDE_NEWER := 2026-10-22T00:00:00Z
LOWEST_COMPILE := uv pip compile -q --python-version 3.10 --exclude-newer $(LOWEST_EXCLUDE_NEWER) \
	--generate-hashes --no-annotate --custom-compile-command "make lowest-pins"

.PHONY: lowest-pins lowest-pins-check
lowest-pins:
	$(LOWEST_COMPILE) --resolution lowest-direct pyproject.toml -o .github/lowest-requirements.txt
	$(LOWEST_COMPILE) -c .github/lowest-requirements.txt .github/lowest-test-tools.in -o .github/lowest-test-tools.txt

lowest-pins-check:
	$(LOWEST_COMPILE) --resolution lowest-direct pyproject.toml | diff .github/lowest-requirements.txt -
	$(LOWEST_COMPILE) -c .github/lowest-requirements.txt .github/lowest-test-tools.in | diff .github/lowest-test-tools.txt -
	@floor=$$(sed -n 's/^ *"bump-my-version>=\([0-9][0-9.]*\)[",].*/\1/p' pyproject.toml); \
	[ "$$(printf '%s\n' "$$floor" | grep -c .)" = 1 ] \
		|| { echo "pyproject.toml needs exactly one \"bump-my-version>=X.Y.Z\" line (an upper bound after a comma is fine); found: $$floor"; exit 1; }; \
	[ "$$(grep -cxF "bump-my-version==$$floor" .github/lowest-test-tools.in)" = 1 ] \
		|| { echo "pin bump-my-version==$$floor (the pyproject.toml floor) on one line of .github/lowest-test-tools.in, then run make lowest-pins"; exit 1; }

# Run integration tests
test-integration:
	uv run pytest -m integration -v

# Show coverage report
coverage:
	uv run pytest --cov=stemtrace --cov-report=html
	@echo "Open htmlcov/index.html to view coverage report"

# Clean build artifacts
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .import_linter_cache htmlcov .coverage dist/
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
	@echo "0. Checking the changelog has entries (fragments in changelog.d/ or [Unreleased] text)..."
	@uv run python scripts/assemble_changelog.py --check
	@test -n "$$(find changelog.d -type f ! -name README.md ! -name '.*' -print -quit 2>/dev/null)" \
		|| uv run python scripts/changelog_section.py Unreleased > /dev/null
	@echo "1. Running all checks..."
	$(MAKE) check
	@echo ""
	@echo "2. Building package..."
	$(MAKE) build
	@echo ""
	@echo "✅ Ready to release!"
	@echo ""

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
	echo "Note: releases use make release-pr, which runs the bump itself; this target is for manual use only"

# Bump minor version (0.1.0 -> 0.2.0)
bump-minor:
	uv run bump-my-version bump minor
	@NEW_VER=$$(uv run bump-my-version show current_version); \
	echo "✅ Version bumped to $$NEW_VER"; \
	echo ""; \
	echo "Note: releases use make release-pr, which runs the bump itself; this target is for manual use only"

# Bump major version (0.1.0 -> 1.0.0)
bump-major:
	uv run bump-my-version bump major
	@NEW_VER=$$(uv run bump-my-version show current_version); \
	echo "✅ Version bumped to $$NEW_VER"; \
	echo ""; \
	echo "Note: releases use make release-pr, which runs the bump itself; this target is for manual use only"

# Tag and push to trigger release workflow
release:
	@echo "Releases go through a PR: make release-pr BUMP=patch|minor|major, then make release-tag."
	@exit 1

# Open a release PR: preflight, checks, release/vX.Y.Z branch, changelog assembled from
# changelog.d/ fragments, version bump, PR with notes.
# Usage: make release-pr BUMP=patch|minor|major
release-pr:
	@set -e; \
	case "$(BUMP)" in patch|minor|major) ;; *) echo "Usage: make release-pr BUMP=patch|minor|major"; exit 1;; esac; \
	test "$$(git rev-parse --abbrev-ref HEAD)" = main || { echo "❌ Run from main"; exit 1; }; \
	test -z "$$(git status --porcelain)" || { echo "❌ Working tree not clean"; exit 1; }; \
	gh auth status >/dev/null 2>&1 || { echo "❌ gh is not authenticated (gh auth login)"; exit 1; }; \
	git fetch -q --tags origin main; \
	test -z "$$(git log HEAD..origin/main --oneline)" || { echo "❌ main is behind origin/main"; exit 1; }; \
	NEW=$$(uv run bump-my-version show new_version --increment $(BUMP)); \
	! git rev-parse -q --verify "refs/heads/release/v$$NEW" >/dev/null || { echo "❌ Local branch release/v$$NEW exists"; exit 1; }; \
	test -z "$$(git ls-remote --heads origin "release/v$$NEW")" || { echo "❌ Remote branch release/v$$NEW exists"; exit 1; }; \
	! git rev-parse -q --verify "refs/tags/v$$NEW" >/dev/null || { echo "❌ Tag v$$NEW exists"; exit 1; }; \
	$(MAKE) release-check; \
	git switch -c "release/v$$NEW"; \
	uv run python scripts/assemble_changelog.py; \
	git add -A CHANGELOG.md changelog.d; \
	git diff --cached --quiet || git commit -q -m "docs: assemble changelog for v$$NEW"; \
	uv run bump-my-version bump $(BUMP); \
	NOTES=$$(mktemp); trap 'rm -f "$$NOTES"' EXIT; \
	uv run python scripts/changelog_section.py "$$NEW" > "$$NOTES"; \
	git push -u origin "release/v$$NEW" || { echo "❌ Push failed; local branch release/v$$NEW holds the bump commit"; exit 1; }; \
	gh pr create --base main --head "release/v$$NEW" --title "chore: release v$$NEW" --body-file "$$NOTES" \
		|| { echo "❌ PR creation failed; release/v$$NEW is pushed, open the PR manually"; exit 1; }; \
	echo ""; \
	echo "✅ Release PR opened for v$$NEW"; \
	echo "Next: gh pr checks --watch, merge with a merge commit, then make release-tag"

# Tag the merged release on main; the tag push triggers the Release workflow.
release-tag:
	@set -e; \
	git switch -q main; \
	git pull -q --ff-only origin main; \
	VERSION=$$(uv run bump-my-version show current_version); \
	TAG="v$$VERSION"; \
	SUBJECT=$$(git log -1 --format=%s); \
	test "$$(git rev-list --parents -n 1 HEAD | wc -w)" -eq 3 || { echo "❌ HEAD of main is not a merge commit (merge the release PR with a merge commit)"; exit 1; }; \
	case "$$SUBJECT" in *"/release/$$TAG"|*"release $$TAG"|*"release $$TAG "*) ;; *) echo "❌ HEAD of main is not the $$TAG release PR merge: $$SUBJECT"; exit 1;; esac; \
	git fetch -q --tags origin; \
	! git rev-parse -q --verify "refs/tags/$$TAG" >/dev/null || { echo "❌ $$TAG already exists"; exit 1; }; \
	git tag -a "$$TAG" -m "Release $$TAG"; \
	git push origin "$$TAG"; \
	echo "✅ Tagged $$TAG → Release workflow publishes PyPI, GHCR and the GitHub Release"

# =============================================================================
# E2E Testing
# =============================================================================

# Run Playwright E2E tests in mock mode (no Docker required)
# This is the default and recommended way to run E2E tests locally
e2e-mock:
	cd $(FRONTEND_DIR) && npm test

# Start E2E test environment (Docker)
e2e-up:
	docker compose -f docker-compose.e2e.yml build
	docker compose -f docker-compose.e2e.yml up -d --wait
	@echo "Waiting for services..."
	@uv run python scripts/wait_for_http.py http://localhost:8000/stemtrace/api/health --timeout 60 --expect-json status=ok --expect-json consumer_connected=true
	@echo "✅ E2E environment ready at http://localhost:8000"

# Stop E2E test environment
e2e-down:
	docker compose -f docker-compose.e2e.yml down -v

# Run API E2E tests (requires e2e-up first)
e2e-api:
	uv run pytest tests/e2e/ -m e2e -v

# Seed the stack with the tasks the Playwright specs need and wait for them
# (requires e2e-up first; tests/e2e/seed.py owns the list). Also run by
# e2e-playwright-real; the CI Playwright job runs the same script.
e2e-seed:
	uv run python -m tests.e2e.seed

# Run Playwright E2E tests against real Docker backend (requires e2e-up).
# Seeds first: the real-mode specs need that data, so no entry point skips it.
e2e-playwright-real: e2e-seed
	cd $(FRONTEND_DIR) && E2E_MODE=real PLAYWRIGHT_BASE_URL=http://localhost:8000 npm test

# Run all E2E tests against Docker (full integration)
e2e:
	$(MAKE) e2e-up
	$(MAKE) e2e-api && $(MAKE) e2e-playwright-real; \
	status=$$?; \
	$(MAKE) e2e-down; \
	exit $$status

# =============================================================================
# E2E Testing (RabbitMQ broker)
# =============================================================================

e2e-up-rabbitmq:
	docker compose -f docker-compose.e2e.rabbitmq.yml build
	docker compose -f docker-compose.e2e.rabbitmq.yml up -d --wait
	@echo "Waiting for services..."
	@uv run python scripts/wait_for_http.py http://localhost:8000/stemtrace/api/health --timeout 60 --expect-json status=ok --expect-json consumer_connected=true
	@echo "✅ RabbitMQ E2E environment ready at http://localhost:8000"

e2e-down-rabbitmq:
	docker compose -f docker-compose.e2e.rabbitmq.yml down -v

e2e-api-rabbitmq:
	CELERY_BROKER_URL=amqp://guest:guest@localhost:5672// \
	CELERY_RESULT_BACKEND=redis://localhost:16380/1 \
	uv run pytest tests/e2e/ -m e2e -v

e2e-seed-rabbitmq:
	CELERY_BROKER_URL=amqp://guest:guest@localhost:5672// \
	CELERY_RESULT_BACKEND=redis://localhost:16380/1 \
	uv run python -m tests.e2e.seed

e2e-playwright-real-rabbitmq: e2e-seed-rabbitmq
	cd $(FRONTEND_DIR) && E2E_MODE=real PLAYWRIGHT_BASE_URL=http://localhost:8000 npm test

e2e-rabbitmq:
	$(MAKE) e2e-up-rabbitmq
	$(MAKE) e2e-api-rabbitmq && $(MAKE) e2e-playwright-real-rabbitmq; \
	status=$$?; \
	$(MAKE) e2e-down-rabbitmq; \
	exit $$status

# Quick E2E alias (mock mode, no Docker)
e2e-quick: e2e-mock
