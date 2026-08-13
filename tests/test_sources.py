"""Source parsing, including malformed and partial responses.

The fixtures are cut down from real responses, keeping the structure that matters.
"""

import unittest

from src.config import ConfigError, SourceSettings
from src.models import normalize_employment_type, parse_salary_text
from src.sources import UNSUPPORTED_BOARDS, build
from src.sources import adzuna, greenhouse, lever, linkedin, remoteok, remotive, rss

LINKEDIN_FRAGMENT = """
<!DOCTYPE html>
<li>
  <div class="base-card relative base-search-card job-search-card"
       data-entity-urn="urn:li:jobPosting:4448789855">
    <a class="base-card__full-link"
       href="https://ca.linkedin.com/jobs/view/information-security-analyst-at-vretta-4448789855?position=1&amp;refId=xyz">
      <span class="sr-only">Information Security Analyst</span>
    </a>
    <div class="search-entity-media">
      <img alt="" data-delayed-url="https://media.licdn.com/logo.png?e=123&amp;v=beta"/>
    </div>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Information Security Analyst</h3>
      <h4 class="base-search-card__subtitle"><a href="#">Example Company</a></h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">Toronto, Ontario, Canada</span>
        <time class="job-search-card__listdate" datetime="2026-08-07">6 days ago</time>
      </div>
    </div>
  </div>
</li>
<li><div class="base-card"><div class="base-search-card__info"></div></div></li>
"""

LINKEDIN_DETAIL = """
<html><body>
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"JobPosting",
 "title":"Information Security Analyst",
 "description":"&lt;p&gt;You will run the &lt;b&gt;SIEM&lt;/b&gt; and handle incident response.&lt;/p&gt;",
 "employmentType":"FULL_TIME",
 "datePosted":"2026-08-07T16:12:43.000Z",
 "baseSalary":{"@type":"MonetaryAmount","currency":"CAD",
   "value":{"@type":"QuantitativeValue","minValue":85000,"maxValue":95000,"unitText":"YEAR"}}}
</script>
<ul class="description__job-criteria-list">
  <li><h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text">Full-time</span></li>
</ul>
</body></html>
"""


class LinkedInTests(unittest.TestCase):
    def test_search_fragment_is_parsed(self):
        jobs = linkedin.parse_search_fragment(LINKEDIN_FRAGMENT)
        self.assertEqual(len(jobs), 1)  # the empty second card is skipped
        job = jobs[0]
        self.assertEqual(job.title, "Information Security Analyst")
        self.assertEqual(job.company, "Example Company")
        self.assertEqual(job.location, "Toronto, Ontario, Canada")
        self.assertEqual(job.external_id, "4448789855")
        self.assertNotIn("refId", job.url)
        self.assertEqual(job.posted_at.year, 2026)
        self.assertTrue(job.company_logo.endswith("logo.png"))

    def test_detail_page_adds_description_type_and_salary(self):
        job = linkedin.parse_search_fragment(LINKEDIN_FRAGMENT)[0]
        self.assertTrue(linkedin.apply_detail_page(job, LINKEDIN_DETAIL))
        self.assertIn("SIEM", job.description)
        self.assertNotIn("<b>", job.description)
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.salary_min, 85000)
        self.assertEqual(job.salary_currency, "CAD")
        self.assertIn("85,000", job.salary_text)

    def test_detail_page_without_structured_data_falls_back_to_the_markup(self):
        html = '<div class="show-more-less-html__markup"><p>Plain <b>description</b>.</p></div>'
        job = linkedin.parse_search_fragment(LINKEDIN_FRAGMENT)[0]
        self.assertTrue(linkedin.apply_detail_page(job, html))
        self.assertEqual(job.description, "Plain description .")

    def test_unexpected_html_yields_nothing_instead_of_raising(self):
        self.assertEqual(linkedin.parse_search_fragment("<html><body>Sorry</body></html>"), [])
        self.assertEqual(linkedin.parse_search_fragment(""), [])

    def test_detail_page_of_garbage_does_not_raise(self):
        job = linkedin.parse_search_fragment(LINKEDIN_FRAGMENT)[0]
        self.assertFalse(linkedin.apply_detail_page(job, "<html>{not json}</html>"))

    def test_searches_beyond_the_cap_are_skipped_out_loud(self):
        """Silently dropping searches is indistinguishable from a board hiding jobs."""

        class CountingSource(linkedin.LinkedInSource):
            def __init__(self, *args, **kwargs):
                self.searched = []
                super().__init__(*args, **kwargs)

            def _search(self, term, location):
                self.searched.append((term, location))
                return []

        source = CountingSource(None, {"locations": ["Canada", "Ireland"], "max_queries": 3})
        with self.assertLogs("src.sources.linkedin", level="WARNING") as logs:
            source.fetch(["SOC Analyst", "Security Analyst", "Security Engineer"])
        self.assertEqual(len(source.searched), 3)
        # The whole of the second location is what gets dropped, which is worth saying.
        self.assertEqual({location for _term, location in source.searched}, {"Canada"})
        self.assertIn("max_queries", " ".join(logs.output))


class GreenhouseTests(unittest.TestCase):
    payload = {
        "jobs": [
            {
                "id": 8503792002,
                "title": "Security Analyst",
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/8503792002",
                "company_name": "Example Company",
                "location": {"name": "Remote, Canada"},
                "content": "&lt;p&gt;Run the &lt;b&gt;SOC&lt;/b&gt;.&lt;/p&gt;",
                "first_published": "2026-04-17T05:58:03-04:00",
                "departments": [{"name": "Security"}],
            },
            {"title": "No URL here"},
        ]
    }

    def test_jobs_are_parsed_and_incomplete_entries_skipped(self):
        jobs = greenhouse.parse_jobs(self.payload, "example")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.company, "Example Company")
        self.assertEqual(job.description, "Run the SOC .")
        self.assertTrue(job.remote)
        self.assertEqual(job.tags, ("Security",))
        self.assertEqual(job.external_id, "example:8503792002")

    def test_unexpected_payload_shapes_are_ignored(self):
        for payload in (None, [], {}, {"jobs": None}, {"jobs": ["nonsense"]}):
            self.assertEqual(greenhouse.parse_jobs(payload, "example"), [])

    def test_companies_option_is_required(self):
        with self.assertRaises(ConfigError):
            build(SourceSettings(board="greenhouse", options={}), client=None)


class LeverTests(unittest.TestCase):
    payload = [
        {
            "id": "33538a2f",
            "text": "Security Analyst",
            "hostedUrl": "https://jobs.lever.co/example/33538a2f",
            "categories": {
                "commitment": "Regular Full Time (Salary)",
                "location": "Toronto, ON",
                "department": "Security",
            },
            "descriptionPlain": "Run the SIEM.",
            "createdAt": 1553186035299,
            "workplaceType": "hybrid",
        },
        {"text": "Broken", "categories": None},
    ]

    def test_jobs_are_parsed_including_awkward_commitment_wording(self):
        jobs = lever.parse_jobs(self.payload, "example")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].employment_type, "full-time")
        self.assertEqual(jobs[0].location, "Toronto, ON")
        self.assertEqual(jobs[0].posted_at.year, 2019)

    def test_unexpected_payload_shapes_are_ignored(self):
        for payload in (None, {}, "text", [None, 3]):
            self.assertEqual(lever.parse_jobs(payload, "example"), [])


class RemotiveTests(unittest.TestCase):
    payload = {
        "jobs": [
            {
                "id": 2090989,
                "title": "Security Analyst",
                "url": "https://remotive.com/remote-jobs/security/security-analyst-2090989",
                "company_name": "Example Company",
                "job_type": "full_time",
                "candidate_required_location": "Canada",
                "salary": "$80,000 - $95,000 CAD",
                "description": "<p>Run the SIEM.</p>",
                "publication_date": "2026-08-12T06:36:49",
                "tags": ["security"],
            }
        ]
    }

    def test_jobs_are_parsed_with_salary_read_from_free_text(self):
        job = remotive.parse_jobs(self.payload)[0]
        self.assertEqual(job.employment_type, "full-time")
        self.assertTrue(job.remote)
        self.assertIn("Remote", job.location)
        self.assertEqual(job.salary_min, 80000)
        self.assertEqual(job.salary_max, 95000)
        self.assertEqual(job.salary_currency, "CAD")

    def test_unexpected_payload_shapes_are_ignored(self):
        for payload in (None, [], {"jobs": "nope"}):
            self.assertEqual(remotive.parse_jobs(payload), [])


class RemoteOkTests(unittest.TestCase):
    payload = [
        {"legal": "API Terms of Service: please link back."},
        {
            "id": "1136570",
            "position": "Security Analyst",
            "company": "Example Company",
            "url": "https://remoteOK.com/remote-jobs/security-analyst-1136570",
            "location": "Worldwide",
            "description": "<p>Run the SOC.</p>",
            "salary_min": 90000,
            "salary_max": 0,
            "date": "2026-08-12T16:01:22+00:00",
            "tags": ["security"],
        },
    ]

    def test_the_legal_entry_is_skipped_and_zero_salary_means_unknown(self):
        jobs = remoteok.parse_jobs(self.payload)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].salary_min, 90000)
        self.assertIsNone(jobs[0].salary_max)
        self.assertTrue(jobs[0].remote)

    def test_unexpected_payload_shapes_are_ignored(self):
        for payload in (None, {}, ["text"]):
            self.assertEqual(remoteok.parse_jobs(payload), [])


RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>We Work Remotely</title>
  <item>
    <title>Example Company: Security Analyst</title>
    <region>Anywhere in the World</region>
    <type>Full-Time</type>
    <category>Security</category>
    <description>&lt;p&gt;Run the &lt;b&gt;SIEM&lt;/b&gt;.&lt;/p&gt;</description>
    <pubDate>Thu, 13 Aug 2026 17:07:58 +0000</pubDate>
    <guid>https://weworkremotely.com/remote-jobs/example-security-analyst</guid>
    <link>https://weworkremotely.com/remote-jobs/example-security-analyst</link>
  </item>
  <item><title>No link</title></item>
</channel></rss>
"""

ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Security Analyst</title>
    <link rel="alternate" href="https://example.com/jobs/1"/>
    <summary>Run the SIEM.</summary>
    <updated>2026-08-13T17:07:58Z</updated>
    <id>tag:example.com,2026:1</id>
  </entry>
</feed>
"""


class RssTests(unittest.TestCase):
    def test_rss_item_is_parsed_and_the_company_split_from_the_title(self):
        jobs = rss.parse_feed(RSS_FEED, "We Work Remotely")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.company, "Example Company")
        self.assertEqual(job.title, "Security Analyst")
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.description, "Run the SIEM .")
        self.assertTrue(job.remote)
        self.assertEqual(job.board, "rss:we-work-remotely")
        self.assertEqual(job.board_label, "We Work Remotely")
        self.assertEqual(job.posted_at.year, 2026)

    def test_atom_feed_is_parsed(self):
        jobs = rss.parse_feed(ATOM_FEED, "Example")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://example.com/jobs/1")
        self.assertEqual(jobs[0].title, "Security Analyst")

    def test_malformed_xml_raises_a_parse_error_the_source_catches(self):
        import xml.etree.ElementTree as ElementTree

        with self.assertRaises(ElementTree.ParseError):
            rss.parse_feed(b"<rss><channel><item>", "Broken")

    def test_feeds_option_is_validated(self):
        with self.assertRaises(ConfigError):
            build(SourceSettings(board="rss", options={}), client=None)
        with self.assertRaises(ConfigError):
            build(
                SourceSettings(board="rss", options={"feeds": [{"url": "not-a-url"}]}), client=None
            )


class AdzunaTests(unittest.TestCase):
    payload = {
        "results": [
            {
                "id": "4567",
                "title": "Security <strong>Analyst</strong>",
                "redirect_url": "https://www.adzuna.ca/land/ad/4567",
                "company": {"display_name": "Example Company"},
                "location": {"display_name": "Toronto, Ontario"},
                "description": "Run the SIEM.",
                "contract_time": "full_time",
                "salary_min": 80000,
                "salary_max": 95000,
                "salary_is_predicted": "1",
                "created": "2026-08-12T10:00:00Z",
                "category": {"label": "IT Jobs"},
            }
        ]
    }

    def test_jobs_are_parsed_and_predicted_salaries_are_flagged(self):
        job = adzuna.parse_jobs(self.payload, "ca")[0]
        self.assertEqual(job.title, "Security Analyst")
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.salary_currency, "CAD")
        self.assertTrue(job.salary_is_estimate)
        self.assertIn("estimated", job.salary_text)

    def test_credentials_are_required(self):
        with self.assertRaises(ConfigError):
            build(SourceSettings(board="adzuna", options={"country": "ca"}), client=None)

    def test_unknown_country_is_rejected(self):
        with self.assertRaises(ConfigError):
            build(
                SourceSettings(
                    board="adzuna", options={"country": "zz", "app_id": "a", "app_key": "b"}
                ),
                client=None,
            )


class RegistryTests(unittest.TestCase):
    def test_unknown_board_lists_the_available_ones(self):
        with self.assertRaises(ConfigError) as caught:
            build(SourceSettings(board="monster", options={}), client=None)
        self.assertIn("linkedin", str(caught.exception))

    def test_indeed_explains_why_it_is_unsupported(self):
        with self.assertRaises(ConfigError) as caught:
            build(SourceSettings(board="indeed", options={}), client=None)
        message = str(caught.exception)
        self.assertIn("403", message)
        self.assertIn("adzuna", message)
        self.assertIn("indeed", UNSUPPORTED_BOARDS)

    def test_a_misspelled_option_is_reported(self):
        with self.assertRaises(ConfigError) as caught:
            build(SourceSettings(board="linkedin", options={"location": ["Canada"]}), client=None)
        self.assertIn("locations", str(caught.exception))


class NormalizationTests(unittest.TestCase):
    def test_employment_wording_from_real_boards(self):
        cases = {
            "FULL_TIME": "full-time",
            "Full-time": "full-time",
            "full_time": "full-time",
            "Regular Full Time (Salary)": "full-time",
            "Part-Time": "part-time",
            "Contract to hire": "contract",
            "Temporary / Seasonal": "temporary",
            "Internship": "internship",
            "Co-op": "internship",
            "": None,
            None: None,
            "Something else entirely": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_employment_type(raw), expected, msg=raw)

    def test_salary_text_parsing(self):
        self.assertEqual(parse_salary_text("$80,000 - $95,000 CAD"), (80000, 95000, "CAD"))
        self.assertEqual(parse_salary_text("80k-95k USD"), (80000, 95000, "USD"))
        self.assertEqual(parse_salary_text("£45,000"), (45000, None, "GBP"))
        self.assertEqual(parse_salary_text(""), (None, None, None))
        self.assertEqual(parse_salary_text("Competitive"), (None, None, None))


if __name__ == "__main__":
    unittest.main()
