"""Behaviour tests for submission/solution.py (standard library only).

Run from the kit folder:   python -m unittest discover -s tests -v
The snapshots are loaded once (about 10 s); every test then calls answer()/conflicts().
"""
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "submission"))
from run_eval import load_snapshots  # noqa: E402
import solution  # noqa: E402


def has(text, value):
    """True if `value` (e.g. '5.39%') appears in text, ignoring spacing."""
    return value.replace(" ", "") in text.replace(" ", "")


class KnowledgeSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = solution.build()
        for snap in load_snapshots(ROOT / "snapshots"):
            cls.ks.ingest(snap)

    def ask(self, q, as_of=None):
        return self.ks.answer(q, as_of=as_of)

    # ---------- current value beats stale copies
    def test_auto_loan_uses_official_rate_not_stale_calculator(self):
        a = self.ask("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?")
        self.assertTrue(has(a.text, "5.39%"), a.text)
        self.assertFalse(has(a.text, "5.34%"), a.text)

    def test_mortgage_is_current_not_homebuying_guidance(self):
        a = self.ask("Can you tell me the interest rate for a 30-year mortgage?")
        self.assertTrue(has(a.text, "7.13%"), a.text)
        self.assertFalse(has(a.text, "5.750%"), a.text)

    def test_checking_rate_not_old_landing_page(self):
        a = self.ask("Provide current checking account interest rates")
        self.assertTrue(has(a.text, "2.50%"), a.text)
        self.assertFalse(has(a.text, "3.00%"), a.text)

    def test_branch_count_from_newest_release(self):
        a = self.ask("How many branches does Dupaco have?")
        self.assertIn("22", a.text)
        self.assertNotIn("24 branch", a.text)

    # ---------- tiers / qualifiers
    def test_standard_vs_jumbo_special(self):
        a = self.ask("What is the APY on Dupaco's 17-month special certificate?")
        self.assertTrue(has(a.text, "4.10%"), a.text)
        self.assertFalse(has(a.text, "4.20%"), a.text)

    def test_deposit_amount_selects_jumbo(self):
        a = self.ask("What's the rate on the 17 month certificate special for $100,000?")
        self.assertTrue(has(a.text, "4.20%"), a.text)

    def test_balance_tier(self):
        a = self.ask("What dividend rate do I earn on Share Savings with a balance between $10,000 and $24,999?")
        self.assertTrue(has(a.text, "0.15%"), a.text)
        self.assertFalse(has(a.text, "0.30%"), a.text)

    def test_model_year_tier(self):
        a = self.ask("What APR would I get on a 72-month loan for a 2019 vehicle?")
        self.assertTrue(has(a.text, "6.49%"), a.text)

    # ---------- as_of
    def test_as_of_returns_old_value(self):
        a = self.ask("What was the rate on the 10-month fixed jumbo special certificate as of May 2026?", "2026-05-31")
        self.assertTrue(has(a.text, "3.90%"), a.text)
        self.assertFalse(has(a.text, "4.00%"), a.text)

    def test_date_written_in_question(self):
        a = self.ask("What was the 10-month jumbo certificate APY as of May 2026?")
        self.assertTrue(has(a.text, "3.90%"), a.text)
        self.assertFalse(has(a.text, "4.00%"), a.text)

    def test_without_as_of_returns_latest(self):
        a = self.ask("What's the APY on the 10-month jumbo certificate special?")
        self.assertTrue(has(a.text, "4.00%"), a.text)

    # ---------- honest refusals: confident=False and no numbers
    def assertRefusal(self, q):
        a = self.ask(q)
        self.assertFalse(a.confident, a.text)
        extra = set(re.findall(r"\d+", a.text)) - set(re.findall(r"\d+", q))
        self.assertFalse(extra, f"refusal contains numbers {extra}: {a.text}")

    def test_refuse_unpublished_arm(self):
        self.assertRefusal("What is your rate on a 7/1 adjustable-rate mortgage?")

    def test_refuse_construction_loan(self):
        self.assertRefusal("What interest rate do you charge on a construction loan?")

    def test_refuse_unknown_branch_hours(self):
        self.assertRefusal("Can you give me the lobby hours for the Mullan Avenue branch?")

    # ---------- facts, links, lists
    def test_routing_number(self):
        self.assertIn("273974581", self.ask("What is Dupaco's routing number?").text)

    def test_branch_address(self):
        a = self.ask("What is the address of the Galena Branch?")
        self.assertIn("11375 Oldenburg", a.text)
        self.assertIn("61036", a.text)

    def test_join_link(self):
        a = self.ask("How do I join Dupaco online?")
        self.assertIn("join.dupaco.com", a.text)

    def test_credit_card_list(self):
        a = self.ask("What credit cards do you offer and what are their APRs?")
        for name in ("Platinum Visa", "Rewards Visa", "Business Platinum", "Business Rewards"):
            self.assertIn(name, a.text)

    def test_sources_cited(self):
        a = self.ask("What is the 12-month certificate rate?")
        self.assertTrue(a.sources and a.sources[0].url.startswith("https://www.dupaco.com"))

    # ---------- consistency and speed
    def test_paraphrases_agree(self):
        nums = lambda t: set(re.findall(r"\d+\.\d+%", t))
        a1 = self.ask("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?").text
        a2 = self.ask("60 month car loan rate for a 2025 vehicle?").text
        self.assertEqual(nums(a1), nums(a2))

    def test_repeat_is_identical(self):
        q = "What are the drive-up hours?"
        self.assertEqual(self.ask(q).text, self.ask(q).text)

    def test_answers_are_fast(self):
        qs = ["What is the Rewards Visa APR?", "What are the lobby hours?", "What is Dupaco's Home$tart program?"]
        t = time.time()
        for q in qs:
            self.ks.answer(q + " ")          # bypass the answer cache
        self.assertLess((time.time() - t) / len(qs), 2.0)

    # ---------- conflicts()
    def test_conflicts_found_and_resolved(self):
        cs = self.ks.conflicts()
        self.assertGreater(len(cs), 10)
        auto = [c for c in cs if any(has(v, "5.34%") for v in c.values) and any(has(v, "5.39%") for v in c.values)]
        self.assertTrue(auto, "60-month auto 5.34% vs 5.39% conflict not reported")
        self.assertTrue(has(auto[0].resolution, "5.39%"), auto[0].resolution)
        for c in cs:
            self.assertGreaterEqual(len(c.values), 2)
            self.assertTrue(c.resolution and c.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
