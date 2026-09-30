"""T1D3: the daily cap, extended. A plan limit window puts a provider on hold until its reset (instead of failing
the attempt), and a hard cap on agent launches per day bounds the damage of any loop (D-020, D-035)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.usage import Meter, limit_hold_until

LIM = {"claude_daily_token_cap": 1000, "codex_daily_token_cap": 500}
NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)  # 15:00 in Chicago (CDT, UTC-5)


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.now = [NOW]
        self.m = lambda: Meter(self.d, clock=lambda: self.now[0])

    def test_hold_makes_the_provider_over_until_it_expires(self):
        self.m().hold("claude", NOW + timedelta(hours=2))
        self.assertTrue(self.m().over("claude", LIM))
        self.assertFalse(self.m().over("codex", LIM))
        self.assertEqual(self.m().held("claude"), NOW + timedelta(hours=2))
        self.now[0] += timedelta(hours=2, seconds=1)
        self.assertFalse(self.m().over("claude", LIM))
        self.assertIsNone(self.m().held("claude"))

    def test_hold_applies_even_to_a_provider_with_no_token_cap(self):
        self.m().hold("fake", NOW + timedelta(minutes=10))
        self.assertTrue(self.m().over("fake", LIM))

    def test_corrupt_holds_file_means_no_hold_not_a_crash(self):
        (self.d / "holds.json").write_text("{nope")
        self.assertFalse(self.m().over("claude", LIM))
        self.m().hold("claude", NOW + timedelta(hours=1))
        self.assertTrue(self.m().over("claude", LIM))

    def test_a_later_hold_extends_an_earlier_one_never_shortens_it(self):
        self.m().hold("claude", NOW + timedelta(hours=3))
        self.m().hold("claude", NOW + timedelta(hours=1))
        self.assertEqual(self.m().held("claude"), NOW + timedelta(hours=3))


class RunsCapTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.m = Meter(self.d, clock=lambda: NOW)

    def run_dir(self, stamp: str):
        (self.d / "runs" / f"{stamp}-builder-abc123").mkdir(parents=True)

    def test_runs_today_counts_todays_run_records_only(self):
        self.run_dir("20260930T010203"); self.run_dir("20260930T235959"); self.run_dir("20260929T120000")
        self.assertEqual(self.m.runs_today(), 2)

    def test_no_runs_folder_is_zero(self):
        self.assertEqual(self.m.runs_today(), 0)

    def test_runs_cap_makes_every_provider_over(self):
        lim = dict(LIM, agent_runs_per_day=2)
        self.run_dir("20260930T010203")
        self.assertFalse(self.m.over("claude", lim))
        self.run_dir("20260930T010204")
        self.assertTrue(self.m.over("claude", lim))
        self.assertTrue(self.m.over("codex", lim))

    def test_without_the_limit_runs_are_uncapped(self):
        for i in range(30):
            self.run_dir(f"20260930T0100{i:02d}")
        self.assertFalse(self.m.over("claude", LIM))


class LimitWindowTests(unittest.TestCase):
    def test_claude_session_limit_with_reset_time_and_zone(self):
        until = limit_hold_until("You've hit your session limit · resets 6am (America/Chicago)", NOW)
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/Chicago")
        except Exception:  # noqa: BLE001 - no tz database (Windows without tzdata): the 1-hour fallback
            self.assertEqual(until, NOW + timedelta(hours=1))
            return
        self.assertEqual(until, datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc))  # 6am CDT tomorrow

    def test_reset_later_today(self):
        until = limit_hold_until("Claude usage limit reached. resets 5:30pm (America/Chicago)", NOW)
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/Chicago")
        except Exception:  # noqa: BLE001
            self.assertEqual(until, NOW + timedelta(hours=1))
            return
        self.assertEqual(until, datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc))

    def test_limit_without_a_readable_reset_holds_one_hour(self):
        self.assertEqual(limit_hold_until("You've hit your usage limit. Try again later.", NOW),
                         NOW + timedelta(hours=1))
        self.assertEqual(limit_hold_until("session limit, resets 6am (Not/AZone)", NOW), NOW + timedelta(hours=1))

    def test_ordinary_failures_are_not_limit_windows(self):
        for text in ("", None, "Codex run failed (exit 1): schema invalid", "agent timed out after 1800s",
                     "AssertionError: 41 != 42", "Not logged in · Please run /login"):
            self.assertIsNone(limit_hold_until(text, NOW), text)

    def test_hold_is_bounded_to_a_day(self):
        until = limit_hold_until("You've hit your weekly limit · resets Oct 6, 6am (America/Chicago)", NOW)
        self.assertLessEqual(until, NOW + timedelta(hours=24))
        self.assertGreater(until, NOW)


if __name__ == "__main__":
    unittest.main()
