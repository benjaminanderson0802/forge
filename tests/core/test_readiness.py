import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.readiness import CHECKS, run_checks


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.d = Path(temporary.name)

    def test_writes_map(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        result = run_checks(
            self.d,
            {"a": lambda: (True, "fine"), "b": lambda: (False, "down")},
            5,
            clock=lambda: now,
        )
        saved = json.loads((self.d / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {
            "a": {"ok": True, "detail": "fine", "checked_at": now.isoformat()},
            "b": {"ok": False, "detail": "down", "checked_at": now.isoformat()},
        })
        self.assertEqual(result, saved)

    def test_check_exception_isolated(self):
        def boom():
            raise RuntimeError("dns down")

        result = run_checks(self.d, {"x": boom, "y": lambda: (True, "ok")}, 5)
        self.assertFalse(result["x"]["ok"])
        self.assertIn("dns down", result["x"]["detail"])
        self.assertTrue(result["y"]["ok"])

    def test_check_timeout(self):
        def slow():
            time.sleep(5)
            return True, ""

        result = run_checks(self.d, {"slow": slow, "fast": lambda: (True, "")}, 1)
        self.assertFalse(result["slow"]["ok"])
        self.assertIn("timed out", result["slow"]["detail"])
        self.assertTrue(result["fast"]["ok"])

    def test_ttl_reuses_fresh_ok_results(self):
        calls = []

        def ai():
            calls.append(1)
            return True, "ok"

        now = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
        first = run_checks(self.d, {"ai": ai}, 5, clock=lambda: now[0], ttl={"ai": 6})
        now[0] += timedelta(hours=2)
        cached = run_checks(self.d, {"ai": ai}, 5, clock=lambda: now[0], ttl={"ai": 6})
        self.assertEqual(len(calls), 1)
        self.assertEqual(cached, first)
        now[0] += timedelta(hours=5)
        refreshed = run_checks(self.d, {"ai": ai}, 5, clock=lambda: now[0], ttl={"ai": 6})
        self.assertEqual(len(calls), 2)
        self.assertEqual(refreshed["ai"]["checked_at"], now[0].isoformat())

    def test_failed_results_never_reused(self):
        calls = []

        def ai():
            calls.append(1)
            return False, "not logged in"

        run_checks(self.d, {"ai": ai}, 5, ttl={"ai": 6})
        run_checks(self.d, {"ai": ai}, 5, ttl={"ai": 6})
        self.assertEqual(len(calls), 2)

    def test_real_check_names(self):
        for name in [
            "git", "github", "claude", "codex", "gmail", "docker", "n8n",
            "ollama", "python_libs", "browser",
        ]:
            with self.subTest(name=name):
                self.assertIn(name, CHECKS)
                self.assertTrue(callable(CHECKS[name]))


if __name__ == "__main__":
    unittest.main()
