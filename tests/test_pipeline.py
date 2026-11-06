"""End-to-end run of the poller against fake job boards (no network, temporary files)."""
import contextlib
import copy
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ats
import notify
import poller
import watchlist
from tests.helpers import load_example_config


def fake_job(i, title, location="Remote - Europe", description="Enterprise customers and AI."):
    return {"id": str(i), "title": title, "location": location, "workplace": "Remote", "remote": True,
            "url": f"https://example.com/jobs/{i}", "posted_at": None, "description": description}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(poller, "DB_PATH", self.tmp / "jobs.db"),
            mock.patch.object(poller, "ATTENTION_PATH", self.tmp / "attention.csv"),
            mock.patch.object(watchlist, "CSV_PATH", self.tmp / "companies.csv"),
            mock.patch.object(watchlist, "ADD_PATH", self.tmp / "additions.txt"),
            mock.patch.object(watchlist, "BLOCK_PATH", self.tmp / "blocklist.txt"),
            mock.patch.object(watchlist, "BASE", self.tmp),
            mock.patch.object(notify, "send_telegram", lambda *a, **k: None),
            mock.patch.dict(ats.FETCHERS, {
                "greenhouse": lambda token: [fake_job(1, "Chief of Staff"), fake_job(2, "Software Engineer"),
                                             fake_job(3, "Implementation Manager - Embedded Finance"), fake_job(4, "Barista Trainer")],
                "ashby": lambda token: [fake_job(5, "Technical Program Manager", "United States"),
                                        fake_job(6, "People Operations Manager")]}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        watchlist.write_companies([{"name": "Acme", "ats": "greenhouse", "token": "acme"}, {"name": "Beta", "ats": "ashby", "token": "beta"}])
        self.cfg = load_example_config()
        self.quiet = contextlib.redirect_stdout(io.StringIO())

    def titles(self):
        conn = sqlite3.connect(poller.DB_PATH)
        try:
            return sorted(r[0] for r in conn.execute("SELECT title FROM jobs"))
        finally:
            conn.close()

    def test_only_relevant_jobs_are_stored(self):
        poller.run_once(self.cfg, send=False)
        self.assertEqual(self.titles(), ["Chief of Staff", "Implementation Manager - Embedded Finance", "Technical Program Manager"])

    def test_a_second_run_finds_nothing_new(self):
        poller.run_once(self.cfg, send=False)
        poller.run_once(self.cfg, send=False)
        self.assertEqual(len(self.titles()), 3)

    def test_rescore_applies_changed_rules_to_stored_jobs(self):
        poller.run_once(self.cfg, send=False)
        stricter = copy.deepcopy(self.cfg)
        stricter["filters"]["title_exclude"].append("chief of staff")
        with self.quiet:
            poller.run_rescore(stricter)
        conn = sqlite3.connect(poller.DB_PATH)
        score = conn.execute("SELECT score FROM jobs WHERE title='Chief of Staff'").fetchone()[0]
        conn.close()
        self.assertEqual(score, 0)

    def test_find_lists_matching_jobs(self):
        poller.run_once(self.cfg, send=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            poller.run_find(self.cfg, "united states")
        self.assertIn("Technical Program Manager", out.getvalue())
        self.assertNotIn("Chief of Staff", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class BeforeTheFirstPoll(unittest.TestCase):
    """A brand-new install has no database yet; the diagnostic commands must still work."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(poller, "DB_PATH", tmp / "jobs.db"),
                  mock.patch.object(watchlist, "CSV_PATH", tmp / "companies.csv"),
                  mock.patch.object(watchlist, "BLOCK_PATH", tmp / "blocklist.txt")):
            p.start()
            self.addCleanup(p.stop)
        watchlist.write_companies([{"name": "Acme", "ats": "greenhouse", "token": "acme"}])
        self.cfg = load_example_config()

    def run_quietly(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(self.cfg, *args)
        return out.getvalue()

    def test_stats_explains_what_to_do_first(self):
        self.assertIn("Run one polling cycle first", self.run_quietly(poller.run_stats))

    def test_find_and_rescore_do_not_crash(self):
        self.assertIn("0 open stored jobs", self.run_quietly(poller.run_find, "anything"))
        self.assertIn("Re-scored 0 jobs", self.run_quietly(poller.run_rescore))
