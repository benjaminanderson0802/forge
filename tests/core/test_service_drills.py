"""T1D4: the Layer 1D gate drills (design §7): kill mid-step, restart after a crash or a hang, and active vs idle
behaviour. Real processes where the behaviour depends on them (an agent process, a crashed lock holder)."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

from core import service
from core.agents import ClaudeAgent
from core.bootstrap import acquire_lock
from tests.core.test_bootstrap import Harness

PY = sys.executable
ROOT = Path(__file__).resolve().parents[2]


class DrillKillMidStep(Harness):
    def test_stop_pressed_while_a_real_agent_process_runs(self):
        """A real (fake-CLI) builder process is working; Ben presses Stop Forge. The process tree dies within
        seconds, the attempt is undone, nothing counts as a failure or as tampering, and the loop ends."""
        fake_cli = Path(self.tmp.name) / "slow_claude.py"
        fake_cli.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(120)\n", encoding="utf-8")
        c = self.advance_to_build(agents={"test_writer": self.write_tests})
        c.team.builder = ClaudeAgent(300, cmd=[PY, str(fake_cli)])
        timer = threading.Timer(1.5, lambda: (self.state / "KILL").write_text("stopped by Stop Forge shortcut\n"))
        timer.start()
        t0 = time.monotonic()
        status = c.run(max_steps=5, sleep=lambda s: None)
        took = time.monotonic() - t0
        timer.cancel()
        self.assertEqual(status, "killed")
        self.assertLess(took, 30, "the agent was not stopped promptly")
        t = c._task("T1")
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(t.get("fail_signatures"), [])
        qs = json.loads((self.state / "questions.json").read_text()) if (self.state / "questions.json").exists() else {}
        self.assertFalse([q for q in qs.values() if q.get("kind") == "tamper"])
        self.assertEqual(self.mails, [])


class WatchdogBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.forge = Path(self.tmp.name)
        self.st = self.forge / "state" / "bootstrap"
        self.root = self.forge / "state" / "service"
        self.st.mkdir(parents=True)
        self.cmds = []

    def tearDown(self):
        self.tmp.cleanup()

    def beat(self, at: float, phase="step", pid=12345):
        service._write_json(self.root / "heartbeat.json", {"pid": pid, "at": at, "phase": phase, "since": at})

    def dog(self, now: float, lock_free: bool | None = None):
        kw = {"lock_free": (lambda: lock_free)} if lock_free is not None else {}
        return service.watchdog(self.forge, {"heartbeat_stale_s": 600}, now=now,
                                run=lambda args: self.cmds.append(list(args)) or 0, **kw)


class DrillRestart(WatchdogBase):
    def test_fresh_heartbeat_is_left_alone(self):
        self.beat(1000.0)
        self.assertEqual(self.dog(1000.0 + 599, lock_free=False), "ok")
        self.assertEqual(self.cmds, [])

    def test_crashed_service_is_started_again(self):
        self.beat(1000.0)
        self.assertEqual(self.dog(1000.0 + 601, lock_free=True), "started")
        self.assertEqual(self.cmds, [["schtasks", "/Run", "/TN", "Forge conductor"]])

    def test_hung_service_is_ended_and_restarted(self):
        """The task instance is still 'running', so the 5-minute trigger alone would never replace it."""
        self.beat(1000.0)
        self.assertEqual(self.dog(1000.0 + 601, lock_free=False), "restarted")
        self.assertEqual(self.cmds, [["schtasks", "/End", "/TN", "Forge conductor"],
                                     ["schtasks", "/Run", "/TN", "Forge conductor"]])
        self.assertIn("restart", (self.root / "service.log").read_text(encoding="utf-8"))

    def test_kill_switch_wins_over_the_watchdog(self):
        self.beat(1000.0)
        (self.st / "KILL").write_text("stopped\n")
        self.assertEqual(self.dog(10**9, lock_free=True), "stopped")
        self.assertEqual(self.cmds, [])

    def test_lock_held_with_no_heartbeat_is_left_alone(self):
        """A conductor from before 1D, or a manual `bootstrap step`: never killed on a guess."""
        self.assertEqual(self.dog(10**9, lock_free=False), "unknown")
        self.assertEqual(self.cmds, [])

    def test_a_real_crash_releases_the_lock_and_the_watchdog_restarts(self):
        """A process that holds the conductor lock and dies without cleanup (os._exit) must not block a restart."""
        script = textwrap.dedent(f"""
            import os, sys, time, json
            sys.path.insert(0, {str(ROOT)!r})
            from pathlib import Path
            from core.bootstrap import acquire_lock
            h = acquire_lock(Path({str(self.st)!r}))
            assert h is not None
            Path({str(self.root)!r}).mkdir(parents=True, exist_ok=True)
            Path({str(self.root / 'heartbeat.json')!r}).write_text(json.dumps({{"pid": os.getpid(), "at": time.time() - 3600, "phase": "step", "since": time.time()}}))
            print("locked", flush=True)
            time.sleep(1)
            os._exit(3)
        """)
        p = subprocess.Popen([PY, "-c", script], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        self.assertIsNone(acquire_lock(self.st), "the lock should be held while the process lives")
        p.wait(30)
        p.stdout.close()
        self.assertEqual(self.dog(time.time()), "started")  # the real lock check
        self.assertEqual(self.cmds, [["schtasks", "/Run", "/TN", "Forge conductor"]])

    def test_watchdog_cli_never_raises_when_schtasks_is_missing(self):
        self.beat(1000.0)
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            rc = service.main(["watchdog"], forge=self.forge)
        self.assertEqual(rc, 0)


class DrillActiveVsIdle(Harness):
    def drive(self, idle_s):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        clock = [1000.0]
        sleeps = []
        svc = service.Service(c, Path(self.tmp.name) / "service", {"active_step_gap_s": 30, "idle_poll_s": 60,
                                                                    "max_parallel_idle": 3},
                              sleep=lambda s: (sleeps.append((svc.health.last_status, s)),
                                               clock.__setitem__(0, clock[0] + s)),
                              clock=lambda: clock[0], idle=lambda: idle_s)
        steps = []
        real_on_step = svc.on_step

        def on_step(status):
            steps.append(status)
            real_on_step(status)
            if len(steps) >= 3:
                (self.state / "KILL").write_text("drill over\n")

        svc.on_step = on_step
        self.assertEqual(svc.serve(), "killed")
        return steps, sleeps, svc

    def test_active_ben_one_agent_and_a_pause_after_every_work_step(self):
        steps, sleeps, svc = self.drive(idle_s=20.0)
        self.assertEqual(steps[0], "worked")
        self.assertEqual(sum(s for st, s in sleeps if st == "worked"), 30 * steps.count("worked"))
        self.assertEqual(svc.mode["max_agents"], 1)
        self.assertFalse(svc.mode["browsers"])

    def test_idle_ben_back_to_back_work_and_parallel_allowance(self):
        steps, sleeps, svc = self.drive(idle_s=3600.0)
        self.assertEqual(steps[0], "worked")
        self.assertEqual([x for x in sleeps if x[0] == "worked"], [])
        self.assertEqual(svc.mode["max_agents"], 3)
        st = json.loads((Path(self.tmp.name) / "service" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(st["mode"]["state"], "idle")


class DrillNumbering(unittest.TestCase):
    """1C, 1D and 1E each added drills on their own branch; after the merge the numbers are unique and sequential."""

    def test_drill_numbers_are_unique_and_sequential(self):
        from drills import run_drills
        nums = [n for n, _, _ in run_drills.DRILLS]
        self.assertEqual(nums, list(range(1, len(nums) + 1)))
        self.assertGreaterEqual(len(nums), 21)
        run_drills.check_numbering()

    def test_a_collision_is_refused(self):
        from drills import run_drills
        with self.assertRaises(ValueError):
            run_drills.check_numbering([(1, "a", None), (2, "b", None), (2, "c", None)])


if __name__ == "__main__":
    unittest.main()
