"""T1D3: the kill switch works mid-cycle (design §5, D-024). KILL is checked before every agent launch, a running
agent is stopped when KILL appears, and a stop is never recorded as a failure or mistaken for tampering. Plan limit
windows and the runs-per-day cap are caps, never failures."""
import json
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from core import agents
from core.agents import ClaudeAgent, CodexAgent, FakeAgent, launch
from core.bootstrap import Capped, Stopped
from tests.core.test_bootstrap import Harness

PY = sys.executable
LIMIT_MSG = "You've hit your session limit · resets 6am (America/Chicago)"


class LaunchStopTests(unittest.TestCase):
    def test_should_stop_kills_a_running_agent_within_seconds(self):
        t0 = time.monotonic()
        stop_at = t0 + 0.5
        with self.assertRaises(agents.Stopped) as cm:
            launch([PY, "-c", "import time; time.sleep(60)"], Path("."), "", 120,
                   should_stop=lambda: time.monotonic() > stop_at)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIsInstance(cm.exception, TimeoutError)  # agents already turn TimeoutError into a failed result
        self.assertIn("stopped", str(cm.exception))

    def test_stop_kills_the_whole_tree(self):
        import tempfile
        d = Path(tempfile.mkdtemp()); flag = d / "child_alive"
        child = f"import time,pathlib; time.sleep(3); pathlib.Path(r'{flag}').write_text('x')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
        start = time.monotonic()
        with self.assertRaises(agents.Stopped):
            launch([PY, "-c", parent], Path("."), "", 120, should_stop=lambda: time.monotonic() - start > 1)
        time.sleep(4)
        self.assertFalse(flag.exists(), "child process survived the stop")

    def test_stdin_and_output_still_work_with_polling(self):
        code, out, _ = launch([PY, "-c", "import sys,time; d=sys.stdin.read(); time.sleep(2.5); print(len(d))"],
                              Path("."), "x" * 50_000, 30, should_stop=lambda: False)
        self.assertEqual((code, out.strip()), (0, "50000"))

    def test_timeout_still_applies_with_polling(self):
        with self.assertRaises(TimeoutError) as cm:
            launch([PY, "-c", "import time; time.sleep(30)"], Path("."), "", 1, should_stop=lambda: False)
        self.assertNotIsInstance(cm.exception, agents.Stopped)

    def test_kill_live_stops_every_running_agent(self):
        """The stall exit uses this before the process exits."""
        errors = []

        def go():
            try:
                launch([PY, "-c", "import time; time.sleep(60)"], Path("."), "", 120)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        th = threading.Thread(target=go)
        t0 = time.monotonic()
        th.start()
        deadline = time.monotonic() + 10
        while not agents.live_count() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(agents.live_count(), 1)
        agents.kill_live()
        th.join(15)
        self.assertFalse(th.is_alive())
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(agents.live_count(), 0)

    def test_agents_pass_should_stop_only_when_set(self):
        """Old launch signatures (fakes in other tests) keep working: no extra argument unless a stop check is set."""
        for cls in (ClaudeAgent, CodexAgent):
            a = cls(30)
            with patch("core.agents._resolve", return_value=["x"]), \
                    patch("core.agents.launch", return_value=(0, "{}", "")) as m:
                a.run("p", Path("."), None)
                self.assertNotIn("should_stop", m.call_args.kwargs)
                a.should_stop = lambda: False
                a.run("p", Path("."), None)
                self.assertIs(m.call_args.kwargs["should_stop"], a.should_stop)


class StoppableFake(FakeAgent):
    should_stop = None  # like ClaudeAgent/CodexAgent: the conductor wires the KILL check in


class ConductorKillTests(Harness):
    def kill(self):
        (self.state / "KILL").write_text("stopped by Stop Forge shortcut\n")

    def tamper_questions(self):
        qs = json.loads((self.state / "questions.json").read_text()) if (self.state / "questions.json").exists() else {}
        return [q for q in qs.values() if q.get("kind") == "tamper"]

    def test_killed_is_a_cap_so_every_stage_undoes_cleanly(self):
        self.assertTrue(issubclass(Stopped, Capped))

    def test_kill_before_launch_launches_nothing(self):
        called = []
        c = self.init(agents={"builder": lambda p, cwd: called.append(1) or ('{"status":"done"}', 1)})
        self.kill()
        with self.assertRaises(Stopped):
            c._call("builder", "do it", None)
        self.assertEqual(called, [])
        self.assertFalse((self.state / "runs").exists() and any((self.state / "runs").iterdir()))

    def test_kill_pressed_during_test_writer_run_undoes_the_stage_and_records_nothing(self):
        def writer(prompt, cwd):
            self.write_tests(prompt, cwd)
            self.kill()  # Ben presses Stop Forge while the agent is working
            return '{"files":["tests/core/test_feat.py"]}', 1

        c = self.init(agents={"test_writer": writer})
        c.step()
        t = c._task("T1")
        self.assertEqual(t["status"], "todo")
        self.assertFalse(any("rejected" in n for n in t["notes"]), t["notes"])
        self.assertNotIn("tests/core/test_feat.py", self.branch_files())
        self.assertEqual(self.tamper_questions(), [])
        self.assertEqual(self.mails, [])
        self.assertEqual(c.run(max_steps=3, sleep=lambda s: None), "killed")

    def test_kill_pressed_during_builder_run_is_not_a_failed_attempt(self):
        def builder(prompt, cwd):
            self.build_feature(prompt, cwd)
            self.kill()
            return '{"status":"done"}', 1

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        c.step()
        t = c._task("T1")
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(t.get("fail_signatures"), [])
        self.assertNotIn("feat.py", self.branch_files())
        self.assertEqual(self.tamper_questions(), [])

    def test_kill_plus_any_other_state_change_is_still_tampering(self):
        def builder(prompt, cwd):
            self.kill()
            (self.state / "queue.json").write_text('{"layer": "layer-1", "tasks": []}')
            return '{"status":"done"}', 1

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        self.assertEqual(c.step(), "killed")
        self.assertEqual(len(self.tamper_questions()), 1)

    def test_an_agent_stopped_by_kill_is_never_a_failure_even_if_kill_was_cleared(self):
        def builder(prompt, cwd):
            raise agents.Stopped("agent stopped: KILL")

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        c.step()
        t = c._task("T1")
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(t.get("fail_signatures"), [])

    def test_real_agents_get_the_kill_check_wired_in(self):
        seen = []

        def script(prompt, cwd):
            seen.append(agent.should_stop())
            self.kill()
            seen.append(agent.should_stop())
            return '{"status":"done"}', 1

        agent = StoppableFake(script, provider="claude")
        c = self.init()
        c.team.builder = agent
        with self.assertRaises(Stopped):
            c._call("builder", "go", None)
        self.assertEqual(seen, [False, True])

    def test_paused_before_launch_launches_nothing(self):
        """R42 + R49 unified: PAUSED is a stop flag too, so it also blocks a launch."""
        called = []
        c = self.init(agents={"builder": lambda p, cwd: called.append(1) or ('{"status":"done"}', 1)})
        (self.state / "PAUSED").write_text("paused\n")
        with self.assertRaises(Stopped):
            c._call("builder", "do it", None)
        self.assertEqual(called, [])

    def test_the_polled_stop_check_sees_paused_too(self):
        seen = []

        def script(prompt, cwd):
            seen.append(agent.should_stop())
            (self.state / "PAUSED").write_text("paused\n")
            seen.append(agent.should_stop())
            return '{"status":"done"}', 1

        agent = StoppableFake(script, provider="claude")
        c = self.init()
        c.team.builder = agent
        with self.assertRaises(Stopped):
            c._call("builder", "go", None)
        self.assertEqual(seen, [False, True])

    def test_kill_during_the_smoke_test_is_not_a_smoke_failure(self):
        from core import bootstrap

        def writer(prompt, cwd):
            self.kill()
            return '{"files":["smoke.txt"]}', 1

        c = self.init(agents={"test_writer": writer})
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertTrue(problems)
        self.assertIn("KILL", problems[0])
        self.assertFalse((self.state / "smoke_fail.json").exists())
        self.assertFalse((self.state / "smoke_ok.json").exists())
        self.assertEqual(self.tamper_questions(), [])


class LimitWindowConductorTests(Harness):
    def test_session_limit_is_a_hold_not_a_failed_attempt(self):
        def builder(prompt, cwd):
            raise RuntimeError(LIMIT_MSG)

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        self.assertEqual(c.step(), "capped")
        t = c._task("T1")
        self.assertEqual(t.get("fail_signatures"), [])
        self.assertIsNotNone(c.meter.held("claude"))
        self.assertEqual(c.step(), "capped")  # held: nothing is launched until the reset

    def test_planner_session_limit_does_not_reject_the_plan(self):
        """What blocked P1D three times on 2026-09-30."""
        def planner(prompt, cwd):
            raise RuntimeError(LIMIT_MSG)

        task = {"id": "P1", "kind": "plan", "title": "Plan", "section": "design",
                "plan_file": "docs/superpowers/plans/p.md"}
        c = self.init(task, agents={"planner": planner})
        self.assertEqual(c.step(), "capped")
        t = c._task("P1")
        self.assertEqual(t["status"], "todo")
        self.assertFalse(any("rejected" in n for n in t["notes"]), t["notes"])

    def test_main_r48_wording_is_the_same_hold(self):
        """R48 + R50 unified: one detector and one holds store (Meter) for both wordings."""
        def builder(prompt, cwd):
            raise RuntimeError("HTTP 429: Too Many Requests")

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        t0 = c.clock()
        self.assertEqual(c.step(), "capped")
        t1 = c.clock()
        self.assertEqual(c._task("T1").get("fail_signatures"), [])
        until = c.meter.held("claude")
        self.assertIsNotNone(until)
        # no reset time in the message: R48's 30 minutes (the one hold mechanism keeps R48's fallback)
        self.assertTrue(t0 + timedelta(minutes=30) <= until <= t1 + timedelta(minutes=30), (t0, until, t1))
        self.assertTrue(c._capped())
        holds = json.loads((self.state / "holds.json").read_text(encoding="utf-8"))
        self.assertEqual(set(holds), {"claude"})
        self.assertIn("limit hit for claude", (self.state / "errors.log").read_text(encoding="utf-8"))

    def test_runs_per_day_cap_stops_launches(self):
        c = self.init(agents={"test_writer": self.write_tests},
                      limits={"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9})
        self.assertEqual(c.step(), "worked")  # readiness probes and the test writer: every launch is counted
        runs = c.meter.runs_today()
        self.assertGreaterEqual(runs, 1)
        c.limits["agent_runs_per_day"] = runs
        self.assertEqual(c.step(), "capped")
        self.assertEqual(c.meter.runs_today(), runs)  # nothing more was launched


if __name__ == "__main__":
    unittest.main()
