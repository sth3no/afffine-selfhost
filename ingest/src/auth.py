"""Bearer-token auth for the ingest HTTP API.

Single shared token (selfhost = single user). Compared with constant-time
equality to dodge token-leak side channels — overkill for personal selfhost
but cheap and correct.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from src.config import settings


async def _check_token(request: Request) -> str:
    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = auth.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be Bearer scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = parts[1].strip()
    expected = settings.ingest_api_token
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented


# Re-exportable Depends instance — use as `_: str = require_token` in routes.
require_token = Depends(_check_token)
