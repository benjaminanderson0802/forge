"""Progress and usage bars on the status page."""
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import status_page


class BarTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.st = self.root / "state" / "bootstrap"
        self.st.mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "progress.json").write_text(json.dumps({"phases": [
            {"name": "0. Core", "done": True}, {"name": "1. Loop", "done": False},
            {"name": "2. Audit", "done": False}, {"name": "3. Hands", "done": False}]}), encoding="utf-8")
        self.now = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
        self.limits = {"claude_daily_token_cap": 20_000_000, "codex_daily_token_cap": 10_000_000,
                       "agent_runs_per_day": 150, "mail_per_day": 10}

    def queue(self, statuses):
        (self.st / "queue.json").write_text(json.dumps({"layer": "l", "tasks": [
            {"id": f"T{i}", "status": s} for i, s in enumerate(statuses)]}), encoding="utf-8")

    def widths(self, html):
        return dict(re.findall(r'aria-label="([^"]+)" aria-valuenow="(\d+)"', html))

    def test_overall_progress_counts_phases_plus_current_queue(self):
        self.queue(["done", "done", "todo", "blocked"])
        pr = status_page.progress(self.root, json.loads((self.st / "queue.json").read_text())["tasks"])
        self.assertEqual((pr["done"], pr["phases"], pr["tasks_done"], pr["tasks"]), (1, 4, 2, 4))
        self.assertAlmostEqual(pr["overall"], (1 + 0.5) / 4)
        self.assertEqual(pr["current"], "1. Loop")

    def test_page_shows_progress_and_usage_bars_and_refreshes(self):
        self.queue(["done", "todo"])
        (self.st / "meter.json").write_text(json.dumps({"2026-09-30": {"claude": 15_000_000, "codex": 1_000_000}}),
                                            encoding="utf-8")
        html = status_page.render(self.st, self.limits, now=self.now, local_tz=timezone.utc)
        w = self.widths(html)
        self.assertEqual(w["Whole build (roadmap)"], "38")
        self.assertEqual(w["Current phase&#x27;s queue"], "50")
        self.assertEqual(w["Claude tokens"], "75")
        self.assertEqual(w["Codex tokens"], "10")
        self.assertIn("15.0M of 20M", html)
        self.assertIn('http-equiv=refresh content=30', html)

    def test_usage_bar_colours(self):
        self.assertIn('class="fill"', status_page._bar("x", 1, 10, "", usage=True))
        self.assertIn('class="fill warn"', status_page._bar("x", 7, 10, "", usage=True))
        self.assertIn('class="fill bad"', status_page._bar("x", 9, 10, "", usage=True))
        self.assertIn('class="fill"', status_page._bar("x", 9.5, 10, ""))  # progress bars stay green

    def test_bars_clamp_and_survive_missing_files(self):
        self.assertIn("width:100.0%", status_page._bar("x", 50, 10, ""))
        self.assertIn("width:0.0%", status_page._bar("x", 5, 0, ""))
        html = status_page.render(self.st, self.limits, now=self.now, local_tz=timezone.utc)
        self.assertIn("Usage today", html)


if __name__ == "__main__":
    unittest.main()
