"""Filtering rules: titles, keywords, companies, locations, types and salary."""

import unittest

from src.config import Filters, Search
from src.matching import best_match, evaluate
from src.models import Job


def make_job(**overrides) -> Job:
    values = {
        "board": "test",
        "title": "Security Analyst",
        "company": "Example Company",
        "location": "Toronto, Ontario, Canada",
        "url": "https://example.com/jobs/1",
        "description": "You will run the SIEM and handle incident response.",
        "employment_type": "full-time",
    }
    values.update(overrides)
    return Job(**values)


def make_search(**overrides) -> Search:
    values = {"name": "Test", "titles": Filters(include=("Security Analyst",))}
    values.update(overrides)
    return Search(**values)


class TitleTests(unittest.TestCase):
    def test_included_title_matches_and_scores(self):
        result = evaluate(make_job(), make_search())
        self.assertTrue(result.matched)
        self.assertEqual(result.score, 3)

    def test_matching_ignores_case_and_punctuation(self):
        job = make_job(title="SECURITY-ANALYST (Tier 2)")
        self.assertTrue(evaluate(job, make_search()).matched)

    def test_unrelated_title_is_rejected(self):
        result = evaluate(make_job(title="Truck Driver"), make_search())
        self.assertFalse(result.matched)
        self.assertIn("no title match", result.rejected_by)

    def test_excluded_word_rejects(self):
        search = make_search(titles=Filters(include=("Security Analyst",), exclude=("Senior",)))
        result = evaluate(make_job(title="Senior Security Analyst"), search)
        self.assertFalse(result.matched)
        self.assertIn("Senior", result.rejected_by)

    def test_exclusions_match_whole_words_only(self):
        """Excluding "Lead" must not reject "Leadership Development Analyst"."""
        search = make_search(
            titles=Filters(include=("Analyst",), exclude=("Lead",)),
        )
        self.assertTrue(evaluate(make_job(title="Leadership Analyst"), search).matched)
        self.assertFalse(evaluate(make_job(title="Lead Analyst"), search).matched)

    def test_short_acronyms_do_not_match_inside_words(self):
        search = make_search(titles=Filters(include=("SOC",)))
        self.assertFalse(evaluate(make_job(title="Social Media Coordinator"), search).matched)
        self.assertTrue(evaluate(make_job(title="SOC Analyst"), search).matched)


class KeywordTests(unittest.TestCase):
    def test_keyword_in_the_description_matches(self):
        search = make_search(keywords=Filters(include=("SIEM",)))
        result = evaluate(make_job(), search)
        self.assertTrue(result.matched)
        self.assertEqual(result.score, 4)  # title 3 + keyword 1

    def test_multi_word_keyword_tolerates_punctuation(self):
        job = make_job(description="Experience with Microsoft-Sentinel required.")
        search = make_search(keywords=Filters(include=("Microsoft Sentinel",)))
        self.assertTrue(evaluate(job, search).matched)

    def test_keyword_also_looks_at_the_title_and_tags(self):
        job = make_job(description="", tags=("crowdstrike",))
        search = make_search(keywords=Filters(include=("CrowdStrike",)))
        self.assertTrue(evaluate(job, search).matched)

    def test_excluded_keyword_rejects(self):
        job = make_job(description="This is an unpaid volunteer placement.")
        search = make_search(keywords=Filters(exclude=("unpaid",)))
        result = evaluate(job, search)
        self.assertFalse(result.matched)
        self.assertIn("unpaid", result.rejected_by)

    def test_missing_description_does_not_reject(self):
        """Boards that publish no description must not silently filter everything out."""
        job = make_job(description="", tags=())
        search = make_search(keywords=Filters(include=("SIEM",)))
        result = evaluate(job, search)
        self.assertTrue(result.matched)
        self.assertIn("keywords not checked", " ".join(result.reasons))


class CompanyAndLocationTests(unittest.TestCase):
    def test_company_include_and_score(self):
        search = make_search(companies=Filters(include=("Example Company",)))
        self.assertEqual(evaluate(make_job(), search).score, 5)  # title 3 + company 2

    def test_excluded_company_rejects(self):
        search = make_search(companies=Filters(exclude=("Example Company",)))
        result = evaluate(make_job(), search)
        self.assertFalse(result.matched)
        self.assertIn("excluded company", result.rejected_by)

    def test_location_include_matches_part_of_the_location(self):
        search = make_search(locations=Filters(include=("Ontario",)))
        self.assertTrue(evaluate(make_job(), search).matched)

    def test_remote_flag_satisfies_a_remote_location_filter(self):
        job = make_job(location="Anywhere", remote=True)
        search = make_search(locations=Filters(include=("Remote",)))
        self.assertTrue(evaluate(job, search).matched)

    def test_excluded_location_rejects(self):
        job = make_job(location="Austin, Texas, United States")
        search = make_search(locations=Filters(exclude=("United States",)))
        self.assertFalse(evaluate(job, search).matched)


class EmploymentTypeTests(unittest.TestCase):
    def test_requested_type_matches_however_the_board_spells_it(self):
        search = make_search(employment_types=Filters(include=("Full-time",)))
        self.assertTrue(evaluate(make_job(employment_type="full-time"), search).matched)

    def test_other_types_are_rejected(self):
        search = make_search(employment_types=Filters(include=("Full-time",)))
        self.assertFalse(evaluate(make_job(employment_type="internship"), search).matched)

    def test_unknown_type_does_not_reject(self):
        search = make_search(employment_types=Filters(include=("Full-time",)))
        self.assertTrue(evaluate(make_job(employment_type=None), search).matched)


class SalaryTests(unittest.TestCase):
    def test_salary_above_the_minimum_passes(self):
        job = make_job(salary_min=90000, salary_max=110000, salary_currency="CAD")
        search = make_search(salary_minimum=80000, salary_currency="CAD")
        self.assertTrue(evaluate(job, search).matched)

    def test_salary_below_the_minimum_is_rejected(self):
        job = make_job(salary_min=40000, salary_max=45000, salary_currency="CAD")
        search = make_search(salary_minimum=80000, salary_currency="CAD")
        self.assertFalse(evaluate(job, search).matched)

    def test_absent_salary_never_rejects(self):
        search = make_search(salary_minimum=80000, salary_currency="CAD")
        self.assertTrue(evaluate(make_job(), search).matched)

    def test_estimated_salary_never_rejects(self):
        job = make_job(salary_min=30000, salary_currency="CAD", salary_is_estimate=True)
        search = make_search(salary_minimum=80000, salary_currency="CAD")
        self.assertTrue(evaluate(job, search).matched)

    def test_hourly_pay_is_compared_annually(self):
        job = make_job(salary_min=50, salary_period="hour", salary_currency="CAD")
        search = make_search(salary_minimum=80000, salary_currency="CAD")
        self.assertTrue(evaluate(job, search).matched)  # 50/h is about 104,000/yr

    def test_a_different_currency_is_not_compared(self):
        job = make_job(salary_min=60000, salary_currency="USD")
        search = make_search(salary_minimum=80000, salary_currency="CAD")
        self.assertTrue(evaluate(job, search).matched)


class ScoreAndSelectionTests(unittest.TestCase):
    def test_min_score_gates_weak_matches(self):
        search = make_search(titles=Filters(include=("Analyst",)), min_score=5)
        self.assertFalse(evaluate(make_job(), search).matched)

    def test_best_match_returns_the_highest_scoring_search(self):
        weak = make_search(name="Weak", titles=Filters(include=("Security Analyst",)))
        strong = make_search(
            name="Strong",
            titles=Filters(include=("Security Analyst",)),
            companies=Filters(include=("Example Company",)),
        )
        result = best_match(make_job(), [weak, strong])
        self.assertEqual(result.search_name, "Strong")

    def test_best_match_returns_none_when_nothing_matches(self):
        self.assertIsNone(best_match(make_job(title="Chef"), [make_search()]))

    def test_malformed_job_does_not_raise(self):
        job = Job(board="test", title="", company="", url="", description="")
        self.assertIsNone(best_match(job, [make_search()]))


if __name__ == "__main__":
    unittest.main()
