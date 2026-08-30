"""Classify LinkedIn flagship/HTML responses (not Voyager)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import httpx

THIN_BODY_BYTES = 5000


def _looks_like_profile_payload(content: bytes) -> bool:
    if b"__como_rehydration__" in content:
        return True
    if b"profileId" in content and b"ACoAA" in content:
        return True
    if b"pagers.profile.details" in content:
        return True
    if b"proto.sdui.actions.requests.PaginationRequest" in content:
        return True
    if b"NavigateToScreen" in content and b"profile_view_base" in content:
        return True
    return False


class ResponseClass(str, Enum):
    OK = "ok"
    SESSION_REJECTED = "session_rejected"
    UPSTREAM_ERROR = "upstream_error"


@dataclass(frozen=True)
class ClassifiedResponse:
    kind: ResponseClass
    status: int
    location: str
    reason: str
    body_bytes: int


def _location(response: httpx.Response) -> str:
    return (response.headers.get("location") or "").strip()


def _is_login_redirect(status: int, location: str, url: str) -> bool:
    if status not in (301, 302, 303, 307, 308):
        return False
    haystack = f"{location} {url}".lower()
    return "login" in haystack or "/uas/login" in haystack


def classify_response(response: httpx.Response) -> ClassifiedResponse:
    """Map live flagship POST / profile HTML GET / experience GET outcomes."""
    status = response.status_code
    location = _location(response)
    url = str(response.url)
    body_bytes = len(response.content or b"")

    if status == 999:
        return ClassifiedResponse(
            ResponseClass.SESSION_REJECTED,
            status,
            location,
            "linkedin_999",
            body_bytes,
        )
    if status in (401, 403):
        return ClassifiedResponse(
            ResponseClass.SESSION_REJECTED,
            status,
            location,
            "http_forbidden",
            body_bytes,
        )
    if _is_login_redirect(status, location, url):
        return ClassifiedResponse(
            ResponseClass.SESSION_REJECTED,
            status,
            location,
            "login_redirect",
            body_bytes,
        )
    if status == 200:
        content = response.content or b""
        if len(content) >= THIN_BODY_BYTES or _looks_like_profile_payload(content):
            return ClassifiedResponse(
                ResponseClass.OK,
                status,
                location,
                "ok",
                body_bytes,
            )
        return ClassifiedResponse(
            ResponseClass.SESSION_REJECTED,
            status,
            location,
            "thin_or_challenge_body",
            body_bytes,
        )
    return ClassifiedResponse(
        ResponseClass.UPSTREAM_ERROR,
        status,
        location,
        "upstream_error",
        body_bytes,
    )


def classify_pager_response(response: httpx.Response) -> ClassifiedResponse:
    """Pagination RSC can be a short empty-state page; do not treat that as a session fail."""
    classified = classify_response(response)
    if (
        response.status_code == 200
        and classified.kind is ResponseClass.SESSION_REJECTED
        and classified.reason == "thin_or_challenge_body"
    ):
        return ClassifiedResponse(
            ResponseClass.OK,
            classified.status,
            classified.location,
            "ok_thin_pager",
            classified.body_bytes,
        )
    return classified


def is_session_rejected(response: httpx.Response) -> bool:
    return classify_response(response).kind is ResponseClass.SESSION_REJECTED
