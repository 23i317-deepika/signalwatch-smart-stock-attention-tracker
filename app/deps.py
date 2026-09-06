"""
Small, reusable FastAPI dependencies shared across routers.

Device identification is deliberately NOT authentication: the frontend
generates a UUID once and stores it in localStorage (see PROJECT_PLAN.md
§8/§9). This dependency only validates that a device id was actually sent —
it never verifies who's behind it.
"""

from fastapi import Header, HTTPException, status


def get_device_id(
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
) -> str:
    """Extract and validate the X-Device-ID header.

    Rejects a missing or blank header with a 400 — every other route that
    depends on this can assume `device_id` is a non-empty, trimmed string.
    """
    if x_device_id is None or not x_device_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-ID header is required",
        )
    return x_device_id.strip()
