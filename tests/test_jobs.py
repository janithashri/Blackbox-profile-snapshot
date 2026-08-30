import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.linkedin.errors import ClassifiedResponse, ResponseClass
from app.linkedin.public_id import PublicIdError, parse_linkedin_public_id
from app.main import app
from app.schemas.profile import ExperienceItem, ProfileSnapshot
from app.services.jobs import compute_missing_fields, reset_runtime_state, start_profile_job


class ParsePublicIdTests(unittest.TestCase):
    def test_vanity_and_url(self):
        self.assertEqual(parse_linkedin_public_id("jane-doe"), "jane-doe")
        self.assertEqual(
            parse_linkedin_public_id("https://www.linkedin.com/in/jane-doe/"),
            "jane-doe",
        )
        self.assertEqual(
            parse_linkedin_public_id("https://www.linkedin.com/in/ada-lovelace?trk=x"),
            "ada-lovelace",
        )

    def test_rejects_empty(self):
        with self.assertRaises(PublicIdError):
            parse_linkedin_public_id("   ")


class JobApiTests(unittest.TestCase):
    def setUp(self):
        reset_runtime_state()

    def test_post_profiles_then_poll_done(self):
        snap = ProfileSnapshot(
            vanity_name="ada-lovelace",
            full_name="Ada Lovelace",
            source="flagship-html",
        )
        classified = ClassifiedResponse(
            ResponseClass.OK, 200, "", "ok", 100
        )
        with (
            patch("app.services.jobs.settings") as mock_settings,
            patch(
                "app.services.jobs.fetch_live_profile",
                new=AsyncMock(return_value=(snap, classified)),
            ),
        ):
            mock_settings.LINKEDIN_LI_AT = "test"
            mock_settings.LINKEDIN_JSESSIONID = "ajax:1"
            mock_settings.LINKEDIN_LI_AT_PRIMARY = ""
            mock_settings.LINKEDIN_JSESSIONID_PRIMARY = ""
            mock_settings.LINKEDIN_LI_AT_SECONDARY = ""
            mock_settings.LINKEDIN_JSESSIONID_SECONDARY = ""
            client = TestClient(app)
            created = client.post(
                "/profiles", json={"linkedin_url_or_id": "https://www.linkedin.com/in/ada-lovelace/"}
            )
            self.assertEqual(created.status_code, 200)
            job_id = created.json()["job_id"]
            polled = client.get(f"/jobs/{job_id}")
            body = polled.json()
            self.assertEqual(body["status"], "done")
            self.assertEqual(body["result"]["source"], "flagship-html")
            self.assertIn("fetched_at", body["result"])
            self.assertIn("missing_fields", body["result"])
            self.assertFalse(body["cached"])

    def test_second_post_same_profile_is_cache_hit(self):
        snap = ProfileSnapshot(
            vanity_name="ada-lovelace",
            full_name="Ada Lovelace",
            source="flagship-html",
        )
        classified = ClassifiedResponse(ResponseClass.OK, 200, "", "ok", 100)
        with (
            patch("app.services.jobs.settings") as mock_settings,
            patch(
                "app.services.jobs.fetch_live_profile",
                new=AsyncMock(return_value=(snap, classified)),
            ) as mock_fetch,
        ):
            mock_settings.LINKEDIN_LI_AT = "test"
            mock_settings.LINKEDIN_JSESSIONID = "ajax:1"
            mock_settings.LINKEDIN_LI_AT_PRIMARY = ""
            mock_settings.LINKEDIN_JSESSIONID_PRIMARY = ""
            mock_settings.LINKEDIN_LI_AT_SECONDARY = ""
            mock_settings.LINKEDIN_JSESSIONID_SECONDARY = ""
            client = TestClient(app)
            first = client.post(
                "/profiles", json={"linkedin_url_or_id": "ada-lovelace"}
            )
            job_id = first.json()["job_id"]
            done = client.get(f"/jobs/{job_id}").json()
            self.assertEqual(done["status"], "done")
            self.assertFalse(done["cached"])
            second = client.post(
                "/profiles", json={"linkedin_url_or_id": "ada-lovelace"}
            )
            self.assertEqual(second.json()["status"], "done")
            self.assertTrue(second.json()["cached"])
            self.assertEqual(second.json()["result"]["full_name"], "Ada Lovelace")
            self.assertEqual(mock_fetch.await_count, 1)

    def test_inflight_job_is_reused(self):
        first, launched = start_profile_job("ada-lovelace")
        self.assertTrue(launched)
        first.status = "running"
        again, launched_again = start_profile_job(
            "https://www.linkedin.com/in/ada-lovelace/"
        )
        self.assertFalse(launched_again)
        self.assertEqual(again.job_id, first.job_id)

    def test_session_rejected_primary_retries_secondary(self):
        snap = ProfileSnapshot(vanity_name="ada-lovelace", full_name="Ada Lovelace")
        rejected = ClassifiedResponse(
            ResponseClass.SESSION_REJECTED, 999, "", "linkedin_999", 10
        )
        ok = ClassifiedResponse(ResponseClass.OK, 200, "", "ok", 100)

        async def fake_fetch(public_id, li_at, jsessionid):
            if li_at == "invalid-primary":
                return None, rejected
            if li_at == "secondary-ok":
                return snap, ok
            self.fail(f"unexpected cookies: {li_at!r}")

        with (
            patch("app.services.jobs.settings") as mock_settings,
            patch(
                "app.services.jobs.fetch_live_profile",
                new=AsyncMock(side_effect=fake_fetch),
            ) as mock_fetch,
        ):
            mock_settings.LINKEDIN_LI_AT = ""
            mock_settings.LINKEDIN_JSESSIONID = ""
            mock_settings.LINKEDIN_LI_AT_PRIMARY = "invalid-primary"
            mock_settings.LINKEDIN_JSESSIONID_PRIMARY = "ajax:primary"
            mock_settings.LINKEDIN_LI_AT_SECONDARY = "secondary-ok"
            mock_settings.LINKEDIN_JSESSIONID_SECONDARY = "ajax:secondary"
            client = TestClient(app)
            created = client.post(
                "/profiles", json={"linkedin_url_or_id": "ada-lovelace"}
            )
            job_id = created.json()["job_id"]
            body = client.get(f"/jobs/{job_id}").json()
            self.assertEqual(body["status"], "done")
            self.assertEqual(mock_fetch.await_count, 2)
            self.assertEqual(mock_fetch.await_args_list[0].args[1], "invalid-primary")
            self.assertEqual(mock_fetch.await_args_list[1].args[1], "secondary-ok")

    def test_session_rejected_without_secondary_fails(self):
        rejected = ClassifiedResponse(
            ResponseClass.SESSION_REJECTED, 999, "", "linkedin_999", 10
        )
        with (
            patch("app.services.jobs.settings") as mock_settings,
            patch(
                "app.services.jobs.fetch_live_profile",
                new=AsyncMock(return_value=(None, rejected)),
            ) as mock_fetch,
        ):
            mock_settings.LINKEDIN_LI_AT = "only-one"
            mock_settings.LINKEDIN_JSESSIONID = "ajax:1"
            mock_settings.LINKEDIN_LI_AT_PRIMARY = ""
            mock_settings.LINKEDIN_JSESSIONID_PRIMARY = ""
            mock_settings.LINKEDIN_LI_AT_SECONDARY = ""
            mock_settings.LINKEDIN_JSESSIONID_SECONDARY = ""
            client = TestClient(app)
            created = client.post(
                "/profiles", json={"linkedin_url_or_id": "ada-lovelace"}
            )
            body = client.get(f"/jobs/{created.json()['job_id']}").json()
            self.assertEqual(body["status"], "failed")
            self.assertEqual(body["error"]["code"], "session_rejected")
            self.assertEqual(mock_fetch.await_count, 1)

    def test_missing_fields_flags_incomplete_experience(self):
        data = ProfileSnapshot(
            full_name="Ada",
            headline="Mathematician",
            location="London",
            experience=[
                ExperienceItem(date_range="Jan 2020 - Present", title=None, company=None)
            ],
        ).model_dump(mode="json")
        missing = compute_missing_fields(data)
        self.assertIn("experience[1/1]_title_or_company", missing)
        self.assertNotIn("full_name", missing)

    def test_index_is_html_console(self):
        client = TestClient(app)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fetch details", response.text)
        self.assertIn("LinkedIn Profile Scraper", response.text)
        self.assertIn("Tross take home challenge", response.text)
        self.assertIn('id="raw-section"', response.text)
        self.assertIn("const rawSection", response.text)
        self.assertIn("Fetching... (", response.text)
        self.assertIn('id="photo-placeholder"', response.text)
        self.assertIn("No photo", response.text)
        self.assertIn("Raw JSON (debug)", response.text)
        self.assertIn("<details class=\"section\"", response.text)
        self.assertIn("Experience (", response.text)
        self.assertIn("id=\"sec-experience\" open", response.text)
        self.assertIn("id=\"sec-skills\"", response.text)
        self.assertNotIn("id=\"sec-skills\" open", response.text)


if __name__ == "__main__":
    unittest.main()
