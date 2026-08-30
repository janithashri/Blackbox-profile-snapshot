"""Turn saved LinkedIn HTML (or raw RSC) into an RSC body the normalizer can parse."""

from __future__ import annotations

import json
import re

_REHYDRATION = re.compile(r"window\.__como_rehydration__\s*=\s*")


def split_http_and_body(raw: str) -> str:
    if raw.startswith("HTTP/") and "\n\n" in raw:
        return raw.split("\n\n", 1)[1]
    return raw


def extract_rsc_body(raw: str) -> tuple[str, str]:
    """Return (rsc_text, source_kind).

    HTML profile/experience pages embed the RSC stream in
    ``window.__como_rehydration__``. Flagship POST captures are already RSC.
    """
    body = split_http_and_body(raw)
    match = _REHYDRATION.search(body)
    if not match:
        kind = "flagship-html" if "<html" in body[:2000].lower() else "flagship-sdui"
        return body, kind
    try:
        data, _ = json.JSONDecoder().raw_decode(body[match.end() :])
    except json.JSONDecodeError:
        return body, "flagship-html"
    if isinstance(data, list) and all(isinstance(item, str) for item in data):
        return "".join(data), "flagship-html"
    if isinstance(data, str):
        return data, "flagship-html"
    return body, "flagship-html"
