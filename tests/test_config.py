"""Configuration loading, validation and secret handling."""

import os
import tempfile
import unittest
from pathlib import Path

from src import config as config_module
from src.config import ConfigError

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcDEF-ghi_jkl"

MINIMAL = """
interval_minutes: 30
discord:
  webhook_url: "%s"
sources:
  - board: remotive
searches:
  - name: Test
    titles:
      include: [Security Analyst]
""" % WEBHOOK


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        os.environ.pop("DISCORD_WEBHOOK_URL", None)

    def write(self, text: str) -> Path:
        path = Path(self._directory.name) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_a_minimal_config(self):
        cfg = config_module.load(self.write(MINIMAL))
        self.assertEqual(cfg.interval_minutes, 30)
        self.assertEqual(cfg.webhook_url, WEBHOOK)
        self.assertEqual([source.board for source in cfg.sources], ["remotive"])
        self.assertEqual(cfg.searches[0].titles.include, ("Security Analyst",))

    def test_missing_file_names_the_file(self):
        with self.assertRaises(ConfigError) as caught:
            config_module.load(Path(self._directory.name) / "nope.yaml")
        self.assertIn("nope.yaml", str(caught.exception))

    def test_rejects_aggressive_polling(self):
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write(MINIMAL.replace("interval_minutes: 30", "interval_minutes: 1")))
        self.assertIn("at least 5", str(caught.exception))

    def test_environment_variable_overrides_the_webhook(self):
        other = "https://discord.com/api/webhooks/999/zzz"
        os.environ["DISCORD_WEBHOOK_URL"] = other
        self.addCleanup(os.environ.pop, "DISCORD_WEBHOOK_URL", None)
        cfg = config_module.load(self.write(MINIMAL))
        self.assertEqual(cfg.webhook_url, other)

    def test_placeholder_is_expanded_from_the_environment(self):
        os.environ["MY_HOOK"] = WEBHOOK
        self.addCleanup(os.environ.pop, "MY_HOOK", None)
        cfg = config_module.load(self.write(MINIMAL.replace(WEBHOOK, "${MY_HOOK}")))
        self.assertEqual(cfg.webhook_url, WEBHOOK)

    def test_a_bad_webhook_error_does_not_echo_the_value(self):
        secret = "https://example.com/not-a-webhook/SUPERSECRETTOKEN"
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write(MINIMAL.replace(WEBHOOK, secret)))
        self.assertNotIn("SUPERSECRETTOKEN", str(caught.exception))

    def test_missing_webhook_is_reported(self):
        text = MINIMAL.replace(f'webhook_url: "{WEBHOOK}"', "webhook_url: \"\"")
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write(text))
        self.assertIn("DISCORD_WEBHOOK_URL", str(caught.exception))

    def test_dry_run_does_not_need_a_webhook(self):
        text = MINIMAL.replace(f'webhook_url: "{WEBHOOK}"', "webhook_url: \"\"")
        cfg = config_module.load(self.write(text), require_webhook=False)
        self.assertEqual(cfg.webhook_url, "")

    def test_search_without_filters_is_rejected(self):
        text = MINIMAL.replace("    titles:\n      include: [Security Analyst]\n", "")
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write(text))
        self.assertIn("no filters", str(caught.exception))

    def test_typo_in_a_key_is_reported_with_its_path(self):
        text = MINIMAL.replace("    titles:", "    title:")
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write(text))
        self.assertIn("searches[0]", str(caught.exception))

    def test_unrecognised_employment_type_lists_the_valid_ones(self):
        text = MINIMAL + "    employment_types:\n      include: [Fulll Time]\n"
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write(text))
        self.assertIn("full-time", str(caught.exception))

    def test_a_bare_list_is_accepted_as_includes(self):
        text = MINIMAL.replace(
            "    titles:\n      include: [Security Analyst]", "    titles: [Security Analyst]"
        )
        cfg = config_module.load(self.write(text))
        self.assertEqual(cfg.searches[0].titles.include, ("Security Analyst",))

    def test_disabled_sources_are_dropped(self):
        text = MINIMAL.replace(
            "  - board: remotive", "  - board: remotive\n  - board: remoteok\n    enabled: false"
        )
        cfg = config_module.load(self.write(text))
        self.assertEqual([source.board for source in cfg.sources], ["remotive"])

    def test_query_terms_are_deduplicated_across_searches(self):
        text = MINIMAL + """
  - name: Second
    titles:
      include: [Security Analyst, SOC Analyst]
"""
        cfg = config_module.load(self.write(text))
        self.assertEqual(cfg.query_terms(), ("Security Analyst", "SOC Analyst"))

    def test_invalid_yaml_is_reported_clearly(self):
        with self.assertRaises(ConfigError) as caught:
            config_module.load(self.write("interval_minutes: 30\n  bad: [indent"))
        self.assertIn("not valid YAML", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
