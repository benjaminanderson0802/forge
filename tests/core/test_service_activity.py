"""T1D1: activity awareness (design §5, D-019). Ben active = one agent, no browsers, lighter; idle = more."""
import unittest
from unittest.mock import patch

from core import service

LIM = {"max_parallel_idle": 3}


class ModeTests(unittest.TestCase):
    def test_recent_input_is_active_one_agent_no_browsers(self):
        m = service.mode(30.0, LIM)
        self.assertEqual(m["state"], "active")
        self.assertEqual(m["max_agents"], 1)
        self.assertFalse(m["browsers"])
        self.assertGreater(m["step_gap_s"], 0)

    def test_ten_minutes_idle_allows_max_parallel_idle(self):
        m = service.mode(600.0, LIM)
        self.assertEqual(m["state"], "idle")
        self.assertEqual(m["max_agents"], 3)
        self.assertTrue(m["browsers"])
        self.assertEqual(m["step_gap_s"], 0)

    def test_just_under_threshold_is_still_active(self):
        self.assertEqual(service.mode(599.9, LIM)["state"], "active")

    def test_unknown_idle_time_is_treated_as_active(self):
        """Fail safe: if we can't tell, assume Ben is at the PC."""
        m = service.mode(None, LIM)
        self.assertEqual(m["state"], "unknown")
        self.assertEqual(m["max_agents"], 1)
        self.assertFalse(m["browsers"])
        self.assertGreater(m["step_gap_s"], 0)

    def test_thresholds_come_from_limits(self):
        lim = {"max_parallel_idle": 5, "idle_after_s": 60, "active_step_gap_s": 7}
        self.assertEqual(service.mode(61, lim)["max_agents"], 5)
        self.assertEqual(service.mode(59, lim)["step_gap_s"], 7)

    def test_bad_max_parallel_idle_never_below_one(self):
        self.assertEqual(service.mode(10**6, {"max_parallel_idle": 0})["max_agents"], 1)
        self.assertEqual(service.mode(10**6, {})["max_agents"], 1)


class IdleSecondsTests(unittest.TestCase):
    def test_tick_arithmetic_handles_wraparound(self):
        self.assertEqual(service._idle_from_ticks(5000, 2000), 3.0)
        # GetTickCount wraps every ~49.7 days; last input just before the wrap, now just after
        self.assertAlmostEqual(service._idle_from_ticks(1000, 2**32 - 1000), 2.0)
        # GetTickCount comes back signed through ctypes' default c_int restype
        self.assertAlmostEqual(service._idle_from_ticks(-(2**31), 2**31 - 1000), 1.0)

    def test_not_windows_returns_none(self):
        with patch.object(service, "IS_WIN", False):
            self.assertIsNone(service.idle_seconds())

    def test_windows_api_failure_returns_none(self):
        class Broken:
            def __getattr__(self, name):
                raise OSError("no user32")
        with patch.object(service, "IS_WIN", True), patch.object(service, "_user32", lambda: Broken()):
            self.assertIsNone(service.idle_seconds())


if __name__ == "__main__":
    unittest.main()
