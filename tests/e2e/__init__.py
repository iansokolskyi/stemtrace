"""End-to-end tests - full system with Docker.

E2E tests require running services (Redis, Celery worker, celery-flow server).
Use docker-compose.e2e.yml to start the test environment.

Run E2E tests:
    docker compose -f docker-compose.e2e.yml up -d --wait
    pytest tests/e2e/ -m e2e
    docker compose -f docker-compose.e2e.yml down
"""
