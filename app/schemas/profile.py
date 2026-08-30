"""Pydantic models for normalized profile snapshots."""

from pydantic import BaseModel, Field


class ExperienceItem(BaseModel):
    title: str | None = None
    company: str | None = None
    date_range: str | None = None
    location: str | None = None
    workplace_type: str | None = None


class EducationItem(BaseModel):
    school: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    date_range: str | None = None
    location: str | None = None


class CertificationItem(BaseModel):
    name: str | None = None
    issuer: str | None = None
    issue_date: str | None = None
    date_range: str | None = None


class SkillItem(BaseModel):
    name: str | None = None


class LanguageItem(BaseModel):
    name: str | None = None
    proficiency: str | None = None


class ProfileSnapshot(BaseModel):
    """Fields we can extract from flagship-web SDUI first-paint or HTML rehydration."""

    vanity_name: str | None = None
    full_name: str | None = None
    dash_profile_id: str | None = None
    member_urn: str | None = None
    headline: str | None = None
    location: str | None = None
    about: str | None = None
    photo_url: str | None = None
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    skills: list[SkillItem] = Field(default_factory=list)
    certifications: list[CertificationItem] = Field(default_factory=list)
    languages: list[LanguageItem] = Field(default_factory=list)
    is_oops: bool | None = None
    screen_id: str | None = None
    lazy_component_ids: list[str] = Field(default_factory=list)
    source: str = "flagship-sdui"
    notes: list[str] = Field(default_factory=list)


Education = EducationItem
Certification = CertificationItem
Language = LanguageItem
