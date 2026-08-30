"""Same-origin proxy for LinkedIn media.licdn.com profile photos.

Browsers often fail to load those URLs directly (hotlink / Referer checks),
so the console fetches them through this service instead.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx

from app.linkedin.client import DESKTOP_FIREFOX_USER_AGENT, _cookies, client_kwargs
from app.services.jobs import primary_cookies

PHOTO_HOSTS = frozenset({"media.licdn.com", "media-exp.licdn.com"})
_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 4


def is_linkedin_photo_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in PHOTO_HOSTS:
        return False
    if parsed.username or parsed.password:
        return False
    return parsed.path.startswith("/dms/")


async def fetch_linkedin_photo(url: str) -> tuple[bytes, str]:
    """Return (bytes, content-type) for an allowed media.licdn.com URL."""
    current = url.strip()
    if not is_linkedin_photo_url(current):
        raise ValueError("not a linkedin media url")

    kwargs = dict(client_kwargs())
    kwargs["follow_redirects"] = False
    kwargs["timeout"] = 20.0
    headers = {
        "User-Agent": DESKTOP_FIREFOX_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    li_at, jsessionid = primary_cookies()
    cookies = _cookies(li_at, jsessionid) if li_at and jsessionid else None

    async with httpx.AsyncClient(**kwargs) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            response = await client.get(current, headers=headers, cookies=cookies)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("empty redirect")
                nxt = urljoin(current, location)
                if not is_linkedin_photo_url(nxt):
                    raise ValueError("redirect off media host")
                current = nxt
                continue
            response.raise_for_status()
            body = response.content or b""
            if len(body) > _MAX_BYTES:
                raise ValueError("photo too large")
            content_type = response.headers.get("content-type") or "image/jpeg"
            if ";" in content_type:
                content_type = content_type.split(";", 1)[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            return body, content_type
    raise ValueError("too many redirects")
