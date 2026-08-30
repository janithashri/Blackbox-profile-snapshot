"""Merge profile snapshots; later sources fill empty fields only."""

import re

from app.schemas.profile import ProfileSnapshot

_SKIP_MERGE = {"source"}
_LIST_MERGE_KEYS = {
    "education": ("school", "degree", "date_range"),
    "skills": ("name",),
    "certifications": ("name", "issuer", "date_range"),
    "languages": ("name", "proficiency"),
}


def _incomplete_photo(url: object) -> bool:
    if not isinstance(url, str) or not url:
        return True
    return not re.search(r"(?:shrink|scale)_\d+_\d+", url)


def _merge_experience(primary: list, extra: list) -> list:
    if not extra:
        return primary
    if not primary:
        return extra
    merged: list[dict] = []
    index_by_date: dict[str, int] = {}
    for item in primary + extra:
        date = item.get("date_range")
        if date and date in index_by_date:
            current = merged[index_by_date[date]]
            for field in ("title", "company", "location", "workplace_type"):
                if current.get(field) in (None, "") and item.get(field) not in (None, ""):
                    current[field] = item.get(field)
            continue
        merged.append(dict(item))
        if date:
            index_by_date[date] = len(merged) - 1
    return merged


def _item_key(item: dict, fields: tuple[str, ...]) -> tuple:
    return tuple(item.get(field) for field in fields)


def _merge_named_list(primary: list, extra: list, fields: tuple[str, ...]) -> list:
    if not extra:
        return primary
    if not primary:
        return extra
    merged: list[dict] = []
    seen: set[tuple] = set()
    for item in primary + extra:
        key = _item_key(item, fields)
        if key in seen and any(part not in (None, "") for part in key):
            continue
        seen.add(key)
        merged.append(dict(item))
    return merged


def merge_snapshots(
    primary: ProfileSnapshot, *others: ProfileSnapshot
) -> ProfileSnapshot:
    data = primary.model_dump()
    extra_notes: list[str] = list(data.get("notes") or [])
    extra_lazy: list[str] = list(data.get("lazy_component_ids") or [])
    for other in others:
        dumped = other.model_dump()
        extra_notes.extend(dumped.get("notes") or [])
        extra_lazy.extend(dumped.get("lazy_component_ids") or [])
        data["experience"] = _merge_experience(
            data.get("experience") or [], dumped.get("experience") or []
        )
        for list_key, fields in _LIST_MERGE_KEYS.items():
            data[list_key] = _merge_named_list(
                data.get(list_key) or [], dumped.get(list_key) or [], fields
            )
        for key, value in dumped.items():
            if key in _SKIP_MERGE or key in (
                "notes",
                "lazy_component_ids",
                "experience",
                *_LIST_MERGE_KEYS,
            ):
                continue
            current = data.get(key)
            if key == "photo_url":
                if _incomplete_photo(current) and not _incomplete_photo(value):
                    data[key] = value
                continue
            if current in (None, "", []):
                if value not in (None, "", []):
                    data[key] = value
    data["notes"] = list(dict.fromkeys(extra_notes))
    data["lazy_component_ids"] = sorted(set(extra_lazy))
    sources = [primary.source] + [o.source for o in others]
    data["source"] = "+".join(dict.fromkeys(sources))
    return dedupe_snapshot(ProfileSnapshot.model_validate(data))


def _fold(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().casefold()


def dedupe_snapshot(snapshot: ProfileSnapshot) -> ProfileSnapshot:
    """Drop near-duplicate list rows. Experience key is (title, company), not dates."""
    data = snapshot.model_dump()

    seen_exp: set[tuple[str, str]] = set()
    experience: list[dict] = []
    for item in data.get("experience") or []:
        key = (_fold(item.get("title")), _fold(item.get("company")))
        if key in seen_exp:
            continue
        seen_exp.add(key)
        experience.append(item)
    data["experience"] = experience

    seen_edu: set[tuple[str, str, str]] = set()
    education: list[dict] = []
    for item in data.get("education") or []:
        key = (
            _fold(item.get("school")),
            _fold(item.get("degree")),
            _fold(item.get("field_of_study")),
        )
        if key in seen_edu:
            continue
        seen_edu.add(key)
        education.append(item)
    data["education"] = education

    seen_skills: set[str] = set()
    skills: list[dict] = []
    for item in data.get("skills") or []:
        key = _fold(item.get("name"))
        if not key or key in seen_skills:
            continue
        seen_skills.add(key)
        skills.append(item)
    data["skills"] = skills

    seen_certs: set[str] = set()
    certifications: list[dict] = []
    for item in data.get("certifications") or []:
        key = _fold(item.get("name"))
        if not key or key in seen_certs:
            continue
        seen_certs.add(key)
        certifications.append(item)
    data["certifications"] = certifications

    seen_langs: set[str] = set()
    languages: list[dict] = []
    for item in data.get("languages") or []:
        key = _fold(item.get("name"))
        if not key or key in seen_langs:
            continue
        seen_langs.add(key)
        languages.append(item)
    data["languages"] = languages

    return ProfileSnapshot.model_validate(data)
