"""Append-only session health log (no cookie values)."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.linkedin.errors import ClassifiedResponse, ResponseClass

_lock = threading.Lock()
_ok_streak = 0
_ok_total = 0
_rejected_total = 0
_first_degraded_after: int | None = None


def session_fingerprint(li_at: str, jsessionid: str) -> str:
    material = f"{li_at.strip()}|{jsessionid.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _log_path() -> Path:
    raw = (settings.LINKEDIN_SESSION_HEALTH_LOG or "data/session_health.jsonl").strip()
    return Path(raw)


def snapshot_counters() -> dict[str, int | None]:
    with _lock:
        return {
            "ok_streak": _ok_streak,
            "ok_total": _ok_total,
            "rejected_total": _rejected_total,
            "first_degraded_after_ok": _first_degraded_after,
        }


def record_classified(
    *,
    li_at: str,
    jsessionid: str,
    endpoint: str,
    public_id: str,
    classified: ClassifiedResponse,
    used_as: str,
) -> dict:
    """Record one upstream call. Returns the JSON object written (no secrets)."""
    global _ok_streak, _ok_total, _rejected_total, _first_degraded_after
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        if classified.kind is ResponseClass.OK:
            _ok_streak += 1
            _ok_total += 1
        elif classified.kind is ResponseClass.SESSION_REJECTED:
            if _first_degraded_after is None and _ok_total > 0:
                _first_degraded_after = _ok_total
            _ok_streak = 0
            _rejected_total += 1

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session": session_fingerprint(li_at, jsessionid),
            "endpoint": endpoint,
            "public_id": public_id,
            "used_as": used_as,
            "kind": classified.kind.value,
            "reason": classified.reason,
            "status": classified.status,
            "location": classified.location,
            "body_bytes": classified.body_bytes,
            "ok_streak": _ok_streak,
            "ok_total": _ok_total,
            "rejected_total": _rejected_total,
            "first_degraded_after_ok": _first_degraded_after,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
