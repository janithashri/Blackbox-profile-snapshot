"""Parse LinkedIn flagship-web SDUI / RSC captures (not Voyager included[])."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.linkedin.capture import extract_rsc_body
from app.linkedin.names import sanitize_full_name
from app.schemas.profile import (
    CertificationItem,
    EducationItem,
    ExperienceItem,
    LanguageItem,
    ProfileSnapshot,
    SkillItem,
)

_CHUNK = re.compile(r"^([0-9a-f]+):(.*)$")
_VANITY = re.compile(r"/in/([A-Za-z0-9_-]+)/")
_ACO = re.compile(r"ACoAA[A-Za-z0-9_-]+")
_MEMBER = re.compile(r"urn:li:member:\d+")
_LAZY = re.compile(
    r"com\.linkedin\.sdui\.generated\.profile\.dsl\.impl\.[A-Za-z0-9_]+"
)
_TITLE_NAME = re.compile(r'"children"\s*:\s*"([^"]+)\s*\|\s*LinkedIn"')
_ARIA_NAME = re.compile(r'"aria-label"\s*:\s*"([^"]{2,80})"')
_DATE_RANGE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4} - "
    r"(Present|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4})"
)
_YEAR_SPAN = re.compile(r"\d{4}\s*[–\-]\s*(Present|\d{4})")
_DURATION_ONLY = re.compile(
    r"^\d+\s+(yrs?|years?|mos?|months?)(\s+\d+\s+(mos?|months?))?\s*$",
    re.I,
)
_CHUNK_REF = re.compile(r"^\$L([0-9a-f]+)$")
_PROFILE_ID = re.compile(r'"profileId"\s*:\s*"(ACoAA[A-Za-z0-9_-]+)"')
_MEDIA_HOST = "https://media.licdn.com"
_PHOTO_KIND = re.compile(
    r"profile-(?:framedphoto|displayphoto)-"
)
_PHOTO_COMPLETE = re.compile(r"(?:shrink|scale)_\d+_\d+/")

_UI_CHROME = {
    "edit profile",
    "open to",
    "add section",
    "enhance profile",
    "resources",
    "more",
    "close",
    "add photo",
    "add background image",
    "add cover image",
    "cover photo",
    "primary content",
    "aside",
    "footer",
    "about",
    "experience",
    "education",
    "skills",
    "activity",
    "follow",
    "message",
    "connect",
    "more profiles for you",
    "ad options",
}

_HEADLINE_SKIP = _UI_CHROME | {
    "0 notifications",
    "don't want to see this",
    "show all",
    "see more",
    "show more",
    "show less",
    "save to pdf",
    "saved items",
    "activity",
    "about this member",
    "connect",
    "following",
    "follow",
    "pending",
    "hiring",
}

_LIST_CHROME = {
    "all",
    "industry knowledge",
    "tools and technologies",
    "interpersonal",
    "spoken languages",
    "other",
    "in progress",
    "verified",
    "premium",
    "primary content",
    "profile language",
    "licenses & certifications",
    "privacy policy",
    "user agreement",
    "pages terms",
    "cookie policy",
    "copyright policy",
    "visit our help center.",
    "go to your settings.",
    "learn more about recommended content.",
    "linkedin corporation © 2026",
}

_HEADLINE_SKIP_SUBSTR = (
    "free trial",
    "send profile in a",
    "finding a new job",
    "show recruiters",
    "providing services",
    "showcase services",
    "finding volunteer",
    "share that you",
    "profile language",
    "public profile",
    "community guidelines",
    "talent solutions",
    "privacy & terms",
    "sales solutions",
    "report / block",
    "ad choices",
)

_WORKPLACE = {"on-site", "remote", "hybrid", "full-time", "part-time", "contract", "internship"}

_SECTION_HEADINGS = {
    "experience",
    "education",
    "skills",
    "about",
    "licenses & certifications",
    "projects",
    "volunteering",
    "languages",
    "publications",
    "honors & awards",
    "recommendations",
}


def parse_rsc_chunks(body: str) -> dict[str, object]:
    chunks: dict[str, object] = {}
    for line in body.splitlines():
        match = _CHUNK.match(line)
        if not match:
            continue
        chunk_id, rest = match.group(1), match.group(2)
        if rest.startswith("I["):
            continue
        try:
            chunks[chunk_id] = json.loads(rest)
        except json.JSONDecodeError:
            continue
    return chunks


def _blob(chunks: dict[str, object]) -> str:
    return json.dumps(chunks, ensure_ascii=False)


def _walk_text_props(obj: object, texts: list[str]) -> None:
    if isinstance(obj, dict):
        props = obj.get("textProps")
        if isinstance(props, dict):
            children = props.get("children")
            if isinstance(children, list):
                joined = "".join(part for part in children if isinstance(part, str)).strip()
                if joined:
                    texts.append(joined)
            elif isinstance(children, str) and children.strip():
                texts.append(children.strip())
        for value in obj.values():
            _walk_text_props(value, texts)
    elif isinstance(obj, list):
        for item in obj:
            _walk_text_props(item, texts)


def collect_text_nodes(chunks: dict[str, object]) -> list[str]:
    texts: list[str] = []
    for value in chunks.values():
        _walk_text_props(value, texts)
    return texts


def _looks_like_location(text: str) -> bool:
    lowered = text.lower()
    if lowered in _WORKPLACE or lowered in _SECTION_HEADINGS:
        return False
    if any(
        token in lowered
        for token in ("mongodb", "react", "node.js", "mern", "stack (", "express")
    ):
        return False
    if "area" in lowered or lowered == "remote":
        return True
    if text.count(",") >= 1 and 8 <= len(text) <= 80:
        return bool(
            re.search(
                r"\b(india|usa|united|kingdom|canada|australia|germany|france|"
                r"california|texas|kerala|tamil|nadu|province|county|district)\b",
                lowered,
            )
        )
    return False


def _looks_like_date_text(text: str) -> bool:
    cleaned = text.strip()
    return bool(_DATE_RANGE.search(cleaned) or _YEAR_SPAN.search(cleaned))


def _looks_like_duration_only(text: str) -> bool:
    return bool(_DURATION_ONLY.match(text.strip()))


def _sane_label(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 220:
        return None
    if _looks_like_date_text(cleaned):
        return None
    if _looks_like_duration_only(cleaned):
        return None
    if _is_chrome_label(cleaned):
        return None
    return cleaned


def _resolve_chunk(node: object, chunks: dict[str, object], seen: frozenset[str]) -> object:
    if isinstance(node, str):
        match = _CHUNK_REF.match(node)
        if match:
            chunk_id = match.group(1)
            if chunk_id in chunks and chunk_id not in seen:
                return _resolve_chunk(chunks[chunk_id], chunks, seen | {chunk_id})
    return node


def _date_from_node(node: object, chunks: dict[str, object], seen: frozenset[str]) -> str | None:
    node = _resolve_chunk(node, chunks, seen)
    if isinstance(node, dict):
        text_props = node.get("textProps")
        if isinstance(text_props, dict):
            children = text_props.get("children")
            if isinstance(children, list):
                text = "".join(part for part in children if isinstance(part, str)).strip()
                if text and _looks_like_date_text(text):
                    # Ensure it doesn't match a workplace type accidentally
                    if text.strip().lower() not in _WORKPLACE:
                        return text
        for value in node.values():
            found = _date_from_node(value, chunks, seen)
            if found: return found
    elif isinstance(node, list):
        for item in node:
            found = _date_from_node(item, chunks, seen)
            if found: return found
    return None


def _workplace_from_node(node: object, chunks: dict[str, object], seen: frozenset[str]) -> str | None:
    node = _resolve_chunk(node, chunks, seen)
    if isinstance(node, dict):
        text_props = node.get("textProps")
        if isinstance(text_props, dict):
            children = text_props.get("children")
            if isinstance(children, list):
                text = "".join(part for part in children if isinstance(part, str)).strip()
                if text.strip().lower() in _WORKPLACE:
                    return text.strip()
        for value in node.values():
            found = _workplace_from_node(value, chunks, seen)
            if found: return found
    elif isinstance(node, list):
        for item in node:
            found = _workplace_from_node(item, chunks, seen)
            if found: return found
    return None


def _direct_date_and_workplace(
    children: list, chunks: dict[str, object], seen: frozenset[str]
) -> tuple[str | None, str | None]:
    workplace = None
    for part in children:
        found = _workplace_from_node(part, chunks, seen)
        if found:
            workplace = found
    date_range = _date_from_node(children[-1], chunks, seen)
    if date_range is None and len(children) >= 2:
        last_is_workplace = _workplace_from_node(children[-1], chunks, seen)
        if last_is_workplace is None:
            date_range = _date_from_node(children[-2], chunks, seen)
    return date_range, workplace


def _p_child_label(node: object, chunks: dict[str, object], seen: frozenset[str]) -> tuple[str | None, str]:
    """Return (text, role) for a React `p` node. Title nodes carry a style dict; company nodes do not."""
    node = _resolve_chunk(node, chunks, seen)
    if not (isinstance(node, list) and len(node) >= 4 and node[1] == "p"):
        return None, ""
    props = node[3]
    if not isinstance(props, dict):
        return None, ""
    children = props.get("children")
    if not (isinstance(children, list) and children and isinstance(children[0], str)):
        return None, ""
    text = children[0].strip()
    role = "title" if isinstance(props.get("style"), dict) else "company"
    return text, role


def _collect_p_labels(
    node: object, chunks: dict[str, object], seen: frozenset[str], acc: list[tuple[str, str]]
) -> None:
    text, role = _p_child_label(node, chunks, seen)
    if text:
        acc.append((text, role))
        return
    node = _resolve_chunk(node, chunks, seen)
    if isinstance(node, dict):
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                _collect_p_labels(child, chunks, seen, acc)
    elif isinstance(node, list):
        if len(node) >= 4 and node[0] == "$":
            text, role = _p_child_label(node, chunks, seen)
            if text:
                acc.append((text, role))
                return
            props = node[3] if isinstance(node[3], dict) else None
            if props and "children" in props:
                _collect_p_labels(props["children"], chunks, seen, acc)
            return
        for child in node:
            _collect_p_labels(child, chunks, seen, acc)


def _split_company_workplace(text: str) -> tuple[str, str | None]:
    if " · " not in text:
        return text.strip(), None
    left, right = text.rsplit(" · ", 1)
    if right.strip().lower() in _WORKPLACE:
        return left.strip(), right.strip()
    return text.strip(), None


def extract_experience_from_chunks(chunks: dict[str, object]) -> list[ExperienceItem]:
    """Pair title/company `p` siblings with the following date `textProps` node.

    Observed card shape (experience details RSC):
    children: [ <div>[title p, company p]</div>, <date $Lae textProps> ]
    Title `p` has a `style` object; company `p` does not. No semantic type field.
    """
    items: list[ExperienceItem] = []
    seen_keys: set[tuple[str | None, str | None, str | None]] = set()

    def consider_children(children: object, seen: frozenset[str]) -> None:
        if not isinstance(children, list) or len(children) < 2:
            return
        # Guard: if children[-1] contains its own title/company p-labels AND children[:-1]
        # also contain labels, this is a group-body container (e.g. [Manager-card,
        # Deputy-Manager-card]) — not a leaf card whose last child is a date node.
        # We must NOT skip when all labels are concentrated in children[-1] (leaf-card
        # shape like [div, div, div, card-node] where the whole card is in child 3).
        last_child_labels: list[tuple[str, str]] = []
        _collect_p_labels(children[-1], chunks, seen, last_child_labels)
        if last_child_labels:
            preceding_labels: list[tuple[str, str]] = []
            for part in children[:-1]:
                _collect_p_labels(part, chunks, seen, preceding_labels)
            if preceding_labels:
                # Both last and preceding children have labels → multi-card group body.
                return
        date_range, workplace_from_meta = _direct_date_and_workplace(
            children, chunks, seen
        )
        label_nodes = children
        if date_range is not None:
            label_nodes = children[:-1]
        labels: list[tuple[str, str]] = []
        for part in label_nodes:
            _collect_p_labels(part, chunks, seen, labels)

        if not labels:
            return
        title_count = sum(1 for _text, role in labels if role == "title")
        if title_count > 1:
            return
        title = company = location = None
        workplace = workplace_from_meta
        glued_from_company = False
        leftovers: list[str] = []
        for text, role in labels:
            if text.strip().lower() in _WORKPLACE:
                workplace = text.strip()
                continue
            if _looks_like_date_text(text) or _looks_like_duration_only(text):
                continue
            if _looks_like_location(text):
                location = text.strip()
                continue
            if role == "title" and title is None:
                title = text
            elif role == "company" and company is None:
                company, glued = _split_company_workplace(text)
                if glued:
                    glued_from_company = True
                    if workplace is None:
                        workplace = glued
            else:
                leftovers.append(text)
        if title is None and leftovers:
            title = leftovers.pop(0)
        if company is None and leftovers:
            company = leftovers.pop(0)
        if location is None:
            for extra in leftovers:
                if _looks_like_location(extra):
                    location = extra
                    break
        title = _sane_label(title)
        company = _sane_label(company)
        if company:
            company, glued = _split_company_workplace(company)
            if glued:
                glued_from_company = True
                if workplace is None:
                    workplace = glued
            company = _sane_label(company)
        location = _sane_label(location)
        if glued_from_company:
            date_range = None
        if not date_range:
            return
        if not title and not company:
            return
        key = (date_range, title, company)
        if key in seen_keys:
            return
        seen_keys.add(key)
        items.append(
            ExperienceItem(
                title=title,
                company=company,
                date_range=date_range,
                location=location,
                workplace_type=workplace,
            )
        )

    def walk(node: object, seen: frozenset[str]) -> None:
        node = _resolve_chunk(node, chunks, seen)
        if isinstance(node, dict):
            children = node.get("children")
            if isinstance(children, list):
                consider_children(children, seen)
            for value in node.values():
                walk(value, seen)
        elif isinstance(node, list):
            consider_children(node, seen)
            for item in node:
                walk(item, seen)

    for value in chunks.values():
        walk(value, frozenset())
    return items


def _is_chrome_label(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _UI_CHROME or lowered in _HEADLINE_SKIP or lowered in _LIST_CHROME:
        return True
    if "linkedin corporation" in lowered or lowered.endswith("policy"):
        return True
    return False


def extract_title_company_pairs(chunks: dict[str, object]) -> list[tuple[str, str, str | None]]:
    pairs: list[tuple[str, str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    pending: str | None = None
    for text, role in collect_p_labels(chunks):
        if _is_chrome_label(text):
            pending = None
            continue
        if _looks_like_duration_only(text):
            pending = None
            continue
        if role == "title":
            pending = text.strip()
            continue
        if not pending:
            continue
        company, workplace = _split_company_workplace(text)
        if _looks_like_duration_only(company):
            pending = None
            continue
        if _is_chrome_label(company):
            pending = None
            continue
        key = (pending, company)
        if key not in seen:
            seen.add(key)
            pairs.append((pending, company, workplace))
        pending = None
    return pairs


def merge_experience_pairs(
    items: list[ExperienceItem], chunks: dict[str, object]
) -> list[ExperienceItem]:
    jobs = _jobs_from_label_sequence(chunks)
    known_companies = {c for t, c, w in jobs if c}
    used: set[int] = set()
    merged: list[ExperienceItem] = []
    # Track how many times we've matched each title, for positional (Nth-occurrence) matching.
    title_match_count: dict[str, int] = {}
    for title, company, workplace in jobs:
        occurrence = title_match_count.get(title, 0)
        title_match_count[title] = occurrence + 1
        match_index = None
        seen_title_count = 0
        for index, item in enumerate(items):
            if index in used or not item.title:
                continue
            if item.title != title:
                continue
            if item.company and company and item.company != company:
                continue
            # Positional matching: if this title appears multiple times in items
            # (e.g. two "Manager" sub-roles under a group header), we match
            # the Nth card (occurrence index) to the Nth job.
            if seen_title_count < occurrence:
                seen_title_count += 1
                continue
            match_index = index
            break
        if match_index is not None:
            item = items[match_index]
            used.add(match_index)
            if not item.company:
                item.company = company
            if not item.workplace_type:
                item.workplace_type = workplace
            merged.append(item)
        else:
            merged.append(
                ExperienceItem(title=title, company=company, workplace_type=workplace)
            )
    for index, item in enumerate(items):
        if index in used:
            continue
        if item.company and _looks_like_duration_only(item.company):
            continue
        # Filter group-header-as-title noise (company name used as a title on a
        # group body container which has no explicit company).
        if item.title and item.title in known_companies:
            continue
        # Also drop any item whose title exactly matches a known company and has no date.
        if item.title and item.title in known_companies and not item.date_range:
            continue
        if item.title or item.date_range:
            merged.append(item)
    return merged


def _jobs_from_label_sequence(
    chunks: dict[str, object],
) -> list[tuple[str, str | None, str | None]]:
    """Walk title/company `p` nodes in order; company-group headers carry forward."""
    labels = _main_experience_labels(collect_p_labels(chunks))
    jobs: list[tuple[str, str | None, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()
    header_company: str | None = None
    index = 0
    while index < len(labels):
        text, role = labels[index]
        nxt = labels[index + 1] if index + 1 < len(labels) else None
        if (
            role == "title"
            and nxt
            and nxt[1] == "company"
            and _looks_like_duration_only(nxt[0])
        ):
            header_company = _sane_label(text)
            index += 2
            continue
        if role != "title":
            index += 1
            continue
        title = _sane_label(text)
        company = workplace = None
        if nxt and nxt[1] == "company" and not _looks_like_duration_only(nxt[0]):
            company, workplace = _split_company_workplace(nxt[0])
            company = _sane_label(company)
            index += 2
        else:
            company = header_company
            index += 1
        if not title:
            continue
        key = (title, company)
        if key in seen:
            continue
        seen.add(key)
        jobs.append((title, company, workplace))
    return jobs


def _main_experience_labels(
    labels: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    trimmed: list[tuple[str, str]] = []
    for text, role in labels:
        if _is_chrome_label(text):
            break
        if role == "company" and text.strip().lower() in {"privacy policy"}:
            break
        trimmed.append((text, role))
    if not trimmed:
        return trimmed
    first = trimmed[0]
    for pos in range(4, len(trimmed)):
        if trimmed[pos] == first:
            return trimmed[:pos]
    return trimmed


def _unescape_media_url(url: str) -> str:
    return (
        url.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
        .rstrip(".,);")
    )


def _is_complete_photo_url(url: str) -> bool:
    if not url.startswith(_MEDIA_HOST):
        return False
    if not _PHOTO_KIND.search(url):
        return False
    if url.endswith(("shrink_", "displayphoto-", "framedphoto-")):
        return False
    return bool(_PHOTO_COMPLETE.search(url)) or "?" in url


def _quoted_media_urls(text: str) -> list[str]:
    """Take https://media.licdn.com ... up to the next unescaped quote (JSON/HTML string)."""
    found: list[str] = []
    start = 0
    while True:
        i = text.find(_MEDIA_HOST, start)
        if i < 0:
            break
        opener = text[i - 1] if i > 0 and text[i - 1] in "\"'" else None
        j = i
        while j < len(text):
            ch = text[j]
            if opener:
                if ch == "\\" and j + 1 < len(text):
                    j += 2
                    continue
                if ch == opener:
                    break
            elif ch in " \t\r\n\"'<>":
                break
            j += 1
        found.append(_unescape_media_url(text[i:j]))
        start = i + 1
    return found


def _rendition_photos_from_text(text: str) -> list[str]:
    found: list[str] = []
    for root_match in re.finditer(
        r'"rootUrl"\s*:\s*"(https://media\.licdn\.com/[^"]+)"',
        text,
    ):
        window = text[root_match.end() : root_match.end() + 4000]
        for suffix_match in re.finditer(r'"suffixUrl"\s*:\s*"([^"]+)"', window):
            found.append(_unescape_media_url(root_match.group(1) + suffix_match.group(1)))
    return found


def _rendition_photos(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        root = node.get("rootUrl")
        renditions = node.get("imageRenditions")
        if isinstance(root, str) and isinstance(renditions, list):
            for item in renditions:
                if isinstance(item, dict) and isinstance(item.get("suffixUrl"), str):
                    found.append(_unescape_media_url(root + item["suffixUrl"]))
        for value in node.values():
            found.extend(_rendition_photos(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_rendition_photos(item))
    return found


def extract_photo_url(blob: str, chunks: dict[str, object] | None = None) -> str | None:
    """Complete profile photo URL; ignore LinkedIn `rootUrl` that ends at `shrink_`."""
    found: list[str] = []
    found.extend(_quoted_media_urls(blob))
    found.extend(_rendition_photos_from_text(blob))
    if chunks:
        found.extend(_rendition_photos(chunks))
    complete = [url for url in found if _is_complete_photo_url(url)]
    if not complete:
        return None

    def _rank(url: str) -> tuple[int, int]:
        size = 0
        match = re.search(r"(?:shrink|scale)_(\d+)_(\d+)", url)
        if match:
            size = int(match.group(1))
        return (size, len(url))

    return max(complete, key=_rank)


def extract_pagination_requests(chunks: dict[str, object]) -> list[dict]:
    """Pull SDUI `nextPageRequest` objects (lazy education/skills/certs lists)."""
    found: list[dict] = []
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            request = node.get("nextPageRequest")
            if isinstance(request, dict) and request.get("pagerId"):
                key = json.dumps(request.get("requestedArguments"), sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    found.append(request)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for value in chunks.values():
        walk(value)
    return found


def collect_p_labels(chunks: dict[str, object]) -> list[tuple[str, str]]:
    labels: list[tuple[str, str]] = []

    def walk(node: object, seen: frozenset[str]) -> None:
        text, role = _p_child_label(node, chunks, seen)
        if text:
            labels.append((text, role))
        node = _resolve_chunk(node, chunks, seen)
        if isinstance(node, dict):
            for value in node.values():
                walk(value, seen)
        elif isinstance(node, list):
            for item in node:
                walk(item, seen)

    for value in chunks.values():
        walk(value, frozenset())
    return labels


def _usable_list_label(text: str) -> bool:
    cleaned = text.strip()
    lowered = cleaned.lower()
    if len(cleaned) < 2 or len(cleaned) > 160:
        return False
    if lowered in _UI_CHROME or lowered in _HEADLINE_SKIP or lowered in _LIST_CHROME:
        return False
    if lowered in _SECTION_HEADINGS or lowered in _WORKPLACE:
        return False
    if _looks_like_date_text(cleaned):
        return False
    if any(part in lowered for part in _HEADLINE_SKIP_SUBSTR):
        return False
    if cleaned.startswith("urn:li:"):
        return False
    return True


def extract_skill_items(chunks: dict[str, object]) -> list[SkillItem]:
    names: list[str] = []
    seen: set[str] = set()
    for text, _role in collect_p_labels(chunks):
        if not _usable_list_label(text):
            continue
        key = text.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(text.strip())
    return [SkillItem(name=name) for name in names]


def education_from_cards(cards: list[ExperienceItem]) -> list[EducationItem]:
    return [
        EducationItem(
            school=card.title,
            degree=card.company,
            date_range=card.date_range,
            location=card.location,
        )
        for card in cards
        if card.title or card.company or card.date_range
    ]


def certifications_from_cards(cards: list[ExperienceItem]) -> list[CertificationItem]:
    return [
        CertificationItem(
            name=card.title,
            issuer=card.company,
            date_range=card.date_range,
        )
        for card in cards
        if card.title or card.company or card.date_range
    ]


def languages_from_cards(
    cards: list[ExperienceItem], chunks: dict[str, object]
) -> list[LanguageItem]:
    items: list[LanguageItem] = []
    seen: set[str] = set()
    for card in cards:
        name = card.title or card.company
        if not name or not _usable_list_label(name):
            continue
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            LanguageItem(
                name=name.strip(),
                proficiency=card.company if card.title else None,
            )
        )
    if items:
        return items
    for skill in extract_skill_items(chunks):
        if skill.name:
            items.append(LanguageItem(name=skill.name))
    return items


def extract_experience(texts: list[str]) -> list[ExperienceItem]:
    start = 0
    for index, text in enumerate(texts):
        if text.strip().lower() == "experience":
            start = index + 1
            break
    slice_ = texts[start:]
    end = len(slice_)
    for index, text in enumerate(slice_):
        if text.strip().lower() in _SECTION_HEADINGS and text.strip().lower() != "experience":
            end = index
            break
    region = slice_[:end]

    items: list[ExperienceItem] = []
    pending_location: str | None = None
    pending_workplace: str | None = None
    for text in region:
        lowered = text.strip().lower()
        if lowered in _SECTION_HEADINGS:
            continue
        if lowered in _WORKPLACE:
            pending_workplace = text.strip()
            continue
        if _DATE_RANGE.search(text):
            items.append(
                ExperienceItem(
                    date_range=text.strip(),
                    location=pending_location,
                    workplace_type=pending_workplace,
                )
            )
            continue
        if _looks_like_location(text):
            pending_location = text.strip()
    return items


def _usable_headline(text: str) -> bool:
    cleaned = text.strip()
    lowered = cleaned.lower()
    if len(cleaned) < 8 or len(cleaned) > 220:
        return False
    if lowered in _HEADLINE_SKIP:
        return False
    if lowered in {"following", "follow"}:
        return False
    if "| LinkedIn" in cleaned:
        return False
    if _DATE_RANGE.search(cleaned):
        return False
    if _looks_like_location(cleaned):
        return False
    if any(part in lowered for part in _HEADLINE_SKIP_SUBSTR):
        return False
    return True


def extract_headline(texts: list[str], *, details_page: bool) -> str | None:
    if details_page:
        return None
    candidates = [text.strip() for text in texts[:40] if _usable_headline(text)]
    for candidate in candidates:
        if " at " in candidate.lower():
            return candidate
    return candidates[0] if candidates else None


def _member_near_profile_id(blob: str, profile_id: str) -> str | None:
    pos = blob.find(profile_id)
    if pos < 0:
        return None
    window = blob[max(0, pos - 800) : pos + 800]
    found = _MEMBER.findall(window)
    return found[0] if found else None


def normalize_sdui_profile(raw: str, *, section: str | None = None) -> ProfileSnapshot:
    body, source = extract_rsc_body(raw)
    chunks = parse_rsc_chunks(body)
    blob = _blob(chunks) if chunks else body
    notes: list[str] = []
    texts = collect_text_nodes(chunks)

    vanity = None
    found = _VANITY.findall(blob)
    if found:
        vanity = found[0]

    full_name = None
    title = _TITLE_NAME.search(blob)
    if title:
        full_name = title.group(1).strip()
    if not full_name:
        for label in _ARIA_NAME.findall(blob):
            if label.strip().lower() in _UI_CHROME:
                continue
            if "|" in label or "LinkedIn" in label:
                continue
            if 2 < len(label) < 80 and not label.startswith("$"):
                full_name = sanitize_full_name(label.strip())
                if full_name:
                    break

    full_name = sanitize_full_name(full_name)

    aco = _PROFILE_ID.findall(blob)
    if not aco:
        aco = _ACO.findall(blob)
    profile_id = aco[0] if aco else None
    member = _member_near_profile_id(blob, profile_id) if profile_id else None
    if not member:
        members = _MEMBER.findall(blob)
        member = members[0] if members else None
    lazy = sorted(set(_LAZY.findall(blob)))

    is_oops = None
    if re.search(r'"isOops"\s*:\s*false', blob):
        is_oops = False
    elif re.search(r'"isOops"\s*:\s*true', blob):
        is_oops = True

    screen_id = None
    details_page = any(
        key in blob
        for key in (
            "profile_view_base_position_details",
            "profile_view_base_education_details",
            "profile_view_base_skills_details",
            "profile_view_base_certifications_details",
            "profile_view_base_languages_details",
        )
    )
    if "ProfileEducationDetails" in blob:
        screen_id = "com.linkedin.sdui.flagshipnav.profile.ProfileEducationDetails"
    elif "ProfileSkillDetails" in blob:
        screen_id = "com.linkedin.sdui.flagshipnav.profile.ProfileSkillDetails"
    elif "ProfileCertificationDetails" in blob:
        screen_id = "com.linkedin.sdui.flagshipnav.profile.ProfileCertificationDetails"
    elif "ProfileLanguageDetails" in blob:
        screen_id = "com.linkedin.sdui.flagshipnav.profile.ProfileLanguageDetails"
    elif "profile_view_base_position_details" in blob:
        screen_id = "com.linkedin.sdui.flagshipnav.profile.ProfileExperienceDetails"
    elif "com.linkedin.sdui.flagshipnav.profile.Profile" in blob:
        screen_id = "com.linkedin.sdui.flagshipnav.profile.Profile"

    if "fsd_profile" not in blob and '"included"' not in blob:
        notes.append("Voyager included/fsd_profile not present; SDUI first-paint only")
    if any("ExperienceOnly" in item or "BelowActivityPart" in item for item in lazy):
        notes.append(
            "Experience/education sections are lazy (rsc-action componentId); not inlined here"
        )

    cards = extract_experience_from_chunks(chunks)
    experience: list[ExperienceItem] = []
    education: list[EducationItem] = []
    certifications: list[CertificationItem] = []
    skills: list[SkillItem] = []
    languages: list[LanguageItem] = []
    inferred = section
    if inferred is None:
        if "profile_view_base_education_details" in blob:
            inferred = "education"
        elif "profile_view_base_skills_details" in blob:
            inferred = "skills"
        elif "profile_view_base_certifications_details" in blob:
            inferred = "certifications"
        elif "profile_view_base_languages_details" in blob:
            inferred = "languages"
    if inferred == "education":
        education = education_from_cards(cards)
    elif inferred == "certifications":
        certifications = certifications_from_cards(cards)
    elif inferred == "skills":
        skills = extract_skill_items(chunks)
    elif inferred == "languages":
        languages = languages_from_cards(cards, chunks)
    else:
        experience = cards
        if not experience:
            experience = extract_experience(texts)
        experience = merge_experience_pairs(experience, chunks)
    headline = extract_headline(texts, details_page=details_page)
    location = None
    for item in experience:
        if item.location:
            location = item.location
            break
    if location is None and not details_page:
        for text in texts:
            if _looks_like_location(text) and text.strip().lower() not in _WORKPLACE:
                location = text.strip()
                break
    photo = extract_photo_url(raw, chunks) or extract_photo_url(blob, chunks)
    from app.linkedin.pager_normalize import extract_about_text

    about = extract_about_text(raw)

    if source == "flagship-html":
        notes.append("Parsed from HTML __como_rehydration__ RSC stream")
    if inferred in {"education", "skills", "certifications", "languages"} and not (
        education or skills or certifications or languages
    ):
        notes.append(f"{inferred}_payload_not_inlined; pager follow-up required")

    from app.linkedin.merge import dedupe_snapshot

    return dedupe_snapshot(
        ProfileSnapshot(
            vanity_name=vanity,
            full_name=full_name,
            dash_profile_id=profile_id,
            member_urn=member,
            headline=headline,
            location=location,
            about=about,
            photo_url=photo,
            experience=experience,
            education=education,
            skills=skills,
            certifications=certifications,
            languages=languages,
            is_oops=is_oops,
            screen_id=screen_id,
            lazy_component_ids=lazy,
            source=source,
            notes=notes,
        )
    )


def normalize_sdui_file(path: str | Path) -> ProfileSnapshot:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return normalize_sdui_profile(text)
