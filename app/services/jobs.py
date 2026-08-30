"""In-memory scrape jobs for the local test console."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.linkedin.client import fetch_live_profile
from app.linkedin.errors import ClassifiedResponse, ResponseClass
from app.linkedin.names import sanitize_full_name
from app.linkedin.public_id import PublicIdError, parse_linkedin_public_id
from app.schemas.profile import ProfileSnapshot
from app.services.cache import cache_get, cache_set

_EMPTY = (None, "", [])
_SKIP_MISSING = {"notes", "lazy_component_ids", "source", "missing_fields", "fetched_at"}
_gate = threading.Lock()
_INFLIGHT: dict[str, str] = {}

_EMPTY = (None, "", [])
_SKIP_MISSING = {"notes", "lazy_component_ids", "source", "missing_fields", "fetched_at"}


@dataclass
class ScrapeJob:
    job_id: str
    status: str
    linkedin_url_or_id: str
    public_id: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    cached: bool = False


_JOBS: dict[str, ScrapeJob] = {}


class JobCapacityError(Exception):
    """Too many scrapes already running in this process."""


def compute_missing_fields(data: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in ("full_name", "headline", "location"):
        if not data.get(key):
            missing.append(key)
    for key, value in data.items():
        if key in _SKIP_MISSING or key in {
            "full_name",
            "headline",
            "location",
            "experience",
        }:
            continue
        if value in _EMPTY:
            missing.append(key)
    experience = data.get("experience") or []
    if experience:
        incomplete_entries = 0
        for exp in experience:
            if isinstance(exp, dict):
                title = exp.get("title")
                company = exp.get("company")
            else:
                title = getattr(exp, "title", None)
                company = getattr(exp, "company", None)
            if not title or not company:
                incomplete_entries += 1
        if incomplete_entries > 0:
            missing.append(
                f"experience[{incomplete_entries}/{len(experience)}]_title_or_company"
            )
    return missing


def snapshot_to_result(snapshot: ProfileSnapshot) -> dict[str, Any]:
    data = snapshot.model_dump(mode="json")
    data["full_name"] = sanitize_full_name(data.get("full_name"))
    data["missing_fields"] = compute_missing_fields(data)
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return data


def create_job(linkedin_url_or_id: str) -> ScrapeJob:
    job = ScrapeJob(
        job_id=str(uuid.uuid4()),
        status="pending",
        linkedin_url_or_id=linkedin_url_or_id,
    )
    _JOBS[job.job_id] = job
    return job


def get_job(job_id: str) -> ScrapeJob | None:
    return _JOBS.get(job_id)


def reset_runtime_state() -> None:
    from app.services.cache import cache_clear

    with _gate:
        _JOBS.clear()
        _INFLIGHT.clear()
    cache_clear()


def _max_inflight() -> int:
    value = getattr(settings, "LINKEDIN_MAX_INFLIGHT_JOBS", 3)
    if not isinstance(value, (int, float, str)):
        return 3
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 3


def start_profile_job(linkedin_url_or_id: str) -> tuple[ScrapeJob, bool]:
    """Return (job, launched). launched means a new scrape should be scheduled."""
    public_id = parse_linkedin_public_id(linkedin_url_or_id)
    with _gate:
        hit = cache_get(public_id)
        if hit is not None:
            job = ScrapeJob(
                job_id=str(uuid.uuid4()),
                status="done",
                linkedin_url_or_id=linkedin_url_or_id.strip(),
                public_id=public_id,
                result=hit,
                cached=True,
            )
            _JOBS[job.job_id] = job
            return job, False
        existing_id = _INFLIGHT.get(public_id)
        if existing_id:
            existing = _JOBS.get(existing_id)
            if existing is not None and existing.status in ("pending", "running"):
                return existing, False
        running = sum(
            1 for item in _JOBS.values() if item.status in ("pending", "running")
        )
        if running >= _max_inflight():
            raise JobCapacityError()
        job = ScrapeJob(
            job_id=str(uuid.uuid4()),
            status="pending",
            linkedin_url_or_id=linkedin_url_or_id.strip(),
            public_id=public_id,
        )
        _JOBS[job.job_id] = job
        _INFLIGHT[public_id] = job.job_id
        return job, True


def _setting_str(name: str) -> str:
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


def primary_cookies() -> tuple[str, str]:
    return (
        _setting_str("LINKEDIN_LI_AT_PRIMARY") or _setting_str("LINKEDIN_LI_AT"),
        _setting_str("LINKEDIN_JSESSIONID_PRIMARY") or _setting_str("LINKEDIN_JSESSIONID"),
    )


def secondary_cookies() -> tuple[str, str] | None:
    li_at = _setting_str("LINKEDIN_LI_AT_SECONDARY")
    jsessionid = _setting_str("LINKEDIN_JSESSIONID_SECONDARY")
    if li_at and jsessionid:
        return li_at, jsessionid
    return None


async def fetch_live_profile_with_fallback(
    public_id: str,
) -> tuple[ProfileSnapshot | None, ClassifiedResponse]:
    """Try primary cookies; one retry on session-rejected if secondary is set."""
    li_at, jsessionid = primary_cookies()
    snapshot, classified = await fetch_live_profile(public_id, li_at, jsessionid)
    if snapshot is not None:
        return snapshot, classified
    extra = secondary_cookies()
    if extra is None or classified.kind is not ResponseClass.SESSION_REJECTED:
        return snapshot, classified
    return await fetch_live_profile(public_id, extra[0], extra[1])


def job_payload(job: ScrapeJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "linkedin_url_or_id": job.linkedin_url_or_id,
        "public_id": job.public_id,
        "created_at": job.created_at,
        "cached": job.cached,
        "result": job.result,
        "error": job.error,
    }


async def run_scrape_job(job_id: str) -> None:
    job = _JOBS.get(job_id)
    if job is None:
        return
    job.status = "running"
    try:
        public_id = parse_linkedin_public_id(job.linkedin_url_or_id)
        job.public_id = public_id
        li_at, jsessionid = primary_cookies()
        if not li_at or not jsessionid:
            job.status = "failed"
            job.error = {"code": "cookies_missing", "status": 503}
            return
        snapshot, classified = await fetch_live_profile_with_fallback(public_id)
        if snapshot is None:
            job.status = "failed"
            job.error = {
                "code": (
                    "session_rejected"
                    if classified.kind is ResponseClass.SESSION_REJECTED
                    else "upstream_error"
                ),
                "status": classified.status,
                "reason": classified.reason,
                "location": classified.location,
            }
            return
        job.result = snapshot_to_result(snapshot)
        job.status = "done"
        cache_set(public_id, job.result)
    except PublicIdError as exc:
        job.status = "failed"
        job.error = {"code": "invalid_linkedin_id", "status": 400, "detail": str(exc)}
    except Exception as exc:
        job.status = "failed"
        job.error = {
            "code": "internal_error",
            "status": 500,
            "detail": type(exc).__name__,
        }
    finally:
        pid = job.public_id
        if pid:
            with _gate:
                if _INFLIGHT.get(pid) == job_id:
                    _INFLIGHT.pop(pid, None)
