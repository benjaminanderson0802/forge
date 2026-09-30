"""T1C4: active time and the D-026 builder focus limit (20 minutes of builder time, enforced during a run).

Fakes only; the conductor clock is controlled. A fake agent "takes" time by advancing the clock inside its
script. Waiting (PAUSED, cap waits, gaps between steps) must never count as active or focus time."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import drift
from core.agents import ClaudeAgent, CodexAgent, FakeAgent

try:
    from tests.core.test_bootstrap import Harness
except ImportError:  # pragma: no cover
    from test_bootstrap import Harness

T0 = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)


class TimeoutArgTests(unittest.TestCase):
    def test_real_agents_cut_their_own_timeout_to_the_deadline(self):
        for agent in (ClaudeAgent(timeout_s=1800), CodexAgent(timeout_s=1800)):
            with patch("core.agents._resolve", return_value=["x"]), \
                    patch("core.agents.launch", side_effect=TimeoutError("agent timed out")) as launch:
                r = agent.run("p", Path("."), None, timeout_s=299.2)
                self.assertFalse(r.ok)
                self.assertEqual(launch.call_args[0][3], 300)
                agent.run("p", Path("."), None, timeout_s=99999)
                self.assertEqual(launch.call_args[0][3], 1800)
                agent.run("p", Path("."), None)
                self.assertEqual(launch.call_args[0][3], 1800)
                agent.run("p", Path("."), None, timeout_s=0.01)
                self.assertEqual(launch.call_args[0][3], 1)

    def test_fake_agent_records_the_deadline(self):
        a = FakeAgent(lambda p, c: ('{"status":"done"}', 1))
        a.run("p", Path("."), None, timeout_s=12)
        a.run("p", Path("."), None)
        self.assertEqual(a.timeouts, [12, None])


class FocusHarness(Harness):
    def setUp(self):
        super().setUp()
        self.now = T0
        self.builder_minutes = []  # minutes each builder run takes
        self.builder_value = 42

    def clocked(self, c):
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        return c

    def slow_builder(self, prompt, cwd):
        mins = self.builder_minutes.pop(0) if self.builder_minutes else 1
        self.now += timedelta(minutes=mins)
        (cwd / "feat.py").write_text(f"VALUE = {self.builder_value}\n", encoding="utf-8")
        return '{"status":"done"}', 1

    def setup_build(self, **agents):
        a = {"test_writer": self.write_tests, "builder": self.slow_builder}
        a.update(agents)
        c = self.clocked(self.init(agents=a))
        self.assertEqual(c.step(), "worked")  # tests accepted
        self.assertEqual(self.task_rec()["status"], "tests_ok")
        return c

    def task_rec(self, tid="T1"):
        q = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        return next(t for t in q["tasks"] if t["id"] == tid)

    def active(self):
        return drift.Activity(self.state).total()


class ActiveTimeTests(FocusHarness):
    def test_agent_runs_add_active_time(self):
        c = self.setup_build()
        before = self.active()
        self.builder_minutes = [7]
        c.step()
        self.assertGreaterEqual(self.active() - before, 7 * 60)
        self.assertLess(self.active() - before, 7 * 60 + 5)

    def test_pauses_cap_waits_and_gaps_add_nothing(self):
        c = self.setup_build()
        base = self.active()
        (self.state / "PAUSED").write_text("x")
        self.now += timedelta(hours=5)
        self.assertEqual(c.step(), "paused")
        (self.state / "PAUSED").unlink()
        c.limits["claude_daily_token_cap"] = 1
        c.meter.add("claude", 5)
        self.now += timedelta(hours=5)
        self.assertEqual(c.step(), "capped")
        self.now += timedelta(hours=5)  # an idle gap between steps
        self.assertEqual(self.active(), base)
        self.assertEqual(self.task_rec().get("focus_s", 0), 0)

    def test_readiness_probes_are_not_active_work(self):
        def slow_probe(p, cwd):
            self.now += timedelta(hours=3)
            return "ok", 0
        probes = {"claude": FakeAgent(slow_probe, provider="claude"), "codex": FakeAgent(slow_probe, provider="codex")}
        c = self.clocked(self.make_conductor(probes=probes))
        c.init_queue(self.layer, [self.task()])
        c._refresh_readiness(force=frozenset({"claude", "codex"}))
        self.assertEqual(self.active(), 0.0)

    def test_test_runs_add_active_time(self):
        import subprocess
        import sys
        c = self.setup_build()
        base = self.active()
        real = subprocess.run

        def slow_unittest(args, *a, **kw):
            if isinstance(args, list) and args[:3] == [sys.executable, "-m", "unittest"]:
                self.now += timedelta(minutes=3)
            return real(args, *a, **kw)
        with patch("core.bootstrap.subprocess.run", slow_unittest):
            c.step()
        self.assertGreaterEqual(self.active() - base, 3 * 60 + 60)  # the task tests + the builder's minute


class FocusLimitTests(FocusHarness):
    def test_builder_gets_the_remaining_focus_budget_as_its_deadline(self):
        self.builder_value = 41  # every attempt fails the judges
        self.builder_minutes = [12]
        c = self.setup_build()
        c.step()
        t = self.task_rec()
        self.assertEqual(t["focus_s"], 12 * 60)
        self.assertEqual(t["fails_since"], 1)
        self.assertEqual(self.team.builder.timeouts[-1], 1200)
        self.builder_minutes = [2]
        c.step()
        self.assertEqual(self.team.builder.timeouts[-1], 1200 - 12 * 60)

    def test_a_running_attempt_past_the_limit_hands_off_at_once(self):
        """One 25-minute attempt that would have passed: cut at the focus limit, Troubleshooter in the same
        step, nothing merged."""
        self.builder_minutes = [25]
        calls = []
        c = self.setup_build(troubleshooter=lambda p, cwd: (calls.append(p) or '{"kind":"fix","notes":"n"}', 1))
        c.step()
        t = self.task_rec()
        self.assertEqual(t["status"], "tests_ok")
        self.assertTrue(any("focus limit" in n for n in t["notes"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(t["troubleshoots"], 1)
        self.assertEqual(t["focus_s"], 0)  # the troubleshoot starts a fresh focus window
        self.assertNotIn("feat.py", self.branch_files())

    def test_exactly_at_the_limit_counts_as_exceeded(self):
        self.builder_minutes = [20]
        c = self.setup_build()
        c.step()
        self.assertTrue(any("focus limit" in n for n in self.task_rec()["notes"]))

    def test_under_the_limit_the_attempt_passes(self):
        self.builder_minutes = [19]
        c = self.setup_build()
        c.step()
        self.assertEqual(self.task_rec()["status"], "done")

    def test_pauses_between_attempts_do_not_eat_the_budget(self):
        self.builder_value = 41
        self.builder_minutes = [10]
        c = self.setup_build()
        c.step()
        (self.state / "PAUSED").write_text("x")
        self.now += timedelta(hours=6)
        c.step()
        (self.state / "PAUSED").unlink()
        self.builder_minutes = [1]
        c.step()
        self.assertEqual(self.team.builder.timeouts[-1], 600)

    def test_limit_comes_from_limits(self):
        self.builder_minutes = [3]
        c = self.setup_build()
        c.limits["builder_focus_s"] = 120
        c.step()
        self.assertEqual(self.team.builder.timeouts[-1], 120)
        self.assertTrue(any("focus limit" in n for n in self.task_rec()["notes"]))

    def test_rounds_used_up_and_focus_exceeded_blocks(self):
        self.builder_minutes = [25]
        c = self.setup_build()
        c._update("T1", troubleshoots=3)
        c.step()
        self.assertEqual(self.task_rec()["status"], "blocked")

    def test_spent_budget_before_an_attempt_hands_off_without_launching(self):
        c = self.setup_build()
        c._update("T1", focus_s=1500)
        n = len(self.team.builder.prompts)
        c.step()
        self.assertEqual(len(self.team.builder.prompts), n)
        t = self.task_rec()
        self.assertEqual(t["troubleshoots"], 1)
        self.assertEqual(t["focus_s"], 0)

    def test_a_late_blocker_claim_is_still_checked_and_handed_off(self):
        """Past the limit, a blocker claim without evidence is still logged as an easy-out, and the task goes to
        the Troubleshooter at once (not after a second failure)."""
        def late_blocker(prompt, cwd):
            self.now += timedelta(minutes=25)
            return '{"status":"blocked","summary":"too hard"}', 1
        c = self.setup_build(builder=late_blocker)
        c.step()
        t = self.task_rec()
        self.assertTrue((self.state / "easy_outs.jsonl").exists())
        self.assertTrue(any("easy out" in n for n in t["notes"]))
        self.assertEqual(t["troubleshoots"], 1)

    def test_ben_unblocking_resets_focus(self):
        c = self.setup_build()
        c._update("T1", status="blocked", focus_s=900)
        qid = c._ask("blocked", "Task T1 is blocked", "b", task="T1")
        code = json.loads((self.state / "questions.json").read_text())[qid]["code"]
        c._answer(qid, "try X", code)
        self.assertEqual(self.task_rec()["focus_s"], 0)


if __name__ == "__main__":
    unittest.main()
