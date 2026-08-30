from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import Response

from app.config import settings
from app.linkedin.errors import ResponseClass
from app.linkedin.photo import fetch_linkedin_photo, is_linkedin_photo_url
from app.linkedin.public_id import PublicIdError
from app.linkedin.merge import merge_snapshots
from app.linkedin.normalizer import normalize_sdui_file, normalize_sdui_profile
from app.schemas.job import ProfileJobRequest
from app.schemas.profile import ProfileSnapshot
from app.services.cache import cache_get
from app.services.jobs import (
    JobCapacityError,
    fetch_live_profile_with_fallback,
    get_job,
    job_payload,
    primary_cookies,
    run_scrape_job,
    start_profile_job,
)

router = APIRouter()


def _capture_files(public_id: str) -> list[Path]:
    raw_dir = settings.LINKEDIN_CAPTURE_DIR.strip()
    if not raw_dir:
        return []
    folder = Path(raw_dir)
    names = (
        f"{public_id}.txt",
        f"{public_id}.html",
        f"{public_id}.rsc.txt",
        f"{public_id}-experience.txt",
        f"{public_id}-experience.html",
    )
    return [path for path in (folder / name for name in names) if path.is_file()]


def _from_captures(public_id: str) -> ProfileSnapshot | None:
    files = _capture_files(public_id)
    if not files:
        return None
    snapshots = [normalize_sdui_file(path) for path in files]
    merged = snapshots[0]
    if len(snapshots) > 1:
        merged = merge_snapshots(snapshots[0], *snapshots[1:])
    merged.notes = list(merged.notes) + [f"from_capture:{path.name}" for path in files]
    return merged


@router.get("/media/photo")
async def proxy_profile_photo(url: str = Query(..., min_length=8, max_length=2000)):
    if not is_linkedin_photo_url(url):
        raise HTTPException(status_code=400, detail={"code": "invalid_photo_url"})
    try:
        body, content_type = await fetch_linkedin_photo(url)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_photo_url"})
    except httpx.HTTPStatusError:
        raise HTTPException(status_code=502, detail={"code": "photo_fetch_failed"})
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail={"code": "photo_fetch_failed"})
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/profiles")
async def create_profile_job(
    body: ProfileJobRequest, background_tasks: BackgroundTasks
):
    try:
        job, launched = start_profile_job(body.linkedin_url_or_id.strip())
    except PublicIdError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_linkedin_id", "detail": str(exc)},
        ) from exc
    except JobCapacityError:
        raise HTTPException(
            status_code=429,
            detail={"code": "too_many_scrapes"},
        )
    if launched:
        background_tasks.add_task(run_scrape_job, job.job_id)
    return job_payload(job)


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})
    return job_payload(job)


@router.post("/profiles/normalize")
async def normalize_capture(request: Request):
    raw = (await request.body()).decode("utf-8", errors="replace")
    if not raw.strip():
        raise HTTPException(status_code=400, detail={"code": "empty_body"})
    return normalize_sdui_profile(raw)


@router.get("/profiles/{public_id}")
async def get_profile(public_id: str):
    captured = _from_captures(public_id)
    if captured is not None:
        return captured
    cached = cache_get(public_id)
    if cached is not None:
        return cached
    li_at, jsessionid = primary_cookies()
    if not li_at or not jsessionid:
        raise HTTPException(
            status_code=503,
            detail={"code": "cookies_missing"},
        )
    snapshot, classified = await fetch_live_profile_with_fallback(public_id)
    if snapshot is None:
        code = (
            "session_rejected"
            if classified.kind is ResponseClass.SESSION_REJECTED
            else "upstream_error"
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": code,
                "status": classified.status,
                "reason": classified.reason,
                "location": classified.location,
            },
        )
    return snapshot
