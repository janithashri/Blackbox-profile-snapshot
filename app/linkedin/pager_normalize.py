"""Normalize flagship-web pagination RSC payloads for details sections."""

from __future__ import annotations

import json
import re

from app.linkedin.capture import extract_rsc_body
from app.linkedin.normalizer import (
    _collect_p_labels,
    _is_chrome_label,
    _looks_like_date_text,
    _p_child_label,
    _resolve_chunk,
    _sane_label,
    parse_rsc_chunks,
)
from app.schemas.profile import Certification, Education, Language

_SKILL_COMPONENT = re.compile(
    r"^com\.linkedin\.sdui\.profile\.skill\([^)]+\)$"
)
_EDUCATION_LOCKUP = "education-lockup-view"
_CERT_LOCKUP = "license-certifications-lockup-view"


def _decode_raw(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _chunks_from_raw(raw: bytes | str) -> dict[str, object]:
    body, _kind = extract_rsc_body(_decode_raw(raw))
    return parse_rsc_chunks(body)


def extract_pager_continuation(raw: bytes | str) -> dict | None:
    """Return the follow-up PaginationRequest embedded in pager RSC, if any.

    Skills (and other long lists) put a JSON string in root chunk slot 1.
    Complete pages use ``$undefined`` there instead.
    """
    chunks = _chunks_from_raw(raw)
    root = chunks.get("0")
    if not isinstance(root, list) or len(root) < 2:
        return None
    slot = root[1]
    if not isinstance(slot, str) or "PaginationRequest" not in slot:
        return None
    try:
        parsed = json.loads(slot)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _react_props(node: object) -> dict | None:
    if isinstance(node, list) and len(node) >= 4 and node[0] == "$":
        props = node[3]
        return props if isinstance(props, dict) else None
    if isinstance(node, dict):
        return node
    return None


def _view_name(node: object) -> str | None:
    props = _react_props(node)
    if not props:
        return None
    specs = props.get("viewTrackingSpecs")
    if isinstance(specs, dict):
        name = specs.get("viewName")
        return name if isinstance(name, str) else None
    return None


def _component_key(node: object) -> str | None:
    props = _react_props(node)
    if not props:
        return None
    key = props.get("componentKey") or props.get("componentkey")
    return key if isinstance(key, str) else None


def _walk(node: object, chunks: dict[str, object], seen: frozenset[str], visit) -> None:
    node = _resolve_chunk(node, chunks, seen)
    visit(node)
    if isinstance(node, dict):
        for value in node.values():
            _walk(value, chunks, seen, visit)
    elif isinstance(node, list):
        for item in node:
            _walk(item, chunks, seen, visit)


def _collect_plain_textprops(
    node: object,
    chunks: dict[str, object],
    seen: frozenset[str],
    acc: list[tuple[str, str | None, str | None]],
) -> None:
    node = _resolve_chunk(node, chunks, seen)
    if isinstance(node, dict):
        props = node.get("textProps")
        if isinstance(props, dict):
            children = props.get("children")
            if (
                isinstance(children, list)
                and children
                and all(isinstance(part, str) for part in children)
            ):
                text = "".join(children).strip()
                if text:
                    weight = props.get("fontWeight")
                    size = props.get("fontSize")
                    acc.append(
                        (
                            text,
                            weight if isinstance(weight, str) else None,
                            size if isinstance(size, str) else None,
                        )
                    )
        for value in node.values():
            _collect_plain_textprops(value, chunks, seen, acc)
    elif isinstance(node, list):
        for item in node:
            _collect_plain_textprops(item, chunks, seen, acc)


def _lockup_subtrees(
    chunks: dict[str, object], view_name: str
) -> list[object]:
    found: list[object] = []

    def visit(node: object) -> None:
        if _view_name(node) == view_name:
            found.append(node)

    for value in chunks.values():
        _walk(value, chunks, frozenset(), visit)
    return found


def _split_degree_field(text: str) -> tuple[str | None, str | None]:
    cleaned = text.strip()
    if ", " not in cleaned:
        return _sane_label(cleaned), None
    degree, field = cleaned.split(", ", 1)
    return _sane_label(degree), _sane_label(field)


def _date_from_subtree(
    node: object, chunks: dict[str, object], seen: frozenset[str]
) -> str | None:
    acc: list[tuple[str, str | None, str | None]] = []
    _collect_plain_textprops(node, chunks, seen, acc)
    for text, _weight, _size in acc:
        if _looks_like_date_text(text):
            return text
    return None


def normalize_education_pager(raw: bytes) -> list[Education]:
    chunks = _chunks_from_raw(raw)
    items: list[Education] = []
    seen_keys: set[tuple[str | None, str | None, str | None]] = set()
    for lockup in _lockup_subtrees(chunks, _EDUCATION_LOCKUP):
        labels: list[tuple[str, str]] = []
        _collect_p_labels(lockup, chunks, frozenset(), labels)
        school = degree_raw = None
        for text, role in labels:
            if _is_chrome_label(text):
                continue
            if role == "title" and school is None:
                school = _sane_label(text)
            elif role == "company" and degree_raw is None:
                degree_raw = text.strip()
        if school is None:
            continue
        degree, field = (
            _split_degree_field(degree_raw) if degree_raw else (None, None)
        )
        date_range = _date_from_subtree(lockup, chunks, frozenset())
        key = (school, degree, date_range)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(
            Education(
                school=school,
                degree=degree,
                field_of_study=field,
                date_range=date_range,
            )
        )
    seen_edu: set[tuple[str, str, str]] = set()
    unique: list[Education] = []
    for item in items:
        key = (
            (item.school or "").casefold(),
            (item.degree or "").casefold(),
            (item.field_of_study or "").casefold(),
        )
        if key in seen_edu:
            continue
        seen_edu.add(key)
        unique.append(item)
    return unique


def normalize_skills_pager(raw: bytes | list[bytes]) -> list[str]:
    pages = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    seen_names: set[str] = set()
    for page in pages:
        for name in _normalize_skills_page(page):
            if name in seen_names:
                continue
            seen_names.add(name)
            names.append(name)
    folded: set[str] = set()
    unique: list[str] = []
    for name in names:
        key = name.casefold()
        if key in folded:
            continue
        folded.add(key)
        unique.append(name)
    return unique


def _normalize_skills_page(raw: bytes) -> list[str]:
    chunks = _chunks_from_raw(raw)
    names: list[str] = []
    seen_keys: set[str] = set()
    seen_names: set[str] = set()

    def visit(node: object) -> None:
        key = _component_key(node)
        if not key or key in seen_keys or key.endswith("-divider"):
            return
        if not _SKILL_COMPONENT.match(key):
            return
        seen_keys.add(key)
        acc: list[tuple[str, str | None, str | None]] = []
        _collect_plain_textprops(node, chunks, frozenset(), acc)
        for text, weight, size in acc:
            if weight != "bold" or size != "medium":
                continue
            if _is_chrome_label(text):
                continue
            label = _sane_label(text)
            if not label or label in seen_names:
                continue
            seen_names.add(label)
            names.append(label)
            return

    for value in chunks.values():
        _walk(value, chunks, frozenset(), visit)
    return names


def _title_company_pairs(
    chunks: dict[str, object],
) -> list[tuple[str | None, str | None, object]]:
    """Sibling ``p`` pairs: styled title then unstyled secondary (certs/edu-like)."""
    pairs: list[tuple[str | None, str | None, object]] = []

    def consider(children: object, container: object) -> None:
        if not isinstance(children, list) or not (2 <= len(children) <= 4):
            return
        labels: list[tuple[str, str]] = []
        for part in children:
            _collect_p_labels(part, chunks, frozenset(), labels)
        if len(labels) < 2:
            return
        title = company = None
        for text, role in labels:
            if _is_chrome_label(text):
                continue
            if role == "title" and title is None:
                title = _sane_label(text)
            elif role == "company" and company is None:
                company = _sane_label(text)
        if title is None or company is None:
            return
        pairs.append((title, company, container))

    def visit(node: object) -> None:
        props = _react_props(node)
        if props and "children" in props:
            consider(props.get("children"), node)
        elif isinstance(node, list):
            consider(node, node)

    for value in chunks.values():
        _walk(value, chunks, frozenset(), visit)
    return pairs


def normalize_certifications_pager(raw: bytes) -> list[Certification]:
    chunks = _chunks_from_raw(raw)
    blob = json.dumps(chunks, ensure_ascii=False)
    if _CERT_LOCKUP not in blob:
        return []
    items: list[Certification] = []
    seen: set[tuple[str | None, str | None]] = set()
    # Confirmed card: sibling p nodes (styled name + unstyled issuer). The
    # license-certifications-lockup-view key sits on the logo, not the text.
    for name, issuer, container in _title_company_pairs(chunks):
        key = (name, issuer)
        if key in seen:
            continue
        seen.add(key)
        date = _date_from_subtree(container, chunks, frozenset())
        items.append(Certification(name=name, issuer=issuer, issue_date=date))
    seen_names: set[str] = set()
    unique: list[Certification] = []
    for item in items:
        key = (item.name or "").casefold()
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        unique.append(item)
    return unique


def normalize_languages_pager(raw: bytes) -> list[Language]:
    """Language cards are styled ``p`` nodes; this dump has no proficiency field."""
    chunks = _chunks_from_raw(raw)
    items: list[Language] = []
    seen: set[str] = set()

    def visit(node: object) -> None:
        text, role = _p_child_label(node, chunks, frozenset())
        if not text or role != "title":
            return
        if _is_chrome_label(text):
            return
        label = _sane_label(text)
        if not label or label.lower() in seen:
            return
        seen.add(label.lower())
        items.append(Language(name=label, proficiency=None))

    for value in chunks.values():
        _walk(value, chunks, frozenset(), visit)
    return items


_ABOUT_VIEW = "profile-card-about"
_ABOUT_SKIP = {
    "about",
    "more",
    "see more",
    "show more",
    "show less",
    "see less",
}


def _about_skip(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _ABOUT_SKIP:
        return True
    if lowered.startswith("expandable_text_block"):
        return True
    return False


def _paragraphs_from_children(children: object) -> list[str]:
    if isinstance(children, str):
        text = children.strip()
        return [text] if text else []
    if not isinstance(children, list):
        return []
    if children and children[0] == "$":
        if len(children) >= 2 and children[1] == "br":
            return []
        props = children[3] if len(children) >= 4 else None
        if isinstance(props, dict):
            return _paragraphs_from_children(props.get("children"))
        return []
    found: list[str] = []
    for item in children:
        found.extend(_paragraphs_from_children(item))
    return found


def _expandable_textprops(node: object) -> dict | None:
    props = _react_props(node)
    if not props:
        return None
    text_props = props.get("textProps")
    if not isinstance(text_props, dict):
        return None
    if "hasShowMore" in text_props or "expansionKey" in props or "lineClamp" in text_props:
        return text_props
    return None


def extract_about_text(raw: bytes | str) -> str | None:
    """Full About body from ``profile-card-about`` (lazy above-activity component).

    LinkedIn inlines the complete expandable text even when ``lineClamp`` is set;
    empty ``br`` rows are dropped and remaining paragraphs are joined with blank lines.
    """
    chunks = _chunks_from_raw(raw)
    cards = _lockup_subtrees(chunks, _ABOUT_VIEW)
    if not cards:
        return None
    paragraphs: list[str] = []
    seen_keys: set[str] = set()

    def visit(node: object) -> None:
        text_props = _expandable_textprops(node)
        if not text_props:
            return
        props = _react_props(node) or {}
        key = props.get("expansionKey")
        if isinstance(key, str):
            if key in seen_keys:
                return
            seen_keys.add(key)
        for text in _paragraphs_from_children(text_props.get("children")):
            if _about_skip(text):
                continue
            paragraphs.append(text)

    _walk(cards[0], chunks, frozenset(), visit)
    if not paragraphs:
        return None
    return "\n\n".join(paragraphs)
