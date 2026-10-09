# stemtrace server Dockerfile
# Multi-stage build for smaller production image

# Stage 1: Build frontend
FROM node:22.23.3-alpine3.24@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend-builder

WORKDIR /app/frontend
COPY src/stemtrace/server/ui/frontend/package*.json ./
RUN npm ci --ignore-scripts

COPY src/stemtrace/server/ui/frontend/ ./
RUN npm run build

# Stage 2: Build a virtualenv with the locked dependencies and stemtrace itself
FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_CACHE=1

WORKDIR /app

# Install locked runtime dependencies from wheels only (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-build --no-install-project && rm -f /opt/venv/.lock

# Copy source
COPY README.md LICENSE build_ui.py ./
COPY src/ src/

# Copy pre-built frontend
COPY --from=frontend-builder /app/frontend/dist/ src/stemtrace/server/ui/frontend/dist/

# Build and install stemtrace itself (non-editable) into the virtualenv
RUN uv sync --locked --no-build --no-editable && rm -f /opt/venv/.lock

# Stage 3: Production image
FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS production

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN useradd -m -u 1000 stemtrace
USER stemtrace

# Default environment
ENV STEMTRACE_BROKER_URL=""
ENV HOST="0.0.0.0"
ENV PORT="8000"

EXPOSE 8000

# Health check (CLI server mounts API at /stemtrace prefix)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/stemtrace/api/health').raise_for_status()"

# Default command: run server
ENTRYPOINT ["stemtrace"]
CMD ["server", "--host", "0.0.0.0", "--port", "8000"]
