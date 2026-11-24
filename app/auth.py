"""API key authentication.

Keys are compared as SHA-256 digests in constant time. The key name (never
the key) is put on ``request.state`` so the access log can say who called.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.config import hash_key


def _presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def match_key(presented: str, api_keys: dict[str, str]) -> str | None:
    """Return the name of the matching key, or None. Checks every key."""
    digest = hash_key(presented)
    matched: str | None = None
    for name, stored in api_keys.items():
        if hmac.compare_digest(digest, stored):
            matched = name
    return matched


def require_api_key(request: Request) -> str:
    """FastAPI dependency. Returns the key name or raises 401."""
    presented = _presented_key(request)
    name = match_key(presented, request.app.state.settings.api_keys) if presented else None
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.api_key_name = name
    return name
