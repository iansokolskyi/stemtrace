# stemtrace server Dockerfile
# Multi-stage build for smaller production image

# Stage 1: Build frontend
# The UI build output is static files, identical for every platform, so build it
# once on the native build platform instead of under QEMU (npm ci hung there).
FROM --platform=$BUILDPLATFORM node:22.23.4-alpine3.24@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS frontend-builder

WORKDIR /app/frontend
COPY src/stemtrace/server/ui/frontend/package*.json ./
RUN npm ci --ignore-scripts

COPY src/stemtrace/server/ui/frontend/ ./
RUN npm run build

# Stage 2: Build a virtualenv with the locked dependencies and stemtrace itself
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

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
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS production

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Drop the base image's system pip: stemtrace runs from /opt/venv and never
# uses it, and it would otherwise ship known advisories. `python -m ensurepip`
# still works for derived images (see CHANGELOG).
RUN /usr/local/bin/python -m pip uninstall --yes --quiet pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

# Create non-root user
RUN useradd -m -u 1000 stemtrace
USER stemtrace

# Default environment
ENV STEMTRACE_BROKER_URL=""
ENV HOST="0.0.0.0"
ENV PORT="8000"

EXPOSE 8000

# Health check (CLI server mounts API at /stemtrace prefix). The liveness
# endpoint needs no login session and answers 200 while the process serves.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/stemtrace/api/health/live').raise_for_status()"

# Default command: run server
ENTRYPOINT ["stemtrace"]
CMD ["server", "--host", "0.0.0.0", "--port", "8000"]
