"""Parse a LinkedIn public vanity from a URL or raw id."""

from __future__ import annotations

import re

_IN_PATH = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/([^/?#]+)",
    re.IGNORECASE,
)
_VANITY = re.compile(r"^[A-Za-z0-9_-]+$")


class PublicIdError(ValueError):
    pass


def parse_linkedin_public_id(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise PublicIdError("empty")
    match = _IN_PATH.search(text)
    if match:
        return match.group(1).strip().rstrip("/")
    stripped = text.split("#")[0].split("?")[0].strip().strip("/")
    if stripped.lower().startswith("in/"):
        stripped = stripped[3:]
    if _VANITY.fullmatch(stripped):
        return stripped
    raise PublicIdError("unrecognized_linkedin_id")
