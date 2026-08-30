"""Strip LinkedIn chrome that sometimes gets glued onto display names."""

from __future__ import annotations

import re

# Longest first so "invite to connect" wins over "invite" / "connect".
_NAME_CHROME = (
    "and",
    "to",
    "And",
    "To",
    "Know",
    "know",
    "in",
    "In",
    "for",
    "For",
    "with",
    "With",
    "on",
    "On",
    "at",
    "At",
    "by",
    "By",
    "from",
    "From",
    "about",
    "About",
    "as",
    "As",
    "for",
    "For",
    "with",
    "With",
    "on",
    "On",
    "at",
    "At",
    "invite to connect",
    "invited to connect",
    "to connect",
    "connect to",
    "connect with",
    "pending invite",
    "send invitation",
    "send invite",
    "remove connection",
    "follow",
    "Follow",
    "following",
    "Following",
    "unfollow",
    "Unfollow",
    "invite",
    "connect",
    "message",
    "messaging",
    "pending",
    "ignore",
    "accept",
    "withdraw",
    "plus",
    "1st",
    "2nd",
    "3rd",
)

_PHRASE = re.compile(
    r"(?i)(?:^|[\s,|]+)(?:"
    + "|".join(re.escape(p) for p in _NAME_CHROME)
    + r")(?=[\s,|]|$)"
)
_MULTI_SPACE = re.compile(r"\s+")


def sanitize_full_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\u00a0", " ").strip()
    if not text:
        return None
    text = text.split("|")[0].strip()
    text = text.split("\n")[0].strip()
    previous = None
    while previous != text:
        previous = text
        text = _PHRASE.sub(" ", text)
        text = _MULTI_SPACE.sub(" ", text).strip(" \t,.-")
    return text or None
