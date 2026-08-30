import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.linkedin.client import (
    FlagshipDetailSection,
    _prefer_pagination_request,
    fetch_live_profile,
)
from app.linkedin.errors import ResponseClass, classify_response
from app.linkedin.merge import merge_snapshots
from app.linkedin.normalizer import extract_pagination_requests, normalize_sdui_profile, parse_rsc_chunks
from app.schemas.profile import ExperienceItem, ProfileSnapshot, SkillItem

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE_HTML = (FIXTURES / "mini_profile.html").read_text(encoding="utf-8")
EXPERIENCE_HTML = (FIXTURES / "mini_experience.html").read_text(encoding="utf-8")


class ClassifyResponseTests(unittest.TestCase):
    def test_307_uas_login_is_session_rejected(self):
        response = httpx.Response(
            307,
            headers={"location": "https://www.linkedin.com/uas/login"},
            request=httpx.Request("POST", "https://www.linkedin.com/flagship-web/in/x/"),
        )
        classified = classify_response(response)
        self.assertEqual(classified.kind, ResponseClass.SESSION_REJECTED)
        self.assertEqual(classified.reason, "login_redirect")

    def test_999_is_session_rejected(self):
        response = httpx.Response(
            999,
            content=b"x" * 1530,
            request=httpx.Request("GET", "https://www.linkedin.com/in/x/"),
        )
        classified = classify_response(response)
        self.assertEqual(classified.kind, ResponseClass.SESSION_REJECTED)
        self.assertEqual(classified.reason, "linkedin_999")

    def test_thin_200_without_payload_is_rejected(self):
        response = httpx.Response(
            200,
            content=b"<html>challenge</html>",
            request=httpx.Request("GET", "https://www.linkedin.com/in/x/"),
        )
        classified = classify_response(response)
        self.assertEqual(classified.kind, ResponseClass.SESSION_REJECTED)
        self.assertEqual(classified.reason, "thin_or_challenge_body")

    def test_small_html_with_rehydration_is_ok(self):
        response = httpx.Response(
            200,
            content=PROFILE_HTML.encode("utf-8"),
            request=httpx.Request("GET", "https://www.linkedin.com/in/ada-lovelace/"),
        )
        classified = classify_response(response)
        self.assertEqual(classified.kind, ResponseClass.OK)


class NormalizeCaptureTests(unittest.TestCase):
    def test_html_rehydration_extracts_name_headline_and_dates(self):
        snap = normalize_sdui_profile(PROFILE_HTML)
        self.assertEqual(snap.vanity_name, "ada-lovelace")
        self.assertEqual(snap.full_name, "Ada Lovelace")
        self.assertEqual(snap.dash_profile_id, "ACoAAAdaLovelaceExampleId0001")
        self.assertEqual(snap.headline, "Mathematician and writer")
        self.assertEqual(snap.source, "flagship-html")
        self.assertEqual(len(snap.experience), 1)
        self.assertIn("Jan 2020 - Present", snap.experience[0].date_range or "")

    def test_sanitize_strips_follow_invite_connect_chrome(self):
        from app.linkedin.merge import dedupe_snapshot
        from app.linkedin.names import sanitize_full_name

        self.assertEqual(sanitize_full_name("Ada Lovelace Follow"), "Ada Lovelace")
        self.assertEqual(sanitize_full_name("Follow Ada Lovelace"), "Ada Lovelace")
        self.assertEqual(
            sanitize_full_name("Invite Ada Lovelace to connect"), "Ada Lovelace"
        )
        self.assertEqual(sanitize_full_name("Ada Lovelace | LinkedIn"), "Ada Lovelace")
        cleaned = dedupe_snapshot(
            ProfileSnapshot(full_name="Connect Ada Lovelace Message")
        )
        self.assertEqual(cleaned.full_name, "Ada Lovelace")

    def test_experience_html_extracts_title_and_company(self):
        snap = normalize_sdui_profile(EXPERIENCE_HTML)
        self.assertEqual(len(snap.experience), 1)
        self.assertEqual(snap.experience[0].title, "Mathematician")
        self.assertEqual(snap.experience[0].company, "Analytical Engines Ltd")
        self.assertIn("Jan 2020 - Present", snap.experience[0].date_range or "")

    def test_duration_is_not_used_as_company_and_group_header_carries(self):
        raw = (
            'a0:["$","p",null,{"style":{"x":"1"},"children":["Acme Group"]}]\n'
            'a1:["$","p",null,{"children":["2 yrs 8 mos"]}]\n'
            'a2:["$","p",null,{"style":{"x":"1"},"children":["Manager"]}]\n'
            'a3:["$","$Lae",null,{"textProps":{"children":["Jul 2021 - Jul 2022 · 1 yr 1 mo"]}}]\n'
            'a4:{"children":[["$","div",null,{"children":["$La2"]}],"$La3"]}\n'
            'a5:["$","p",null,{"style":{"x":"1"},"children":["Principal Engineer"]}]\n'
            'a6:["$","p",null,{"children":["Example Corp · Full-time"]}]\n'
            'a7:{"pageKey":"d_flagship3_profile_view_base_position_details"}\n'
        )
        snap = normalize_sdui_profile(raw)
        companies = [item.company for item in snap.experience]
        titles = [item.title for item in snap.experience]
        self.assertNotIn("2 yrs 8 mos", companies)
        self.assertIn("Manager", titles)
        manager = next(item for item in snap.experience if item.title == "Manager")
        self.assertEqual(manager.company, "Acme Group")
        cloud = next(
            item
            for item in snap.experience
            if item.title == "Principal Engineer"
        )
        self.assertEqual(cloud.company, "Example Corp")
        self.assertEqual(cloud.workplace_type, "Full-time")

    def test_dedupe_experience_by_title_and_company_keeps_one_row(self):
        from app.linkedin.merge import dedupe_snapshot

        snap = ProfileSnapshot(
            full_name="Sample User",
            experience=[
                ExperienceItem(
                    title="Network Security Engineer",
                    company="Example Corp",
                    date_range="Jan 2018 - Dec 2019 · 2 yrs",
                ),
                ExperienceItem(
                    title="Network Security Engineer",
                    company="Example Corp",
                    date_range=None,
                ),
            ],
            skills=[SkillItem(name="Networking"), SkillItem(name="networking")],
        )
        cleaned = dedupe_snapshot(snap)
        self.assertEqual(len(cleaned.experience), 1)
        self.assertEqual(cleaned.experience[0].company, "Example Corp")
        self.assertEqual(len(cleaned.skills), 1)

    def test_photo_url_prefers_complete_shrink_size_over_truncated_root(self):
        from app.linkedin.normalizer import extract_photo_url

        blob = (
            'https://media.licdn.com/dms/image/v2/D4E35AQH7t1cod_1eMg/profile-framedphoto-shrink_"'
            " https://media.licdn.com/dms/image/v2/D4E35AQH7t1cod_1eMg/"
            "profile-framedphoto-shrink_100_100/B4EZ4.W.JKHQAk-/0/1779162670051?e=1788598800&v=beta&t=abc"
        )
        url = extract_photo_url(blob)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("shrink_100_100", url)
        self.assertTrue(url.endswith("t=abc"))
        self.assertFalse(url.endswith("shrink_"))

    def test_photo_url_joins_root_url_and_suffix_url(self):
        from app.linkedin.normalizer import extract_photo_url

        blob = (
            '{"rootUrl":"https://media.licdn.com/dms/image/v2/D4E35AQH7t1cod_1eMg/'
            'profile-framedphoto-shrink_","imageRenditions":['
            '{"width":800,"height":800,"suffixUrl":"800_800/B4EZ/0/1?e=9&v=beta&t=tok"}]}'
        )
        url = extract_photo_url(blob)
        self.assertEqual(
            url,
            "https://media.licdn.com/dms/image/v2/D4E35AQH7t1cod_1eMg/"
            "profile-framedphoto-shrink_800_800/B4EZ/0/1?e=9&v=beta&t=tok",
        )

    def test_photo_url_displayphoto_scale(self):
        from app.linkedin.normalizer import extract_photo_url

        blob = (
            'src="https://media.licdn.com/dms/image/v2/D5603AQFi9G4R0Nebug/'
            'profile-displayphoto-scale_400_400/B56Z/0/1?e=9&amp;v=beta&amp;t=tok"'
        )
        url = extract_photo_url(blob)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("scale_400_400", url)
        self.assertIn("t=tok", url)

    def test_photo_url_none_when_only_truncated_root(self):
        from app.linkedin.normalizer import extract_photo_url

        blob = (
            '"rootUrl":"https://media.licdn.com/dms/image/v2/xx/'
            'profile-framedphoto-shrink_"'
        )
        self.assertIsNone(extract_photo_url(blob))

    def test_merge_replaces_truncated_photo_url(self):
        primary = ProfileSnapshot(
            full_name="Ada",
            photo_url="https://media.licdn.com/dms/image/v2/x/profile-framedphoto-shrink_",
            source="flagship-sdui",
        )
        extra = ProfileSnapshot(
            photo_url=(
                "https://media.licdn.com/dms/image/v2/x/"
                "profile-framedphoto-shrink_800_800/a/0/1?e=9&t=tok"
            ),
            source="flagship-html",
        )
        merged = merge_snapshots(primary, extra)
        self.assertIn("shrink_800_800", merged.photo_url or "")

    def test_merge_fills_experience_from_second_snapshot(self):
        primary = ProfileSnapshot(full_name="Ada Lovelace", source="flagship-html")
        extra = normalize_sdui_profile(PROFILE_HTML)
        merged = merge_snapshots(primary, extra)
        self.assertEqual(merged.full_name, "Ada Lovelace")
        self.assertEqual(len(merged.experience), 1)
        self.assertIn("html", merged.source)

    def test_education_section_maps_cards_to_school(self):
        snap = normalize_sdui_profile(EXPERIENCE_HTML, section="education")
        self.assertEqual(len(snap.education), 1)
        self.assertEqual(snap.education[0].school, "Mathematician")
        self.assertEqual(snap.education[0].degree, "Analytical Engines Ltd")
        self.assertEqual(snap.experience, [])

    def test_skills_ignore_footer_chrome(self):
        from app.linkedin.normalizer import extract_skill_items

        raw = (
            'a0:["$","p",null,{"children":["Privacy Policy"]}]\n'
            'a1:["$","p",null,{"children":["React"]}]\n'
        )
        skills = extract_skill_items(parse_rsc_chunks(raw))
        self.assertEqual([item.name for item in skills], ["React"])

    def test_extract_pagination_requests_from_rsc(self):
        raw = (
            '52:{"items":[],"nextPageRequest":{"$type":"proto.sdui.actions.requests.PaginationRequest",'
            '"pagerId":"com.linkedin.sdui.pagers.profile.details.education",'
            '"requestedArguments":{"payload":{"vanityName":"ada-lovelace","start":0,"count":10}}}}\n'
        )
        requests = extract_pagination_requests(parse_rsc_chunks(raw))
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0]["pagerId"],
            "com.linkedin.sdui.pagers.profile.details.education",
        )

    def test_prefer_profile_details_pager_not_feed(self):
        section = FlagshipDetailSection(
            "education",
            "com.linkedin.sdui.flagshipnav.profile.ProfileEducationDetails",
            "profile_view_base_education_details",
            "education_raw",
            "com.linkedin.sdui.pagers.profile.details.education",
        )
        chosen = _prefer_pagination_request(
            section,
            [
                {"pagerId": "com.linkedin.sdui.pagers.feed.mainFeed"},
                {
                    "pagerId": "com.linkedin.sdui.pagers.profile.details.education",
                    "requestedArguments": {"payload": {"start": 0}},
                },
            ],
        )
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertIn("profile.details.education", chosen["pagerId"])

    def test_section_pager_body_is_wrapper_not_bare_request(self):
        from app.linkedin.client import EDUCATION_PAGER_ID, section_pager_body

        body = section_pager_body(
            "sample-user",
            "ACoAASampleProfileIdPlaceholder00001",
            EDUCATION_PAGER_ID,
            "com.linkedin.sdui.flagshipnav.profile.ProfileEducationDetails",
        )
        self.assertEqual(body["pagerId"], EDUCATION_PAGER_ID)
        self.assertNotIn("onClientError", body)
        self.assertNotIn("onClientError", body["paginationRequest"])
        payload = body["clientArguments"]["payload"]
        self.assertEqual(payload["start"], 0)
        self.assertEqual(payload["count"], 10)
        self.assertEqual(
            payload,
            body["paginationRequest"]["requestedArguments"]["payload"],
        )
        self.assertEqual(
            body["clientArguments"]["screenId"],
            "com.linkedin.sdui.flagshipnav.profile.ProfileEducationDetails",
        )
        self.assertNotIn("screenId", body["paginationRequest"]["requestedArguments"])
        skills = section_pager_body(
            "rajshamani",
            "ACoAAExample",
            "com.linkedin.sdui.pagers.profile.details.skills",
            "com.linkedin.sdui.flagshipnav.profile.ProfileSkillDetails",
            extra_payload={"filter": "ProfileSkillCategory_ALL"},
        )
        self.assertEqual(
            skills["clientArguments"]["payload"]["filter"],
            "ProfileSkillCategory_ALL",
        )


class FetchLiveProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_fallback_then_experience_merge(self):
        post = httpx.Response(
            307,
            headers={"location": "/uas/login"},
            request=httpx.Request(
                "POST", "https://www.linkedin.com/flagship-web/in/ada-lovelace/"
            ),
        )
        html = httpx.Response(
            200,
            content=PROFILE_HTML.encode("utf-8"),
            request=httpx.Request(
                "GET", "https://www.linkedin.com/in/ada-lovelace/"
            ),
        )
        experience = httpx.Response(
            200,
            content=EXPERIENCE_HTML.encode("utf-8"),
            request=httpx.Request(
                "GET",
                "https://www.linkedin.com/in/ada-lovelace/details/experience/",
            ),
        )
        with (
            patch(
                "app.linkedin.client.fetch_profile_raw",
                new=AsyncMock(return_value=post),
            ),
            patch(
                "app.linkedin.client.fetch_profile_html",
                new=AsyncMock(return_value=html),
            ),
            patch(
                "app.linkedin.client.fetch_experience_html",
                new=AsyncMock(return_value=experience),
            ),
            patch(
                "app.linkedin.client.enrich_profile_detail_sections",
                new=AsyncMock(side_effect=lambda snap, *args, **kwargs: snap),
            ),
            patch(
                "app.linkedin.client.enrich_about",
                new=AsyncMock(side_effect=lambda snap, *args, **kwargs: snap),
            ),
            patch("app.linkedin.client.record_classified"),
        ):
            snapshot, classified = await fetch_live_profile("ada-lovelace", "li", "ajax:1")
        self.assertEqual(classified.kind, ResponseClass.OK)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.full_name, "Ada Lovelace")
        self.assertGreaterEqual(len(snapshot.experience), 1)
        self.assertEqual(snapshot.experience[0].title, "Mathematician")
        self.assertEqual(snapshot.experience[0].company, "Analytical Engines Ltd")
        self.assertTrue(any("live_path:profile_html_get" in note for note in snapshot.notes))
        self.assertTrue(any("live_path:experience_html_get" in note for note in snapshot.notes))


if __name__ == "__main__":
    unittest.main()
