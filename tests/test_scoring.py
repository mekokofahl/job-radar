import unittest

import scoring
from tests.helpers import job, load_example_config


class ScorerTestCase(unittest.TestCase):
    def setUp(self):
        self.cfg = load_example_config()
        self.s = scoring.Scorer(self.cfg)

    def score(self, *args, **kwargs):
        return self.s.evaluate(job(*args, **kwargs), "Acme")


class TitleRules(ScorerTestCase):
    def test_target_titles_are_kept(self):
        for t in ("Implementation Manager", "Chief of Staff", "Technical Program Manager", "Head of Delivery", "Engagement Manager"):
            self.assertIsNotNone(self.score(t), t)

    def test_phrasings_that_real_postings_use(self):
        for t in ("Manager of Implementations", "Technical Project Manager", "Head of Operations", "Strategic Programs Manager"):
            self.assertIsNotNone(self.score(t), t)

    def test_engineering_and_sales_titles_are_excluded(self):
        for t in ("Software Engineer", "Account Executive", "Senior Recruiter"):
            self.assertIsNone(self.score(t), t)
            self.assertIn("exclude list", self.s.reject)

    def test_titles_outside_your_targets_are_rejected_with_a_reason(self):
        self.assertIsNone(self.score("Barista Trainer"))
        self.assertIn("does not match", self.s.reject)

    def test_noise_that_shares_words_with_targets_is_excluded(self):
        for t in ("People Operations Manager", "HR Operations Lead", "Product Designer", "Billing Operations Manager", "Senior Data Analyst, Operations"):
            self.assertIsNone(self.score(t), t)

    def test_product_words_inside_a_title_do_not_cause_exclusion(self):
        # "Embedded Finance" is a product category, not an embedded-software role.
        self.assertIsNotNone(self.score("Implementation Manager - Embedded Finance"))


class LocationRules(ScorerTestCase):
    def test_remote_open_to_your_region_scores_highest(self):
        eu = self.score("Implementation Manager", "Remote - Europe")
        us = self.score("Implementation Manager", "United States")
        self.assertEqual(eu["loc_class"], "remote_ok")
        self.assertGreater(eu["score"], us["score"])
        self.assertIn("US-only remote: check hours overlap", us["flags"])

    def test_onsite_in_your_home_area_carries_no_penalty(self):
        r = self.score("Implementation Manager", "Berlin", workplace=None, remote=None)
        self.assertEqual(r["loc_class"], "onsite_target")
        self.assertIn("onsite_target +0", " ".join(r["reasons"]))
        self.assertIn("in your home area", r["flags"])

    def test_onsite_elsewhere_in_a_target_city_is_penalized(self):
        r = self.score("Implementation Manager", "New York City", workplace=None, remote=None)
        self.assertIn("onsite_target -7", " ".join(r["reasons"]))

    def test_onsite_in_a_preferred_relocation_city_is_penalized_less(self):
        r = self.score("Implementation Manager", "Amsterdam", workplace=None, remote=None)
        self.assertIn("onsite_target -3", " ".join(r["reasons"]))
        self.assertIn("preferred relocation city", r["flags"])

    def test_unlisted_onsite_location_is_penalized_most(self):
        r = self.score("Implementation Manager", "Tokyo", workplace=None, remote=None)
        self.assertEqual(r["loc_class"], "onsite_other")

    def test_single_city_flagged_remote_by_the_job_board_is_not_treated_as_remote(self):
        r = self.score("Implementation Manager", "New York, New York, United States", workplace=None, remote=True)
        self.assertEqual(r["loc_class"], "onsite_target")
        self.assertIn("feed says remote, but location is a city: verify", r["flags"])

    def test_remote_for_other_countries_is_restricted(self):
        r = self.score("Implementation Manager", "Remote - Brazil")
        self.assertEqual(r["loc_class"], "remote_restricted")


class DescriptionRules(ScorerTestCase):
    def test_wording_that_restricts_hiring_is_penalized_and_flagged(self):
        base = self.score("Implementation Manager", description="Enterprise customers.")
        r = self.score("Implementation Manager", description="Enterprise customers. Open to residents of the United States only.")
        self.assertLess(r["score"], base["score"])
        self.assertTrue(any("wording limits who can be hired" in f for f in r["flags"]))

    def test_location_based_pay_is_flagged(self):
        r = self.score("Implementation Manager", description="Enterprise customers. We offer competitive, location-based pay.")
        self.assertTrue(any("pay adjusted by location" in f for f in r["flags"]))

    def test_salary_below_your_floor_is_flagged(self):
        r = self.score("Implementation Manager", description="Enterprise customers. Salary range $60,000 - $70,000.")
        self.assertTrue(any("below floor" in f for f in r["flags"]))

    def test_salary_above_the_floor_is_not_flagged(self):
        r = self.score("Implementation Manager", description="Enterprise customers. Salary range $150,000 - $170,000.")
        self.assertFalse(any("below floor" in f for f in r["flags"]))

    def test_avoided_industries_are_excluded(self):
        self.assertIsNone(self.score("Implementation Manager", description="Crypto, blockchain, web3 and defi for enterprise customers."))
        self.assertIn("avoided industry", self.s.reject)

    def test_ai_and_executive_language_adds_points(self):
        plain = self.score("Implementation Manager", description="A role.")
        rich = self.score("Implementation Manager", description="AI platform. Work with the CEO and executive team on enterprise customers.")
        self.assertGreater(rich["score"], plain["score"])


class TravelRules(ScorerTestCase):
    def test_a_range_uses_its_upper_end(self):
        self.assertEqual(scoring.travel_info("You will travel roughly 30\u201350% of the time."), (50, "pct"))

    def test_simple_percentages_and_week_counts(self):
        self.assertEqual(scoring.travel_info("Up to 25% travel."), (25, "pct"))
        self.assertEqual(scoring.travel_info("Travel one week per month to customers."), (25, "pct"))

    def test_percentages_unrelated_to_travel_are_ignored(self):
        self.assertEqual(scoring.travel_info("401(k) with a 4% match; travel and expense reimbursement included."), (None, ""))
        self.assertEqual(scoring.travel_info("This is a 100% remote role with occasional travel to the offsite."), (None, "mention"))

    def test_no_travel_and_vague_travel(self):
        self.assertEqual(scoring.travel_info("No travel required."), (None, ""))
        self.assertEqual(scoring.travel_info("Extensive travel is part of the job."), (None, "qual"))

    def test_travel_above_your_limit_is_penalized(self):
        ok = self.score("Implementation Manager", description="Enterprise customers. Up to 20% travel.")
        heavy = self.score("Implementation Manager", description="Enterprise customers. Up to 50% travel.")
        self.assertLess(heavy["score"], ok["score"])
        self.assertTrue(any("heavy travel" in f for f in heavy["flags"]))
        self.assertEqual(heavy["travel_pct"], 50)


if __name__ == "__main__":
    unittest.main()
