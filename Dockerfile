# stemtrace server Dockerfile
# Multi-stage build for smaller production image

# Stage 1: Build frontend
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY src/stemtrace/server/ui/frontend/package*.json ./
RUN npm ci

COPY src/stemtrace/server/ui/frontend/ ./
RUN npm run build

# Stage 2: Build Python package
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir hatch

# Copy source
COPY pyproject.toml README.md LICENSE build_ui.py ./
COPY src/ src/

# Copy pre-built frontend
COPY --from=frontend-builder /app/frontend/dist/ src/stemtrace/server/ui/frontend/dist/

# Build wheel
RUN hatch build -t wheel

# Stage 3: Production image
FROM python:3.12-slim AS production

WORKDIR /app

# Install the wheel
COPY --from=builder /app/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

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
