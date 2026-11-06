import unittest
from unittest import mock

import watchlist


class Resp:
    def __init__(self, url, text):
        self.url, self.text = url, text


class BoardDetection(unittest.TestCase):
    def detect(self, html, url="https://example.com/careers"):
        with mock.patch.object(watchlist.requests, "get", return_value=Resp(url, html)):
            return watchlist.detect_from_url(url)

    def test_links_to_each_supported_board_are_recognised(self):
        cases = {
            "greenhouse": "<a href='https://job-boards.greenhouse.io/acme/jobs/1'>x</a>",
            "lever": "<a href='https://jobs.lever.co/acme/abc'>x</a>",
            "ashby": "<a href='https://jobs.ashbyhq.com/acme/uuid'>x</a>",
            "workable": "<a href='https://apply.workable.com/acme/j/AB12/'>x</a>",
            "recruitee": "<a href='https://acme.recruitee.com/o/job'>x</a>",
        }
        for name, html in cases.items():
            self.assertEqual(self.detect(html), (name, "acme"), name)

    def test_board_hidden_in_javascript_is_found_in_the_script_files(self):
        pages = {
            "https://example.com/careers": '<script src="/static/careers.js"></script>',
            "https://example.com/static/careers.js": 'fetch("https://api.ashbyhq.com/posting-api/job-board/acme-co")',
        }
        def fake_get(url, headers=None, timeout=None):
            return Resp(url, pages[url])
        with mock.patch.object(watchlist.requests, "get", side_effect=fake_get):
            self.assertEqual(watchlist.detect_from_url("https://example.com/careers"), ("ashby", "acme-co"))

    def test_page_language_codes_are_not_mistaken_for_companies(self):
        self.assertIsNone(self.detect("<a href='https://job-boards.greenhouse.io/en-GB/jobs'>x</a>"))

    def test_a_page_with_no_board_returns_nothing(self):
        self.assertIsNone(self.detect("<h1>Careers</h1>"))


class PlatformReport(unittest.TestCase):
    def test_unsupported_boards_are_named_so_you_can_decide_what_to_build(self):
        cases = {"personio": "https://acme.jobs.personio.de/job/1", "smartrecruiters": "https://jobs.smartrecruiters.com/AcmeCorp/1",
                 "teamtailor": "https://acme.teamtailor.com/jobs", "workday": "https://acme.wd5.myworkdayjobs.com/External"}
        for name, link in cases.items():
            with mock.patch.object(watchlist.requests, "get", return_value=Resp("https://example.com", f"<a href='{link}'>x</a>")):
                platform, token, supported = watchlist.detect_platform("https://example.com")
            self.assertEqual(platform, name)
            self.assertFalse(supported)

    def test_an_unreachable_page_is_reported_not_raised(self):
        with mock.patch.object(watchlist.requests, "get", side_effect=watchlist.requests.ConnectionError("down")):
            self.assertEqual(watchlist.detect_platform("https://nope.example")[0], "unreachable")


if __name__ == "__main__":
    unittest.main()
