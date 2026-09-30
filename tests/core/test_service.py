"""T1D2: the service loop around the conductor: heartbeat, stall exit, kill-aware sleep, wake, activity-aware
pacing and the status data (design §5, D-007, D-019, D-024)."""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from core import service
from core.bootstrap import Conductor
from tests.core.test_bootstrap import Harness


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeConductor:
    def __init__(self, state: Path):
        self.state = state
        self.kw = None

    def run(self, **kw):
        self.kw = kw
        return "killed"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state" / "bootstrap"
        self.root = root / "state" / "service"
        self.state.mkdir(parents=True)
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def heartbeat(self):
        return json.loads((self.root / "heartbeat.json").read_text(encoding="utf-8"))


class HealthTests(Base):
    def test_beat_writes_pid_phase_and_time_outside_the_fingerprinted_state(self):
        h = service.Health(self.root, {}, clock=self.clock)
        h.set_phase("step")
        self.assertFalse(h.beat())
        hb = self.heartbeat()
        self.assertEqual(hb["pid"], os.getpid())
        self.assertEqual(hb["phase"], "step")
        self.assertEqual(hb["at"], 1000.0)
        self.assertFalse((self.state / "heartbeat.json").exists())

    def test_a_step_running_past_step_stall_s_calls_on_stall_once(self):
        stalls = []
        h = service.Health(self.root, {"step_stall_s": 100}, clock=self.clock, on_stall=stalls.append)
        h.set_phase("step")
        self.clock.t += 99
        self.assertFalse(h.beat())
        self.clock.t += 2
        self.assertTrue(h.beat())
        self.assertEqual(len(stalls), 1)
        self.assertIn("stall", stalls[0])
        self.assertIn("stall", (self.root / "service.log").read_text(encoding="utf-8"))

    def test_long_sleep_or_pause_is_never_a_stall(self):
        stalls = []
        h = service.Health(self.root, {"step_stall_s": 100}, clock=self.clock, on_stall=stalls.append)
        for phase in ("sleep", "startup", "paused"):
            h.set_phase(phase)
            self.clock.t += 10**6
            self.assertFalse(h.beat())
        self.assertEqual(stalls, [])

    def test_new_phase_restarts_the_stall_clock(self):
        stalls = []
        h = service.Health(self.root, {"step_stall_s": 100}, clock=self.clock, on_stall=stalls.append)
        h.set_phase("step")
        self.clock.t += 90
        h.set_phase("sleep")
        h.set_phase("step")
        self.clock.t += 90
        self.assertFalse(h.beat())

    def test_heartbeat_write_failure_is_survived(self):
        """R12: a failed heartbeat is logged, never fatal."""
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_text("not a folder")
        h = service.Health(self.root, {}, clock=self.clock)
        self.assertFalse(h.beat())

    def test_thread_beats_until_stopped_then_marks_exited(self):
        h = service.Health(self.root, {"beat_s": 0.02})
        h.start()
        try:
            deadline = time.time() + 5
            while not (self.root / "heartbeat.json").exists() and time.time() < deadline:
                time.sleep(0.01)
            first = self.heartbeat()["at"]
            while self.heartbeat()["at"] == first and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreater(self.heartbeat()["at"], first)
        finally:
            h.stop()
        self.assertEqual(self.heartbeat()["phase"], "exited")


class NapTests(Base):
    def svc(self, limits=None, idle=lambda: None):
        self.slept = []

        def sleep(s):
            self.slept.append(s)
            self.clock.t += s
            if self.on_sleep:
                self.on_sleep(len(self.slept))

        self.on_sleep = None
        h = service.Health(self.root, limits or {}, clock=self.clock)
        return service.Service(FakeConductor(self.state), self.root, limits or {}, health=h, sleep=sleep,
                               clock=self.clock, idle=idle)

    def test_sleeps_in_ticks_until_the_deadline(self):
        s = self.svc()
        s.nap(7)
        self.assertEqual(sum(self.slept), 7)
        self.assertTrue(all(x <= 2 for x in self.slept))

    def test_kill_ends_the_sleep_within_a_tick(self):
        s = self.svc()
        self.on_sleep = lambda n: (self.state / "KILL").write_text("stop") if n == 2 else None
        s.nap(600)
        self.assertEqual(len(self.slept), 2)

    def test_wake_file_ends_the_sleep_and_is_consumed(self):
        s = self.svc()
        self.on_sleep = lambda n: service.wake(self.root) if n == 3 else None
        s.nap(600)
        self.assertEqual(len(self.slept), 3)
        self.assertFalse((self.root / "WAKE").exists())

    def test_no_sleep_at_all_when_already_killed(self):
        s = self.svc()
        (self.state / "KILL").write_text("stop")
        s.nap(600)
        self.assertEqual(self.slept, [])

    def test_phase_is_sleep_during_and_step_after(self):
        s = self.svc()
        seen = []
        self.on_sleep = lambda n: seen.append(s.health.phase)
        s.nap(3)
        self.assertEqual(set(seen), {"sleep"})
        self.assertEqual(s.health.phase, "step")


class PacingTests(NapTests):
    def test_active_ben_gets_a_pause_after_each_work_step(self):
        s = self.svc({"active_step_gap_s": 30}, idle=lambda: 5.0)
        s.on_step("worked")
        self.assertEqual(sum(self.slept), 30)

    def test_idle_ben_means_back_to_back_steps(self):
        s = self.svc({"active_step_gap_s": 30}, idle=lambda: 3600.0)
        s.on_step("worked")
        self.assertEqual(self.slept, [])

    def test_no_extra_pause_after_non_work_statuses(self):
        s = self.svc({"active_step_gap_s": 30}, idle=lambda: 5.0)
        for st in ("idle", "capped", "error", "paused"):
            s.on_step(st)
        self.assertEqual(self.slept, [])

    def test_on_step_publishes_mode_and_status(self):
        s = self.svc({"max_parallel_idle": 3}, idle=lambda: 3600.0)
        s.on_step("idle")
        st = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(st["last_status"], "idle")
        self.assertEqual(st["mode"]["state"], "idle")
        self.assertEqual(st["mode"]["max_agents"], 3)

    def test_serve_hands_the_conductor_the_kill_aware_sleep_and_hook(self):
        s = self.svc({"idle_poll_s": 45})
        self.assertEqual(s.serve(), "killed")
        kw = s.c.kw
        self.assertEqual(kw["sleep"], s.nap)
        self.assertEqual(kw["on_step"], s.on_step)
        self.assertEqual(kw["idle_sleep_s"], 45)
        self.assertEqual(kw["heartbeat"], self.state / "conductor.heartbeat")


class RunHookTests(Harness):
    def test_run_calls_on_step_with_every_status(self):
        c = self.init()
        seen = []
        c.run(max_steps=3, sleep=lambda s: None, on_step=seen.append)
        self.assertEqual(len(seen), 3)

    def test_a_failing_hook_is_logged_and_the_loop_goes_on(self):
        c = self.init()

        def bad(status):
            raise RuntimeError("hook broke")

        c.run(max_steps=2, sleep=lambda s: None, on_step=bad)
        self.assertIn("hook broke", (self.state / "errors.log").read_text(encoding="utf-8"))

    def test_run_stops_right_after_a_step_that_ended_with_kill_set(self):
        c = self.init()
        calls = []
        real_step = Conductor.step

        def step():
            calls.append(1)
            st = real_step(c)
            (self.state / "KILL").write_text("stop pressed mid-step\n")
            return st

        c.step = step
        self.assertEqual(c.run(max_steps=5, sleep=lambda s: None), "killed")
        self.assertEqual(len(calls), 1)


class SnapshotTests(Base):
    def test_snapshot_has_what_the_status_page_and_digest_need(self):
        (self.state / "queue.json").write_text(json.dumps({"layer": "layer-1", "tasks": [
            {"id": "T1", "status": "done"}, {"id": "T2", "status": "tests_ok"}, {"id": "T3", "status": "todo"},
            {"id": "T4", "status": "blocked"}]}))
        (self.state / "questions.json").write_text(json.dumps({"q-1": {"kind": "blocked", "status": "open"},
                                                               "q-2": {"kind": "gate", "status": "answered"}}))
        (self.state / "KILL").write_text("stopped by Stop Forge shortcut\n")
        (self.state / "errors.log").write_text("".join(f"line {i}\n" for i in range(20)))
        now = datetime(2026, 9, 30, 22, 15, tzinfo=timezone.utc)
        (self.state / "meter.json").write_text(json.dumps({"2026-09-30": {"claude": 700}}))
        h = service.Health(self.root, {}, clock=lambda: now.timestamp())
        h.set_phase("step")
        h.beat()
        snap = service.snapshot(self.state, self.root, {"claude_daily_token_cap": 1000, "codex_daily_token_cap": 50},
                                now=now)
        self.assertEqual(snap["kill"], "stopped by Stop Forge shortcut")
        self.assertIsNone(snap["paused"])
        self.assertEqual(snap["tasks"], {"done": 1, "tests_ok": 1, "todo": 1, "blocked": 1})
        self.assertEqual(snap["current"], "T2")
        self.assertEqual(snap["open_questions"], 1)
        self.assertEqual(snap["caps"]["claude"]["used"], 700)
        self.assertEqual(snap["caps"]["claude"]["cap"], 1000)
        self.assertFalse(snap["caps"]["claude"]["over"])
        self.assertEqual(snap["caps"]["codex"]["used"], 0)
        self.assertEqual(snap["cap_resets_at"], "2026-10-01T00:00:00+00:00")
        self.assertTrue(snap["running"])
        self.assertEqual(snap["phase"], "step")
        self.assertEqual(snap["recent_errors"], [f"line {i}" for i in range(15, 20)])

    def test_snapshot_of_an_empty_install_does_not_crash(self):
        snap = service.snapshot(self.state, self.root, {}, now=datetime.now(timezone.utc))
        self.assertFalse(snap["running"])
        self.assertEqual(snap["tasks"], {})
        self.assertIsNone(snap["kill"])

    def test_stale_heartbeat_is_not_running(self):
        h = service.Health(self.root, {}, clock=lambda: 1000.0)
        h.beat()
        snap = service.snapshot(self.state, self.root, {"heartbeat_stale_s": 600},
                                now=datetime.fromtimestamp(1000.0 + 601, timezone.utc))
        self.assertFalse(snap["running"])


class CliTests(Base):
    def forge(self):
        return Path(self.tmp.name)

    def test_stop_writes_kill_with_a_reason(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["stop", "--reason", "status page button"], forge=self.forge()), 0)
        self.assertIn("status page button", (self.state / "KILL").read_text(encoding="utf-8"))

    def test_request_stop_never_erases_an_existing_reason(self):
        service.request_stop(self.state, "email STOP")
        service.request_stop(self.state, "button")
        text = (self.state / "KILL").read_text(encoding="utf-8")
        self.assertIn("email STOP", text)

    def test_wake_and_status(self):
        self.assertEqual(service.main(["wake"], forge=self.forge()), 0)
        self.assertTrue((self.root / "WAKE").exists())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(service.main(["status"], forge=self.forge()), 0)
        self.assertIn("running", json.loads(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
