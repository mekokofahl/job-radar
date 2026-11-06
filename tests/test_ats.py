import unittest
from unittest import mock

import ats

REQUIRED = {"id", "title", "location", "workplace", "remote", "url", "posted_at", "description"}


class StripHtml(unittest.TestCase):
    def test_escaped_html_becomes_plain_text(self):
        out = ats.strip_html("&lt;p&gt;Hello&lt;br&gt;World&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;")
        self.assertIn("Hello", out)
        self.assertIn("World", out)
        self.assertNotIn("<", out)

    def test_empty_input(self):
        self.assertEqual(ats.strip_html(None), "")
        self.assertEqual(ats.strip_html(""), "")


class Fetchers(unittest.TestCase):
    """Each reader turns a company's public job feed into the same normalized shape."""

    def run_fetcher(self, fetcher, payload):
        with mock.patch.object(ats, "_get", return_value=payload):
            return fetcher("example")

    def check_shape(self, jobs):
        self.assertTrue(jobs)
        for j in jobs:
            self.assertTrue(REQUIRED <= set(j), j)

    def test_greenhouse(self):
        jobs = self.run_fetcher(ats.greenhouse, {"jobs": [{"id": 7, "title": "Delivery Manager", "location": {"name": "Remote - Europe"},
                                                            "absolute_url": "https://boards.greenhouse.io/x/jobs/7", "updated_at": "2026-11-01T10:00:00Z",
                                                            "content": "&lt;p&gt;Lead customer deployments.&lt;/p&gt;"}]})
        self.check_shape(jobs)
        self.assertEqual((jobs[0]["id"], jobs[0]["title"], jobs[0]["location"]), ("7", "Delivery Manager", "Remote - Europe"))
        self.assertIn("customer deployments", jobs[0]["description"])

    def test_lever_joins_locations_and_reads_workplace(self):
        jobs = self.run_fetcher(ats.lever, [{"id": "abc", "text": "Implementation Manager", "workplaceType": "remote",
                                             "categories": {"allLocations": ["Berlin", "Remote"]}, "hostedUrl": "https://jobs.lever.co/x/abc",
                                             "createdAt": 1793000000000, "descriptionPlain": "Enterprise onboarding.",
                                             "salaryRange": {"currency": "EUR", "min": 90000, "max": 110000, "interval": "per-year-salary"}}])
        self.check_shape(jobs)
        self.assertEqual(jobs[0]["location"], "Berlin; Remote")
        self.assertTrue(jobs[0]["remote"])
        self.assertIn("110000", jobs[0]["description"])

    def test_lever_rejects_an_unexpected_response(self):
        with self.assertRaises(ValueError):
            self.run_fetcher(ats.lever, {"error": "not a list"})

    def test_ashby_adds_secondary_locations_and_compensation(self):
        jobs = self.run_fetcher(ats.ashby, {"jobs": [{"id": "u1", "title": "Technical Program Manager", "location": "London",
                                                       "secondaryLocations": [{"location": "Dublin"}], "workplaceType": "Hybrid", "isRemote": False,
                                                       "jobUrl": "https://jobs.ashbyhq.com/x/u1", "publishedAt": "2026-11-01T00:00:00Z",
                                                       "descriptionPlain": "Programs.", "compensation": {"compensationTierSummary": "EUR 100K - 120K"}}]})
        self.check_shape(jobs)
        self.assertEqual(jobs[0]["location"], "London; Dublin")
        self.assertIn("EUR 100K - 120K", jobs[0]["description"])

    def test_workable_builds_a_location_from_parts(self):
        jobs = self.run_fetcher(ats.workable, {"jobs": [{"shortcode": "AB12", "title": "Program Manager", "city": "Lisbon", "country": "Portugal",
                                                          "telecommuting": False, "url": "https://apply.workable.com/x/j/AB12/",
                                                          "description": "&lt;p&gt;Run programs.&lt;/p&gt;", "published_on": "2026-11-01"}]})
        self.check_shape(jobs)
        self.assertEqual(jobs[0]["location"], "Lisbon, Portugal")
        self.assertIsNone(jobs[0]["remote"])

    def test_recruitee_ignores_unpublished_offers_and_uses_explicit_flags(self):
        payload = {"offers": [
            {"id": 1, "status": "published", "title": "Delivery Lead", "location": "Amsterdam", "hybrid": True, "remote": True,
             "careers_url": "https://x.recruitee.com/o/1", "description": "Lead.", "requirements": "Experience."},
            {"id": 2, "status": "draft", "title": "Hidden", "location": "Berlin"}]}
        jobs = self.run_fetcher(ats.recruitee, payload)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["workplace"], "hybrid")      # the boolean "remote" alone is not trusted

    def test_every_supported_board_has_a_fetcher(self):
        self.assertEqual(set(ats.FETCHERS), {"greenhouse", "lever", "lever_eu", "ashby", "workable", "recruitee"})


class Retries(unittest.TestCase):
    def test_temporary_server_errors_are_retried_then_succeed(self):
        class Resp:
            def __init__(self, code, body=None):
                self.status_code, self._b = code, body or {}
            def json(self):
                return self._b
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise ats.requests.HTTPError(str(self.status_code), response=self)
        calls = []
        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return Resp(503) if len(calls) < 3 else Resp(200, {"jobs": []})
        fake_session = mock.Mock()
        fake_session.get = fake_get
        with mock.patch.object(ats, "_session", return_value=fake_session), mock.patch.object(ats.time, "sleep"):
            self.assertEqual(ats._get("https://example.com/feed"), {"jobs": []})
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
