# tests/core/test_usage.py
import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.usage import Meter

LIM = {"claude_daily_token_cap": 1000, "codex_daily_token_cap": 500}

class MeterTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.now = [datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)]
        self.m = lambda: Meter(self.d, clock=lambda: self.now[0])

    def test_providers_independent(self):
        m = self.m(); m.add("codex", 600)
        self.assertTrue(m.over("codex", LIM)); self.assertFalse(m.over("claude", LIM))

    def test_persists_across_instances(self):
        self.m().add("claude", 400); self.m().add("claude", 700)
        self.assertEqual(self.m().used_today("claude"), 1100); self.assertTrue(self.m().over("claude", LIM))

    def test_utc_rollover(self):
        self.m().add("claude", 5000)
        self.now[0] += timedelta(hours=1)
        self.assertEqual(self.m().used_today("claude"), 0); self.assertFalse(self.m().over("claude", LIM))

    def test_uncapped_provider_never_over(self):
        m = self.m(); m.add("fake", 10**9); self.assertFalse(m.over("fake", LIM))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError): self.m().add("claude", -1)

    def test_corrupt_file_starts_fresh_not_crash(self):
        (self.d / "meter.json").write_text("{not json")
        self.m().add("claude", 5); self.assertEqual(self.m().used_today("claude"), 5)

if __name__ == "__main__":
    unittest.main()
