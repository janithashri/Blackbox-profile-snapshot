"""LinkedIn profile HTTP client (flagship-web SDUI + HTML GET)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.linkedin.errors import ClassifiedResponse, ResponseClass, classify_pager_response, classify_response
from app.linkedin.merge import dedupe_snapshot, merge_snapshots
from app.linkedin.session_health import record_classified
from app.schemas.profile import ProfileSnapshot

FLAGSHIP_PROFILE_URL = "https://www.linkedin.com/flagship-web/in/{public_id}/"
FLAGSHIP_DETAILS_URL = (
    "https://www.linkedin.com/flagship-web/in/{public_id}/details/{slug}/"
)
PAGINATION_URLS = (
    "https://www.linkedin.com/rsc-action/actions/pagination/",
    "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination/",
)
PROFILE_HTML_URL = "https://www.linkedin.com/in/{public_id}/"
EXPERIENCE_HTML_URL = "https://www.linkedin.com/in/{public_id}/details/experience/"
FLAGSHIP_PAGER_URL = (
    "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
)
FLAGSHIP_COMPONENT_URL = (
    "https://www.linkedin.com/flagship-web/rsc-action/actions/component"
)
ABOVE_ACTIVITY_COMPONENT_ID = (
    "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsAboveActivity"
)
EDUCATION_PAGER_ID = "com.linkedin.sdui.pagers.profile.details.education"

DESKTOP_FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"
)

SDUI_VERSION = "0.1.51189"
APPLICATION_VERSION = "0.2.7003"
PROFILE_PAGE_KEY = "d_flagship3_profile_view_base"
PROFILE_SCREEN_ID = "com.linkedin.sdui.flagshipnav.profile.Profile"
_MAX_PAGER_PAGES = 3
_MAX_SKILL_PAGES = 5
_PAGER_PAGE_SIZE = 10


@dataclass(frozen=True)
class FlagshipDetailSection:
    slug: str
    screen_id: str
    page_key: str
    dump_kind: str
    pager_id: str


# Pager IDs from profile-details first-paint nextPageRequest.
FLAGSHIP_DETAIL_SECTIONS = (
    FlagshipDetailSection(
        "education",
        "com.linkedin.sdui.flagshipnav.profile.ProfileEducationDetails",
        "profile_view_base_education_details",
        "education_raw",
        "com.linkedin.sdui.pagers.profile.details.education",
    ),
    FlagshipDetailSection(
        "skills",
        "com.linkedin.sdui.flagshipnav.profile.ProfileSkillDetails",
        "profile_view_base_skills_details",
        "skills_raw",
        "com.linkedin.sdui.pagers.profile.details.skills",
    ),
    FlagshipDetailSection(
        "certifications",
        "com.linkedin.sdui.flagshipnav.profile.ProfileCertificationDetails",
        "profile_view_base_certifications_details",
        "certifications_raw",
        "com.linkedin.sdui.pagers.profile.details.certifications",
    ),
    FlagshipDetailSection(
        "languages",
        "com.linkedin.sdui.flagshipnav.profile.ProfileLanguageDetails",
        "profile_view_base_languages_details",
        "languages_raw",
        "com.linkedin.sdui.pagers.profile.details.languages",
    ),
)

_last_linkedin_monotonic = 0.0
_throttle_lock = asyncio.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _debug_dump_raw(kind: str, public_id: str, response: httpx.Response) -> None:
    """Local diagnosis only. Off in production so Railway does not fill disk with RSC dumps."""
    if (settings.ENVIRONMENT or "").strip().lower() not in ("development", "dev", "local"):
        return
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in public_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    folder = _PROJECT_ROOT / "data" / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}_{safe_id}_{stamp}.txt"
    path.write_bytes(response.content or b"")
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in public_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    folder = _PROJECT_ROOT / "data" / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}_{safe_id}_{stamp}.txt"
    path.write_bytes(response.content or b"")


def _jsessionid_token(jsessionid: str) -> str:
    return jsessionid.strip().strip('"')


def _cookies(li_at: str, jsessionid: str) -> dict[str, str]:
    token = _jsessionid_token(jsessionid)
    return {
        "li_at": li_at.strip().strip('"'),
        "JSESSIONID": f'"{token}"',
    }


def _profile_request_body(public_id: str) -> dict:
    profile_path = f"/in/{public_id}/"
    requested_arguments = {
        "payload": {
            "vanityName": public_id,
            "isVanityNameResolved": True,
        },
        "states": [],
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
        "screenId": "",
        "knownTemplateIds": [],
    }
    return {
        "$type": "proto.sdui.actions.core.NavigateToScreen",
        "screenId": PROFILE_SCREEN_ID,
        "pageKey": "profile_view_base",
        "presentationStyle": "PresentationStyle_FULL_PAGE",
        "presentation": {
            "$case": "fullPage",
            "fullPage": {
                "$type": "proto.sdui.actions.core.presentation.FullPagePresentation"
            },
        },
        "title": "",
        "url": profile_path,
        "inheritActor": False,
        "colorScheme": "ColorScheme_UNKNOWN",
        "disableScreenGutters": False,
        "shouldHideMobileTopNavBar": False,
        "shouldHideLoadingSpinner": False,
        "replaceCurrentScreen": False,
        "shouldHideMobileTopNavBarDivider": False,
        "clearBackStack": False,
        "newHierarchy": {
            "$type": "proto.sdui.navigation.ScreenHierarchy",
            "screenHash": "com.linkedin.sdui.flagshipnav.home.Home#0",
            "screenId": "com.linkedin.sdui.flagshipnav.home.Home",
            "pageKey": "",
            "isAnchorPage": True,
            "url": "",
            "childHierarchy": {
                "$type": "proto.sdui.navigation.ScreenHierarchy",
                "screenHash": f"{PROFILE_SCREEN_ID}#9c4a7cde",
                "screenId": PROFILE_SCREEN_ID,
                "pageKey": "",
                "isAnchorPage": True,
                "url": "",
            },
        },
        "screenTitle": [],
        "requestedArguments": requested_arguments,
    }


def _details_request_body(public_id: str, section: FlagshipDetailSection) -> dict:
    details_path = f"/in/{public_id}/details/{section.slug}/"
    requested_arguments = {
        "payload": {"vanityName": public_id},
        "states": [],
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
        "screenId": "",
        "knownTemplateIds": [],
    }
    return {
        "$type": "proto.sdui.actions.core.NavigateToScreen",
        "screenId": section.screen_id,
        "pageKey": section.page_key,
        "presentationStyle": "PresentationStyle_FULL_PAGE",
        "presentation": {
            "$case": "fullPage",
            "fullPage": {
                "$type": "proto.sdui.actions.core.presentation.FullPagePresentation"
            },
        },
        "title": "",
        "url": details_path,
        "inheritActor": False,
        "colorScheme": "ColorScheme_UNKNOWN",
        "disableScreenGutters": False,
        "shouldHideMobileTopNavBar": False,
        "shouldHideLoadingSpinner": False,
        "replaceCurrentScreen": False,
        "shouldHideMobileTopNavBarDivider": False,
        "clearBackStack": False,
        "newHierarchy": {
            "$type": "proto.sdui.navigation.ScreenHierarchy",
            "screenHash": "com.linkedin.sdui.flagshipnav.home.Home#0",
            "screenId": "com.linkedin.sdui.flagshipnav.home.Home",
            "pageKey": "",
            "isAnchorPage": True,
            "url": "",
            "childHierarchy": {
                "$type": "proto.sdui.navigation.ScreenHierarchy",
                "screenHash": f"{section.screen_id}#9c4a7cde",
                "screenId": section.screen_id,
                "pageKey": "",
                "isAnchorPage": True,
                "url": "",
            },
        },
        "screenTitle": [],
        "requestedArguments": requested_arguments,
    }


def client_kwargs() -> dict:
    kwargs: dict = {
        "timeout": 15.0,
        "follow_redirects": False,
        "http2": True,
    }
    if settings.LINKEDIN_HTTP_PROXY:
        kwargs["proxy"] = settings.LINKEDIN_HTTP_PROXY
        if settings.LINKEDIN_PROXY_INSECURE:
            kwargs["verify"] = False
    return kwargs


async def _throttle() -> None:
    global _last_linkedin_monotonic
    minimum = max(0.0, float(settings.LINKEDIN_MIN_INTERVAL_SECONDS))
    async with _throttle_lock:
        now = time.monotonic()
        wait = minimum - (now - _last_linkedin_monotonic)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_linkedin_monotonic = time.monotonic()


def _flagship_headers(
    token: str,
    public_id: str,
    *,
    page_key: str = PROFILE_PAGE_KEY,
    screen_id: str = PROFILE_SCREEN_ID,
    referer: str | None = None,
    route_url: str | None = None,
) -> dict[str, str]:
    headers = {
        "User-Agent": DESKTOP_FIREFOX_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Content-Type": "application/json",
        "Origin": "https://www.linkedin.com",
        "Referer": referer or f"https://www.linkedin.com/in/{public_id}/",
        "csrf-token": token,
        "X-Li-Anchor-Page-Key": page_key,
        "X-Li-Closest-Anchor-Page-Key": page_key,
        "X-Li-Leaf-Screen-Id": screen_id,
        "X-Li-Sdui-Version": SDUI_VERSION,
        "X-Li-Application-Version": APPLICATION_VERSION,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if route_url:
        headers["X-Li-Route-Url"] = route_url
    return headers


def _html_headers(token: str, public_id: str) -> dict[str, str]:
    return {
        "User-Agent": DESKTOP_FIREFOX_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "csrf-token": token,
        "Referer": "https://www.linkedin.com/feed/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }


async def fetch_profile_raw(
    public_id: str, li_at: str, jsessionid: str
) -> httpx.Response:
    token = _jsessionid_token(jsessionid)
    url = FLAGSHIP_PROFILE_URL.format(public_id=public_id)
    await _throttle()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        response = await client.post(
            url,
            headers=_flagship_headers(token, public_id),
            cookies=_cookies(li_at, jsessionid),
            json=_profile_request_body(public_id),
        )
    _debug_dump_raw("shell_raw", public_id, response)
    return response


async def fetch_profile_html(
    public_id: str, li_at: str, jsessionid: str
) -> httpx.Response:
    token = _jsessionid_token(jsessionid)
    url = PROFILE_HTML_URL.format(public_id=public_id)
    await _throttle()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        return await client.get(
            url,
            headers=_html_headers(token, public_id),
            cookies=_cookies(li_at, jsessionid),
        )


async def fetch_experience_html(
    public_id: str, li_at: str, jsessionid: str
) -> httpx.Response:
    token = _jsessionid_token(jsessionid)
    url = EXPERIENCE_HTML_URL.format(public_id=public_id)
    await _throttle()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        response = await client.get(
            url,
            headers=_html_headers(token, public_id),
            cookies=_cookies(li_at, jsessionid),
        )
    _debug_dump_raw("experience_raw", public_id, response)
    return response


async def fetch_flagship_details(
    public_id: str,
    section: FlagshipDetailSection,
    li_at: str,
    jsessionid: str,
) -> httpx.Response:
    token = _jsessionid_token(jsessionid)
    url = FLAGSHIP_DETAILS_URL.format(public_id=public_id, slug=section.slug)
    details_path = f"/in/{public_id}/details/{section.slug}/"
    await _throttle()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        response = await client.post(
            url,
            headers=_flagship_headers(
                token,
                public_id,
                page_key=f"d_flagship3_{section.page_key}",
                screen_id=section.screen_id,
                referer=f"https://www.linkedin.com/in/{public_id}/",
                route_url=details_path,
            ),
            cookies=_cookies(li_at, jsessionid),
            json=_details_request_body(public_id, section),
        )
    _debug_dump_raw(section.dump_kind, public_id, response)
    return response


def _json_without_undefined(value: object) -> object:
    if value == "$undefined":
        return None
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _json_without_undefined(item)) is not None
        }
    if isinstance(value, list):
        return [_json_without_undefined(item) for item in value]
    return value


def _section_pagers(section: FlagshipDetailSection, requests: list[dict]) -> list[dict]:
    needle = f"pagers.profile.details.{section.slug}"
    alt = "pagers.profile.details.language" if section.slug == "languages" else needle
    matched = [
        request
        for request in requests
        if needle in str(request.get("pagerId") or "")
        or (alt != needle and alt in str(request.get("pagerId") or ""))
    ]
    return matched


def _prefer_pagination_request(section: FlagshipDetailSection, requests: list[dict]) -> dict | None:
    matched = _section_pagers(section, requests)
    if not matched:
        return None
    if section.slug == "skills":
        for request in matched:
            payload = (request.get("requestedArguments") or {}).get("payload") or {}
            if payload.get("filter") in (None, "ProfileSkillCategory_ALL"):
                return request
    return matched[0]


async def fetch_pagination(
    public_id: str,
    section: FlagshipDetailSection,
    pager_request: dict,
    li_at: str,
    jsessionid: str,
    page_index: int,
) -> httpx.Response:
    token = _jsessionid_token(jsessionid)
    pager_id = str(pager_request.get("pagerId") or "")
    query = urlencode({"sduiid": pager_id})
    details_path = f"/in/{public_id}/details/{section.slug}/"
    payload = _json_without_undefined(pager_request)
    last: httpx.Response | None = None
    for base in PAGINATION_URLS:
        url = f"{base}?{query}"
        await _throttle()
        async with httpx.AsyncClient(**client_kwargs()) as client:
            last = await client.post(
                url,
                headers=_flagship_headers(
                    token,
                    public_id,
                    page_key=f"d_flagship3_{section.page_key}",
                    screen_id=section.screen_id,
                    referer=f"https://www.linkedin.com{details_path}",
                    route_url=f"/rsc-action/actions/pagination/?{query}",
                ),
                cookies=_cookies(li_at, jsessionid),
                json=payload,
            )
        if last.status_code != 404:
            break
    assert last is not None
    _debug_dump_raw(f"{section.dump_kind}_page{page_index}", public_id, last)
    return last


def section_pager_body(
    public_id: str,
    profile_id: str,
    pager_id: str,
    screen_id: str,
    extra_payload: dict | None = None,
    start: int = 0,
    count: int = 10,
) -> dict:
    payload = {
        "vanityName": public_id,
        "profileId": profile_id,
        "start": start,
        "count": count,
        **(extra_payload or {}),
    }
    requested_inner = {
        "$type": "proto.sdui.actions.requests.RequestedArguments",
        "requestedStateKeys": [],
        "payload": dict(payload),
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
    }
    return {
        "pagerId": pager_id,
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": dict(payload),
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "states": [],
            "screenId": screen_id,
            "knownTemplateIds": [],
        },
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": pager_id,
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 3,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": requested_inner,
        },
    }


def _section_for_screen(screen_id: str) -> FlagshipDetailSection | None:
    for section in FLAGSHIP_DETAIL_SECTIONS:
        if section.screen_id == screen_id:
            return section
    return None


def _binding(public_id: str, name: str) -> dict:
    return {
        "type": "com.linkedin.sdui.components.core.BindingImpl",
        "value": {
            "key": f"ProfileComponentState{name}{public_id}ProfileComponentState",
            "namespace": "MemoryNamespace",
        },
    }


def above_activity_component_body(public_id: str, profile_id: str) -> dict:
    """POST body for profileCardsAboveActivity (About + featured)."""
    return {
        "clientArguments": {
            "payload": {
                "isSelfView": False,
                "vanityName": public_id,
                "replaceableSectionArgs": {
                    "vanityName": public_id,
                    "hideCardsForGoldenGate": False,
                    "shouldSetupReplaceableComponent": True,
                    "vieweeProfileId": profile_id,
                    "isSelfView": False,
                    "isSelfViewResolved": False,
                },
                "profileComponentState": {
                    "profileId": public_id,
                    "shouldRefreshScreenOnReappear": _binding(
                        public_id, "ShouldRefreshScreen"
                    ),
                    "shouldFetchFromCache": _binding(public_id, "FetchFromCache"),
                    "shouldDisplayTabAnchors": _binding(
                        public_id, "ShouldDisplayTabAnchors"
                    ),
                    "shouldReloadTopCardOnReappear": _binding(
                        public_id, "ShouldReloadTopCardOnReappear"
                    ),
                    "deferredTopCardReloadProfileId": _binding(
                        public_id, "DeferredTopCardReloadProfileId"
                    ),
                    "shouldDisplayStickyHeader": _binding(
                        public_id, "ShouldDisplayStickyHeader"
                    ),
                    "shouldRefreshLanguageDetailScreen": _binding(
                        public_id, "ShouldRefreshLanguageDetails"
                    ),
                    "lastPerformedActionRef": _binding(
                        public_id, "LastPerformedActionRef"
                    ),
                    "shouldFocusOnReappear": _binding(
                        public_id, "ShouldFocusOnReappear"
                    ),
                    "shouldFocusFeaturedOnReappear": _binding(
                        public_id, "ShouldFocusFeaturedOnReappear"
                    ),
                    "lastFeaturedActionRef": _binding(
                        public_id, "LastFeaturedActionRef"
                    ),
                    "shouldHideProfileCards": _binding(public_id, "ProfileHideCards"),
                },
            },
            "states": [],
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "screenId": PROFILE_SCREEN_ID,
            "knownTemplateIds": [],
        }
    }


async def fetch_above_activity_component(
    public_id: str,
    profile_id: str,
    li_at: str,
    jsessionid: str,
) -> httpx.Response:
    """POST rsc-action component for About (profileCardsAboveActivity)."""
    token = _jsessionid_token(jsessionid)
    query = urlencode(
        {
            "componentId": ABOVE_ACTIVITY_COMPONENT_ID,
            "sduiid": ABOVE_ACTIVITY_COMPONENT_ID,
        }
    )
    url = f"{FLAGSHIP_COMPONENT_URL}?{query}"
    await _throttle()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        response = await client.post(
            url,
            headers=_flagship_headers(
                token,
                public_id,
                page_key=PROFILE_PAGE_KEY,
                screen_id=PROFILE_SCREEN_ID,
                referer=f"https://www.linkedin.com/in/{public_id}/",
                route_url=f"/rsc-action/actions/component/?{query}",
            ),
            cookies=_cookies(li_at, jsessionid),
            json=above_activity_component_body(public_id, profile_id),
        )
    _debug_dump_raw("about_component_raw", public_id, response)
    return response


async def enrich_about(
    snapshot: ProfileSnapshot,
    public_id: str,
    li_at: str,
    jsessionid: str,
) -> ProfileSnapshot:
    if (snapshot.about or "").strip():
        return snapshot
    profile_id = (snapshot.dash_profile_id or "").strip()
    if not profile_id:
        snapshot.notes = list(snapshot.notes) + ["about_skipped:profile_id_missing"]
        return snapshot
    from app.linkedin.pager_normalize import extract_about_text

    try:
        response = await fetch_above_activity_component(
            public_id, profile_id, li_at, jsessionid
        )
    except Exception:
        snapshot.notes = list(snapshot.notes) + ["about_fetch_failed"]
        return snapshot
    classified = _record(
        response,
        li_at=li_at,
        jsessionid=jsessionid,
        endpoint="pager_about",
        public_id=public_id,
        used_as="about",
    )
    if classified.kind is not ResponseClass.OK:
        snapshot.notes = list(snapshot.notes) + [
            f"about_fetch_failed:{classified.reason}:{classified.status}"
        ]
        return snapshot
    try:
        about = extract_about_text(response.content or b"")
    except Exception:
        snapshot.notes = list(snapshot.notes) + ["about_fetch_failed"]
        return snapshot
    if about:
        snapshot.about = about
        snapshot.notes = list(snapshot.notes) + ["live_path:about_component"]
    else:
        snapshot.notes = list(snapshot.notes) + ["about_payload_empty"]
    return snapshot


async def fetch_section_pager(
    public_id: str,
    profile_id: str,
    pager_id: str,
    screen_id: str,
    li_at: str,
    jsessionid: str,
    extra_payload: dict | None = None,
    start: int = 0,
    count: int = 10,
    dump_kind: str = "section_pager_raw",
) -> httpx.Response:
    """POST wrapped PaginationRequest to /flagship-web/rsc-action/actions/pagination."""
    token = _jsessionid_token(jsessionid)
    query = urlencode({"sduiid": pager_id})
    url = f"{FLAGSHIP_PAGER_URL}?{query}"
    section = _section_for_screen(screen_id)
    slug = section.slug if section else "education"
    page_key = (
        f"d_flagship3_{section.page_key}"
        if section
        else "d_flagship3_profile_view_base_education_details"
    )
    details_path = f"/in/{public_id}/details/{slug}/"
    await _throttle()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        response = await client.post(
            url,
            headers=_flagship_headers(
                token,
                public_id,
                page_key=page_key,
                screen_id=screen_id,
                referer=f"https://www.linkedin.com{details_path}",
                route_url=f"/rsc-action/actions/pagination/?{query}",
            ),
            cookies=_cookies(li_at, jsessionid),
            json=section_pager_body(
                public_id,
                profile_id,
                pager_id,
                screen_id,
                extra_payload=extra_payload,
                start=start,
                count=count,
            ),
        )
    _debug_dump_raw(dump_kind, public_id, response)
    return response


def _section_by_slug(slug: str) -> FlagshipDetailSection:
    for section in FLAGSHIP_DETAIL_SECTIONS:
        if section.slug == slug:
            return section
    raise KeyError(slug)


async def fetch_all_skills(
    public_id: str,
    profile_id: str,
    li_at: str,
    jsessionid: str,
) -> list[httpx.Response]:
    """Fetch skills pager pages until no continuation or the 5-page cap."""
    from app.linkedin.pager_normalize import extract_pager_continuation

    section = _section_by_slug("skills")
    pages: list[httpx.Response] = []
    start = 0
    for _index in range(_MAX_SKILL_PAGES):
        response = await fetch_section_pager(
            public_id,
            profile_id,
            section.pager_id,
            section.screen_id,
            li_at,
            jsessionid,
            extra_payload={"filter": "ProfileSkillCategory_ALL"},
            start=start,
            count=_PAGER_PAGE_SIZE,
            dump_kind="skills_pager_v2_raw",
        )
        pages.append(response)
        if classify_pager_response(response).kind is not ResponseClass.OK:
            break
        continuation = extract_pager_continuation(response.content or b"")
        if not continuation:
            break
        payload = (continuation.get("requestedArguments") or {}).get("payload") or {}
        nxt = payload.get("start")
        start = int(nxt) if isinstance(nxt, int) else start + _PAGER_PAGE_SIZE
    return pages


async def enrich_profile_detail_sections(
    snapshot: ProfileSnapshot,
    public_id: str,
    li_at: str,
    jsessionid: str,
) -> ProfileSnapshot:
    """Fetch education/skills/certs/languages pagers; one section failing does not fail others."""
    from app.linkedin.pager_normalize import (
        normalize_certifications_pager,
        normalize_education_pager,
        normalize_languages_pager,
        normalize_skills_pager,
    )
    from app.schemas.profile import SkillItem

    profile_id = (snapshot.dash_profile_id or "").strip()
    if not profile_id:
        snapshot.notes = list(snapshot.notes) + ["pager_skipped:profile_id_missing"]
        snapshot.education = []
        snapshot.skills = []
        snapshot.certifications = []
        snapshot.languages = []
        return snapshot

    async def _one_section(section: FlagshipDetailSection) -> None:
        nonlocal snapshot
        try:
            extra_payload = None
            response = await fetch_section_pager(
                public_id,
                profile_id,
                section.pager_id,
                section.screen_id,
                li_at,
                jsessionid,
                extra_payload=extra_payload,
                dump_kind=f"{section.slug}_pager_v2_raw",
            )
        except Exception:
            snapshot.notes = list(snapshot.notes) + [f"{section.slug}_fetch_failed"]
            _clear_section(section.slug)
            return
        classified = _record(
            response,
            li_at=li_at,
            jsessionid=jsessionid,
            endpoint=f"pager_{section.slug}",
            public_id=public_id,
            used_as=section.slug,
        )
        if classified.kind is not ResponseClass.OK:
            snapshot.notes = list(snapshot.notes) + [
                f"{section.slug}_fetch_failed:{classified.reason}:{classified.status}"
            ]
            _clear_section(section.slug)
            return
        try:
            raw = response.content or b""
            if section.slug == "education":
                snapshot.education = normalize_education_pager(raw)
            elif section.slug == "certifications":
                snapshot.certifications = normalize_certifications_pager(raw)
            elif section.slug == "languages":
                snapshot.languages = normalize_languages_pager(raw)
        except Exception:
            snapshot.notes = list(snapshot.notes) + [f"{section.slug}_fetch_failed"]
            _clear_section(section.slug)
            return
        snapshot.notes = list(snapshot.notes) + [f"live_path:{section.slug}_pager"]

    def _clear_section(slug: str) -> None:
        if slug == "education":
            snapshot.education = []
        elif slug == "skills":
            snapshot.skills = []
        elif slug == "certifications":
            snapshot.certifications = []
        elif slug == "languages":
            snapshot.languages = []

    async def _fetch_skills() -> None:
        nonlocal snapshot
        try:
            skill_pages = await fetch_all_skills(
                public_id, profile_id, li_at, jsessionid
            )
        except Exception:
            snapshot.notes = list(snapshot.notes) + ["skills_fetch_failed"]
            snapshot.skills = []
            return
        ok_bodies: list[bytes] = []
        failed: ClassifiedResponse | None = None
        for index, page in enumerate(skill_pages):
            classified = _record(
                page,
                li_at=li_at,
                jsessionid=jsessionid,
                endpoint="pager_skills",
                public_id=public_id,
                used_as=f"skills_page_{index}",
            )
            if classified.kind is not ResponseClass.OK:
                failed = classified
                break
            ok_bodies.append(page.content or b"")
        if failed is not None and not ok_bodies:
            snapshot.notes = list(snapshot.notes) + [
                f"skills_fetch_failed:{failed.reason}:{failed.status}"
            ]
            snapshot.skills = []
            return
        if failed is not None:
            snapshot.notes = list(snapshot.notes) + [
                f"skills_fetch_failed:{failed.reason}:{failed.status}"
            ]
        try:
            snapshot.skills = [
                SkillItem(name=name) for name in normalize_skills_pager(ok_bodies)
            ]
            snapshot.notes = list(snapshot.notes) + ["live_path:skills_pager"]
        except Exception:
            snapshot.notes = list(snapshot.notes) + ["skills_fetch_failed"]
            snapshot.skills = []

    for section in FLAGSHIP_DETAIL_SECTIONS:
        if section.slug == "skills":
            await _fetch_skills()
            continue
        await _one_section(section)
    return dedupe_snapshot(snapshot)


def _record(
    response: httpx.Response,
    *,
    li_at: str,
    jsessionid: str,
    endpoint: str,
    public_id: str,
    used_as: str,
) -> ClassifiedResponse:
    classified = (
        classify_pager_response(response)
        if endpoint.startswith("pager_")
        else classify_response(response)
    )
    record_classified(
        li_at=li_at,
        jsessionid=jsessionid,
        endpoint=endpoint,
        public_id=public_id,
        classified=classified,
        used_as=used_as,
    )
    return classified


async def fetch_live_profile(
    public_id: str, li_at: str, jsessionid: str
) -> tuple[ProfileSnapshot | None, ClassifiedResponse]:
    """Flagship POST, HTML GET fallback on login/999, experience GET, then details POSTs."""
    from app.linkedin.normalizer import normalize_sdui_profile

    post = await fetch_profile_raw(public_id, li_at, jsessionid)
    post_class = _record(
        post,
        li_at=li_at,
        jsessionid=jsessionid,
        endpoint="flagship_post",
        public_id=public_id,
        used_as="primary",
    )
    primary_response = post
    primary_class = post_class
    path = "flagship_post"

    if post_class.kind is ResponseClass.SESSION_REJECTED:
        html = await fetch_profile_html(public_id, li_at, jsessionid)
        html_class = _record(
            html,
            li_at=li_at,
            jsessionid=jsessionid,
            endpoint="profile_html_get",
            public_id=public_id,
            used_as="fallback",
        )
        primary_response = html
        primary_class = html_class
        path = "profile_html_get"

    if primary_class.kind is not ResponseClass.OK:
        return None, primary_class

    snapshot = normalize_sdui_profile(primary_response.text)
    snapshot.notes = list(snapshot.notes) + [f"live_path:{path}"]

    experience = await fetch_experience_html(public_id, li_at, jsessionid)
    exp_class = _record(
        experience,
        li_at=li_at,
        jsessionid=jsessionid,
        endpoint="experience_html_get",
        public_id=public_id,
        used_as="experience",
    )
    if exp_class.kind is ResponseClass.OK:
        extra = normalize_sdui_profile(experience.text)
        snapshot = merge_snapshots(snapshot, extra)
        snapshot.notes = list(snapshot.notes) + ["live_path:experience_html_get"]
    else:
        snapshot.notes = list(snapshot.notes) + [
            f"experience_skipped:{exp_class.reason}:{exp_class.status}"
        ]

    snapshot = await enrich_about(snapshot, public_id, li_at, jsessionid)
    snapshot = await enrich_profile_detail_sections(
        snapshot, public_id, li_at, jsessionid
    )
    return snapshot, primary_class
