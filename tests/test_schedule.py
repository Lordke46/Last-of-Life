import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _on(doc):
    # PyYAML 1.1, 'on' anahtarını bool sanabilir.
    return doc.get("on", doc.get(True))


class ScheduleTests(unittest.TestCase):
    def test_crons_match_the_hourly_cadence(self):
        expected = {
            "quick.yml": ["17 * * * *"],
            "update.yml": ["47 5,11,17,23 * * *"],
            "health.yml": ["45 * * * *"],
            "backtest.yml": ["23 * * * *"],
        }
        for name, crons in expected.items():
            doc = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
            self.assertEqual([item["cron"] for item in _on(doc)["schedule"]], crons, name)

    def test_health_can_dispatch_the_quick_workflow(self):
        text = (ROOT / ".github" / "workflows" / "health.yml").read_text()
        self.assertIn("actions: write", text)
        self.assertIn("app.health_dispatch", text)
        self.assertIn("--dispatch", text)
