"""Pager-section normalizers against saved RSC dumps (no live fetch)."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.linkedin.pager_normalize import (
    extract_about_text,
    extract_pager_continuation,
    normalize_certifications_pager,
    normalize_education_pager,
    normalize_languages_pager,
    normalize_skills_pager,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class EducationPagerNormalizerTests(unittest.TestCase):
    def test_schools_and_fields(self):
        raw = (FIXTURES / "education_pager_v2.txt").read_bytes()
        items = normalize_education_pager(raw)
        schools = [item.school for item in items]
        self.assertIn("Dr. Babasaheb Ambedkar Technological University", schools)
        self.assertTrue(
            any(
                school is not None
                and school.startswith("KARMVEER BHAURAO PATIL MAHAVIDYALYA")
                for school in schools
            )
        )
        babasaheb = [
            item
            for item in items
            if item.school == "Dr. Babasaheb Ambedkar Technological University"
        ]
        self.assertGreaterEqual(len(babasaheb), 1)
        self.assertEqual(extract_pager_continuation(raw), None)
        degrees = {item.degree for item in items}
        self.assertTrue(
            "Bachelor's degree" in degrees or "Bachelor of Technology" in degrees
        )


class SkillsPagerNormalizerTests(unittest.TestCase):
    def test_first_page_skill_names(self):
        raw = (FIXTURES / "skills_pager_v2.txt").read_bytes()
        names = normalize_skills_pager(raw)
        for expected in (
            "Cloudwan",
            "Nework security",
            "Network Services",
            "Network Systems",
            "Checkpoint",
        ):
            self.assertIn(expected, names)
        self.assertNotIn("Assistant Manager at Example Inc", names)
        self.assertNotIn("1 endorsement", names)
        self.assertEqual(len(names), 10)
        continuation = extract_pager_continuation(raw)
        self.assertIsNotNone(continuation)
        assert continuation is not None
        payload = (continuation.get("requestedArguments") or {}).get("payload") or {}
        self.assertEqual(payload.get("start"), 10)
        self.assertEqual(payload.get("count"), 10)

    def test_merge_multiple_pages_dedupes(self):
        raw = (FIXTURES / "skills_pager_v2.txt").read_bytes()
        merged = normalize_skills_pager([raw, raw])
        self.assertEqual(len(merged), 10)
        self.assertEqual(merged, normalize_skills_pager(raw))


class CertificationsPagerNormalizerTests(unittest.TestCase):
    def test_ccna_cisco(self):
        raw = (FIXTURES / "certifications_pager_v2.txt").read_bytes()
        items = normalize_certifications_pager(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "CCNA")
        self.assertEqual(items[0].issuer, "Cisco")
        self.assertIsNone(items[0].issue_date)
        self.assertEqual(extract_pager_continuation(raw), None)


class LanguagesPagerNormalizerTests(unittest.TestCase):
    def test_three_languages_no_proficiency(self):
        raw = (FIXTURES / "languages_pager_v2.txt").read_bytes()
        items = normalize_languages_pager(raw)
        names = [item.name for item in items]
        self.assertEqual(len(items), 3)
        self.assertEqual(names, ["English", "Hindi", "marathi"])
        self.assertTrue(all(item.proficiency is None for item in items))
        self.assertEqual(extract_pager_continuation(raw), None)


class AboutComponentNormalizerTests(unittest.TestCase):
    def test_about_paragraphs(self):
        raw = (FIXTURES / "about_component.txt").read_bytes()
        about = extract_about_text(raw)
        self.assertIsNotNone(about)
        assert about is not None
        self.assertIn("spent my career leading global businesses", about)
        self.assertIn("Quantum Marketing", about)
        self.assertIn("Mastercard", about)
        self.assertNotIn("expandable_text_block", about)
        self.assertNotIn("profile-card-about", about)
        self.assertNotIn("$35", about)
        self.assertGreater(about.count("\n\n"), 2)


if __name__ == "__main__":
    unittest.main()
