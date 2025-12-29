"""Authentication helpers for stemtrace API."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials

_basic_auth = HTTPBasic(auto_error=False)


def require_basic_auth(username: str, password: str) -> Any:
    """Create dependency requiring HTTP Basic auth."""

    def _verify(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_auth)],
    ) -> str:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )

        username_ok = secrets.compare_digest(
            credentials.username.encode("utf-8"),
            username.encode("utf-8"),
        )
        password_ok = secrets.compare_digest(
            credentials.password.encode("utf-8"),
            password.encode("utf-8"),
        )

        if not (username_ok and password_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

        return credentials.username

    return Depends(_verify)


def require_api_key(api_key: str, *, header_name: str = "X-API-Key") -> Any:
    """Create dependency requiring API key in header."""
    key_header = APIKeyHeader(name=header_name, auto_error=False)

    async def _verify(
        provided_key: str | None = Depends(key_header),
    ) -> str:
        if provided_key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required",
            )

        if not secrets.compare_digest(
            provided_key.encode("utf-8"),
            api_key.encode("utf-8"),
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

        return provided_key

    return Depends(_verify)


def no_auth() -> Any:
    """Dependency that allows all requests."""
    return Depends(lambda: None)
