"""
auth.py — API key authentication middleware for FastAPI.
"""

from fastapi import Request, HTTPException, Depends
from fastapi.security import APIKeyHeader
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from database import get_api_key_info, get_usage, check_tier_limits

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def authenticate(request: Request) -> dict:
    """Validate API key and return key info. Raises HTTPException on failure."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    key_info = await get_api_key_info(api_key)
    if not key_info:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key",
        )

    return key_info


async def require_plan(min_tier: str = "starter") -> dict:
    """Dependency that authenticates and checks minimum tier.

    Usage:
        @app.post("/api/v1/remember")
        async def remember(key_info: dict = Depends(require_plan("pro")):
            ...
    """
    async def inner(request: Request) -> dict:
        key_info = await authenticate(request)
        tier = key_info["tier"]
        tiers = ["starter", "pro", "team"]
        min_idx = tiers.index(min_tier) if min_tier in tiers else 0
        current_idx = tiers.index(tier) if tier in tiers else 0
        if current_idx < min_idx:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires at least the {min_tier} plan",
            )
        return key_info
    return inner


async def check_usage_limits(key_info: dict) -> None:
    """Check if the API key has exceeded its tier limits."""
    tier = key_info["tier"]
    usage = await get_usage(key_info["key"])
    allowed, error = check_tier_limits(tier, usage)
    if not allowed:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail=error,
        )
