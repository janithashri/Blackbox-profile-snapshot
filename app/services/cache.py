"""In-memory profile result cache with a TTL. Process-local; gone on restart."""

from __future__ import annotations

import time
from typing import Any

from app.config import settings


def _ttl_seconds() -> float:
    value = getattr(settings, "LINKEDIN_CACHE_TTL_SECONDS", 300)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 300.0


_entries: dict[str, tuple[float, dict[str, Any]]] = {}


def cache_get(public_id: str) -> dict[str, Any] | None:
    ttl = _ttl_seconds()
    if ttl <= 0:
        return None
    item = _entries.get(public_id)
    if item is None:
        return None
    expires_at, payload = item
    if time.monotonic() >= expires_at:
        _entries.pop(public_id, None)
        return None
    return payload


def cache_set(public_id: str, payload: dict[str, Any]) -> None:
    ttl = _ttl_seconds()
    if ttl <= 0:
        return
    _entries[public_id] = (time.monotonic() + ttl, payload)


def cache_clear() -> None:
    _entries.clear()
