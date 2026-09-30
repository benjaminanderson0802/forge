"""T1B2b: readiness runs before every cycle and session; guarded AI probes; map and dead ends in prompts.

Fakes only: core.agents.launch raises if anything real would start."""
import json
import re
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core import bootstrap
from core import readiness as rd
from core.agents import FakeAgent
from core.bootstrap import Conductor, Team
from core.usage import Meter

try:
    from tests.core.test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes
except ImportError:  # pragma: no cover
    from test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes

T0 = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
ROLES = ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper", "planner")


class ReadinessHarness(Harness):
    def setUp(self):
        super().setUp()
        launch = patch("core.agents.launch", side_effect=AssertionError("a real process was launched"))
        launch.start()
        self.addCleanup(launch.stop)
        self.now = T0
        self.calls = Counter()

    def counting_checks(self, overrides=None):
        overrides = overrides or {}

        def make(name):
            def check():
                self.calls[name] += 1
                result = overrides.get(name, (True, f"{name} fine"))
                if isinstance(result, Exception):
                    raise result
                return result
            return check
        return {name: make(name) for name in HEALTHY_CHECKS}

    def probe(self, provider, answer=("ok", 0)):
        def script(prompt, cwd):
            return answer(prompt, cwd) if callable(answer) else answer
        return FakeAgent(script, provider=provider)

    def conductor(self, checks=None, probes=None, limits=None, agents=None):
        c = self.make_conductor(agents=agents, limits=limits,
                                checks=self.counting_checks() if checks is None else checks,
                                probes=probes)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        return c

    def cap_map(self):
        return json.loads((self.state / "capabilities.json").read_text(encoding="utf-8"))


class EveryStepRefreshes(ReadinessHarness):
    def test_each_step_calls_every_uncached_plain_check_again(self):
        c = self.conductor()
        for n in (1, 2, 3):
            self.now += timedelta(seconds=5)
            self.assertEqual(c.step(), "idle")
            for name in HEALTHY_CHECKS:
                if rd.ok_ttl_s(name, c.limits) == 0:  # gmail/browser are the permitted per-check caches
                    self.assertEqual(self.calls[name], n, name)
        self.assertEqual(self.calls["gmail"], 1)
        self.assertEqual(self.calls["browser"], 1)
        self.now += timedelta(seconds=rd.ok_ttl_s("gmail", c.limits) + 1)
        c.step()
        self.assertEqual(self.calls["gmail"], 2)
        self.assertEqual(self.calls["browser"], 2)

    def test_same_instant_steps_still_rerun_checks(self):
        c = self.conductor()
        c.step()
        c.step()
        self.assertEqual(self.calls["git"], 2)
        self.assertEqual(self.calls["docker"], 2)

    def test_map_has_ok_detail_and_checked_at_for_every_check_and_probe(self):
        c = self.conductor()
        c.step()
        m = self.cap_map()
        self.assertEqual(set(m), set(HEALTHY_CHECKS) | {"claude", "codex"})
        for name, entry in m.items():
            self.assertIs(entry["ok"], True, name)
            self.assertIsInstance(entry["detail"], str)
            self.assertEqual(datetime.fromisoformat(entry["checked_at"]), T0)
        self.assertIsNone(rd.broken(m["claude"], self.now, 60))

    def test_raising_check_is_stored_as_failed(self):
        c = self.conductor(checks=self.counting_checks({"docker": RuntimeError("daemon exploded")}))
        c.step()
        entry = self.cap_map()["docker"]
        self.assertIs(entry["ok"], False)
        self.assertIn("daemon exploded", entry["detail"])

    def test_refresh_runs_before_any_task_or_drift(self):
        order = []
        checks = {"git": lambda: order.append("check") or (True, "ok")}

        def writer(prompt, cwd):
            order.append("agent")
            return self.write_tests(prompt, cwd)

        c = self.init(agents={"test_writer": writer}, checks=checks)
        c.clock = lambda: self.now
        c.step()
        self.assertEqual(order[:2], ["check", "agent"])
        q = c._queue()
        q["drift_due"] = True
        c._save_queue(q)
        order.clear()
        c.team.drift_keeper = FakeAgent(lambda p, cwd: (order.append("agent") or '{"status":"ok"}', 1), "claude")
        c.step()
        self.assertEqual(order, ["check", "agent"])

    def test_result_keeps_only_configured_names(self):
        c = self.conductor()
        rd.write_map(self.state / "capabilities.json",
                     {"stale_old_name": {"ok": True, "detail": "x", "checked_at": T0.isoformat()}})
        result = c._refresh_readiness()
        self.assertNotIn("stale_old_name", result)
        self.assertNotIn("stale_old_name", self.cap_map())
        self.assertEqual(result, self.cap_map())


class ProbeCachingAndCaps(ReadinessHarness):
    def test_ok_probe_reused_inside_ttl_and_relaunched_after(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        c.step()
        self.now += timedelta(minutes=10)
        c.step()
        self.assertEqual(len(probes["claude"].prompts), 1)
        self.assertEqual(probes["claude"].prompts[0], rd.PROBE_PROMPT)
        self.now = T0 + timedelta(seconds=rd.ok_ttl_s("claude", c.limits) + 1)
        c.step()
        self.assertEqual(len(probes["claude"].prompts), 2)
        self.assertEqual(len(probes["codex"].prompts), 2)

    def test_failed_probe_not_relaunched_inside_retry_window(self):
        probes = {"claude": self.probe("claude", ("I refuse", 0)), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        c.step()
        self.assertIs(self.cap_map()["claude"]["ok"], False)
        self.now += timedelta(seconds=rd.fail_retry_s("claude", c.limits) - 5)
        c.step()
        self.assertEqual(len(probes["claude"].prompts), 1)
        self.now += timedelta(seconds=10)
        c.step()
        self.assertEqual(len(probes["claude"].prompts), 2)

    def test_failed_probe_with_invalid_timestamp_is_relaunched(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        rd.write_map(self.state / "capabilities.json",
                     {"claude": {"ok": False, "detail": "x", "checked_at": "2026-09-30T08:00:00"}})
        c._refresh_readiness()
        self.assertEqual(len(probes["claude"].prompts), 1)

    def test_force_relaunches_probe_and_plain_check(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        c._refresh_readiness()
        self.assertEqual(self.calls["gmail"], 1)
        c._refresh_readiness(names={"claude", "gmail"}, force={"claude", "gmail"})
        self.assertEqual(len(probes["claude"].prompts), 2)
        self.assertEqual(self.calls["gmail"], 2)
        self.assertEqual(len(probes["codex"].prompts), 1)

    def test_targeted_refresh_touches_only_named_and_preserves_others(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes, checks=self.counting_checks({"docker": (False, "down")}))
        c._refresh_readiness()
        before = self.cap_map()
        self.now += timedelta(seconds=30)
        result = c._refresh_readiness(names={"git"})
        self.assertEqual(self.calls["git"], 2)
        self.assertEqual(self.calls["docker"], 1)
        self.assertEqual(result["docker"], before["docker"])
        self.assertEqual(result["claude"], before["claude"])
        self.assertEqual(datetime.fromisoformat(result["git"]["checked_at"]), self.now)

    def test_capped_provider_probe_never_launched_and_old_entry_kept(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes, limits={"claude_daily_token_cap": 10, "codex_daily_token_cap": 10**9})
        old = {"ok": False, "detail": "old", "checked_at": (T0 - timedelta(days=1)).isoformat()}
        rd.write_map(self.state / "capabilities.json", {"claude": old})
        c.meter.add("claude", 10)
        meter_before = (self.state / "meter.json").read_bytes()
        result = c._refresh_readiness()
        self.assertEqual(probes["claude"].prompts, [])
        self.assertEqual((self.state / "meter.json").read_bytes(), meter_before)
        self.assertEqual(result["claude"], old)
        self.assertEqual(len(probes["codex"].prompts), 1)
        # forcing never overrides a cap
        c._refresh_readiness(names={"claude"}, force={"claude"})
        self.assertEqual(probes["claude"].prompts, [])

    def test_capped_probe_without_old_entry_stays_absent(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes, limits={"claude_daily_token_cap": 10, "codex_daily_token_cap": 10**9})
        c.meter.add("claude", 10)
        self.assertNotIn("claude", c._refresh_readiness())

    def test_capped_step_calls_no_check(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes, limits={"claude_daily_token_cap": 10, "codex_daily_token_cap": 10**9})
        c.meter.add("claude", 10)
        self.assertEqual(c.step(), "capped")
        self.assertEqual(sum(self.calls.values()), 0)
        self.assertEqual(probes["codex"].prompts, [])
        self.assertFalse((self.state / "capabilities.json").exists())

    def test_no_refresh_while_killed(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        (self.state / "KILL").write_text("stop")
        c._refresh_readiness()
        self.assertEqual(sum(self.calls.values()), 0)
        self.assertEqual(probes["claude"].prompts, [])


class ProbeRunsAreGuarded(ReadinessHarness):
    def test_probe_tokens_are_metered(self):
        probes = {"claude": self.probe("claude", ("ok", 7)), "codex": self.probe("codex", ("ok", 3))}
        c = self.conductor(probes=probes)
        c.step()
        self.assertEqual(c.meter.used_today("claude"), 7)
        self.assertEqual(c.meter.used_today("codex"), 3)
        self.assertIn("7 tokens", self.cap_map()["claude"]["detail"])

    def test_probe_run_writes_run_record_in_probe_folder(self):
        seen = []
        probes = {"claude": self.probe("claude", lambda p, cwd: seen.append(cwd) or ("ok", 0)),
                  "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        c.step()
        runs = [d for d in (self.state / "runs").iterdir() if "probe-claude" in d.name]
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0] / "prompt.md").read_text(encoding="utf-8"), rd.PROBE_PROMPT)
        out = json.loads((runs[0] / "output.json").read_text(encoding="utf-8"))
        self.assertTrue(out["ok"])
        self.assertEqual(seen, [self.work / "_probe"])
        self.assertTrue((self.work / "_probe").is_dir())

    def test_probe_writing_state_kills(self):
        def tamper(prompt, cwd):
            (self.state / "sneaky.json").write_text("x", encoding="utf-8")
            return "ok", 0
        probes = {"claude": self.probe("claude", tamper), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        self.assertEqual(c.step(), "killed")
        self.assertTrue((self.state / "KILL").exists())
        self.assertTrue(any("tamper" in s.lower() for s, b in self.mails))
        self.assertEqual(c.step(), "killed")

    def test_probe_crash_is_a_failed_entry(self):
        def boom(prompt, cwd):
            raise RuntimeError("no such binary")
        probes = {"claude": self.probe("claude", boom), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        c.step()
        self.assertIs(self.cap_map()["claude"]["ok"], False)


class SessionStart(ReadinessHarness):
    def test_session_start_refreshes_on_clean_start(self):
        probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
        c = self.conductor(probes=probes)
        result = c.session_start()
        self.assertEqual(self.calls["git"], 1)
        self.assertEqual(len(probes["claude"].prompts), 1)
        self.assertEqual(result, self.cap_map())

    def test_session_start_does_nothing_when_killed_paused_or_capped(self):
        for blocker in ("KILL", "PAUSED", "cap"):
            with self.subTest(blocker=blocker):
                self.calls.clear()
                probes = {"claude": self.probe("claude"), "codex": self.probe("codex")}
                c = self.conductor(probes=probes,
                                   limits={"claude_daily_token_cap": 10, "codex_daily_token_cap": 10**9})
                for f in ("KILL", "PAUSED"):
                    (self.state / f).unlink(missing_ok=True)
                (self.state / "capabilities.json").unlink(missing_ok=True)
                c.meter = Meter(self.state.parent / f"meter-{blocker}", c.clock)
                if blocker == "cap":
                    c.meter.add("claude", 10)
                else:
                    (self.state / blocker).write_text("x")
                self.assertEqual(c.session_start(), {})
                self.assertEqual(sum(self.calls.values()), 0)
                self.assertEqual(probes["claude"].prompts + probes["codex"].prompts, [])
                self.assertFalse((self.state / "capabilities.json").exists())


class MainRunSessionStart(ReadinessHarness):
    def setUp(self):
        super().setUp()
        self.state = self.repo / "state" / "bootstrap"
        self.state.mkdir(parents=True)
        self.make_conductor()
        self.c.clock = lambda: self.now
        self.c._write("smoke_ok.json", {"at": (self.now - timedelta(hours=25)).isoformat()})
        self.events = []
        self.inbox = Mock(side_effect=lambda: self.events.append("inbox") or list(self.messages))
        real_init = Conductor.__init__

        def timed_init(conductor, *args, **kwargs):
            kwargs["clock"] = lambda: self.now
            real_init(conductor, *args, **kwargs)

        self.real_session_start = Conductor.session_start

        def session(conductor):
            self.events.append("session")
            return self.real_session_start(conductor)

        patches = {
            "module_file": patch("core.bootstrap.__file__", str(self.repo / "core" / "bootstrap.py")),
            "limits": patch("core.agents.load_limits", return_value=self.c.limits),
            "team": patch("core.bootstrap.real_team", return_value=self.team),
            "mailer": patch("core.bootstrap.gmail_mailer", return_value=self.c.mailer),
            "inbox_factory": patch("core.bootstrap.gmail_inbox", return_value=self.inbox),
            "gh": patch("core.bootstrap.gh_cli", return_value=self.gh),
            "init": patch.object(Conductor, "__init__", new=timed_init),
            "lock": patch("core.bootstrap.acquire_lock", side_effect=lambda state: Mock()),
            "guarded_smoke": patch("core.bootstrap._guarded_smoke",
                                   side_effect=lambda *a, **k: self.events.append("smoke") or []),
            "run": patch.object(Conductor, "run", side_effect=lambda *a, **k: self.events.append("run") or "idle"),
            "checks": patch("core.bootstrap.real_checks", side_effect=lambda: self.counting_checks()),
            "probes": patch("core.bootstrap.real_probes", side_effect=lambda limits: healthy_probes()),
            "session": patch.object(Conductor, "session_start", new=session),
        }
        self.cli = {}
        for name, patcher in patches.items():
            self.cli[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_main_run_refreshes_after_inbox_and_before_smoke(self):
        self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertEqual(self.events, ["inbox", "session", "smoke", "run"])
        self.assertEqual(self.calls["git"], 1)
        self.assertTrue((self.state / "capabilities.json").exists())

    def test_main_run_stop_skips_session_start(self):
        self.messages.append({"from": "benjaminanderson0802@gmail.com", "subject": "STOP", "body": ""})
        self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertNotIn("session", self.events)
        self.assertEqual(sum(self.calls.values()), 0)
        self.assertFalse((self.state / "capabilities.json").exists())

    def test_main_run_tamper_during_session_start_exits_zero(self):
        def tampering_probes(limits):
            def tamper(prompt, cwd):
                (self.state / "sneaky.json").write_text("x", encoding="utf-8")
                return "ok", 0
            return {"claude": FakeAgent(tamper, provider="claude")}
        self.cli["probes"].side_effect = tampering_probes
        self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertTrue((self.state / "KILL").exists())
        self.assertNotIn("smoke", self.events)
        self.assertNotIn("run", self.events)


class RealDefaults(ReadinessHarness):
    def test_conductor_without_checks_or_probes_uses_real_ones(self):
        sentinel_checks = {"git": lambda: (True, "sentinel")}
        sentinel_probes = {"claude": FakeAgent(lambda p, c: ("ok", 0), provider="claude")}
        limits = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9}
        self.make_conductor()
        with patch("core.bootstrap.real_checks", return_value=sentinel_checks) as rc, \
                patch("core.bootstrap.real_probes", return_value=sentinel_probes) as rp:
            c = Conductor(self.repo, self.work, self.state, self.team, limits, owner_email="ben@example.com",
                          mailer=lambda s, b: None, inbox=lambda: [], gh=self.gh, push=False)
        rc.assert_called_once_with()
        rp.assert_called_once_with(limits)
        self.assertEqual(c.checks, sentinel_checks)
        self.assertEqual(c.probes, sentinel_probes)

    def test_real_helpers_match_readiness_module(self):
        self.assertEqual(bootstrap.real_checks(), dict(rd.PLAIN_CHECKS))
        self.assertNotIn("claude", bootstrap.real_checks())
        with patch("core.readiness.probe_agents", return_value={"x": 1}) as pa:
            self.assertEqual(bootstrap.real_probes({"a": 1}), {"x": 1})
        pa.assert_called_once_with({"a": 1})


class PromptBlocks(ReadinessHarness):
    DEAD = '{"task": "T0", "notes": "vendoring libfoo fails", "alternative": "use stdlib"}'

    def broken_docker(self):
        return self.counting_checks({"docker": (False, "docker not installed or not on PATH")})

    def assert_map_block(self, prompt, role):
        self.assertIn("CAPABILITY MAP (plain-code readiness check; blocker claims that contradict it are rejected):",
                      prompt, role)
        self.assertRegex(prompt, r"- git: OK - git fine \(checked 2026-09-30T08:00:00\+00:00\)")
        self.assertIn("- docker: BROKEN (failing: docker not installed or not on PATH) - "
                      "docker not installed or not on PATH (checked", prompt)
        self.assertIn("- claude: OK", prompt)

    def test_every_role_in_stages_gets_map_and_dead_ends_only_for_builder_and_troubleshooter(self):
        (self.state / "dead_ends.jsonl").write_text(self.DEAD + "\n\n", encoding="utf-8")
        attempts = []

        def builder(prompt, cwd):
            attempts.append(1)
            if len(attempts) < 3:
                return '{"status":"done"}', 1  # no implementation: judge fails, twice -> troubleshooter
            return self.build_feature(prompt, cwd)

        c = self.init(agents={"test_writer": self.write_tests, "builder": builder}, checks=self.broken_docker())
        c.clock = lambda: self.now
        for _ in range(6):
            c.step()
        self.assertEqual(c._task("T1")["status"], "done")
        prompts = {role: getattr(c.team, role).prompts for role in ROLES}
        for role in ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper"):
            self.assertTrue(prompts[role], role)
            for p in prompts[role]:
                self.assert_map_block(p, role)
                if role in ("builder", "troubleshooter"):
                    self.assertEqual(p.count("KNOWN DEAD ENDS:"), 1, role)
                    self.assertEqual(p.count(self.DEAD), 1, role)
                else:
                    self.assertNotIn("KNOWN DEAD ENDS:", p, role)
                    self.assertNotIn(self.DEAD, p, role)
        b = prompts["builder"][0]
        self.assertTrue(b.startswith("You are the BUILDER"))
        self.assertLess(b.index("Answer with JSON"), b.index("CAPABILITY MAP"))

    def test_planner_and_plan_reviewer_get_the_map(self):
        planfile = "plan.md"
        task = {"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan it", "plan_file": planfile}

        def planner(p, cwd):
            (cwd / planfile).write_text("plan\n")
            return json.dumps({"tasks": [self.task(id="T2")]}), 1
        c = self.init(task, agents={"planner": planner}, checks=self.broken_docker())
        c.clock = lambda: self.now
        c.step()
        self.assertTrue(c.team.planner.prompts and c.team.reviewer.prompts)
        for p in c.team.planner.prompts + c.team.reviewer.prompts:
            self.assert_map_block(p, "planner/reviewer")
            self.assertNotIn("KNOWN DEAD ENDS:", p)

    def test_smoke_prompts_get_map_and_dead_ends_for_builder_and_troubleshooter(self):
        (self.state / "dead_ends.jsonl").write_text(self.DEAD + "\n", encoding="utf-8")
        c = self.conductor(checks=self.broken_docker())
        c._refresh_readiness()
        answers = {"test_writer": {"files": ["smoke.txt"]}, "builder": {"status": "done"},
                   "planner": {"tasks": []}, "reviewer": {"verdict": "pass", "reasons": []},
                   "drift_keeper": {"status": "ok"}, "troubleshooter": {"kind": "suggestion", "notes": "ok"}}
        members = {}
        for role, answer in answers.items():
            def script(prompt, cwd, role=role, answer=answer):
                if role in {"test_writer", "builder", "planner"}:
                    (cwd / "smoke.txt").write_text("ok", encoding="utf-8")
                return json.dumps(answer), 1
            members[role] = FakeAgent(script, provider="codex" if role in {"test_writer", "reviewer"} else "claude")
        c.team = Team(**members)
        self.assertEqual(bootstrap._guarded_smoke(c, self.work), [])
        for role in ROLES:
            prompts = getattr(c.team, role).prompts
            self.assertEqual(len(prompts), 1, role)
            p = prompts[0]
            self.assertTrue(p.startswith("SMOKE TEST for Forge"), role)
            self.assert_map_block(p, role)
            if role in ("builder", "troubleshooter"):
                self.assertEqual(p.count("KNOWN DEAD ENDS:"), 1, role)
                self.assertEqual(p.count(self.DEAD), 1, role)
            else:
                self.assertNotIn("KNOWN DEAD ENDS:", p, role)

    def test_empty_map_reads_no_evidence_yet(self):
        c = self.conductor()
        c._call("reviewer", "PROMPT", None, cwd=self.work)
        p = c.team.reviewer.prompts[0]
        self.assertTrue(p.startswith("PROMPT"))
        self.assertIn("CAPABILITY MAP", p)
        self.assertIn("(no readiness evidence yet)", p)

    def test_map_lines_sorted_and_malformed_shown_broken(self):
        c = self.conductor()
        rd.write_map(self.state / "capabilities.json", {
            "zeta": {"ok": True, "detail": "fine", "checked_at": T0.isoformat()},
            "alpha": {"ok": "true", "detail": "fake", "checked_at": T0.isoformat()},
            "stale": {"ok": True, "detail": "old", "checked_at": (T0 - timedelta(days=2)).isoformat()},
        })
        c._call("reviewer", "PROMPT", None, cwd=self.work)
        p = c.team.reviewer.prompts[0]
        self.assertLess(p.index("- alpha:"), p.index("- stale:"))
        self.assertLess(p.index("- stale:"), p.index("- zeta:"))
        self.assertIn("- alpha: BROKEN (malformed evidence)", p)
        self.assertIn("- stale: BROKEN (stale evidence", p)
        self.assertIn("- zeta: OK - fine", p)

    def test_map_block_capped_at_4000_chars(self):
        c = self.conductor()
        rd.write_map(self.state / "capabilities.json", {
            f"cap{i:03d}": {"ok": False, "detail": "x" * 250, "checked_at": T0.isoformat()} for i in range(100)})
        c._call("reviewer", "PROMPT", None, cwd=self.work)
        p = c.team.reviewer.prompts[0]
        block = p[p.index("CAPABILITY MAP"):].rstrip("\n")
        self.assertLessEqual(len(block), 4000)
        self.assertIn("- cap000: BROKEN", block)

    def test_dead_ends_last_50_lines_capped_at_20000(self):
        lines = [json.dumps({"task": "T", "notes": f"dead end number {i:03d} " + "y" * 100}) for i in range(60)]
        (self.state / "dead_ends.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        c = self.conductor()
        c._call("builder", "PROMPT", None, cwd=self.work)
        p = c.team.builder.prompts[0]
        self.assertIn("dead end number 059", p)
        self.assertIn("dead end number 010", p)
        self.assertNotIn("dead end number 009", p)
        block = p[p.index("KNOWN DEAD ENDS:"):]
        self.assertLessEqual(len(block), 20000 + len("KNOWN DEAD ENDS:\n") + 1)
        big = [json.dumps({"notes": f"big {i:02d} " + "z" * 1000}) for i in range(50)]
        (self.state / "dead_ends.jsonl").write_text("\n".join(big) + "\n", encoding="utf-8")
        c._call("troubleshooter", "PROMPT", None, cwd=self.work)
        p = c.team.troubleshooter.prompts[0]
        block = p[p.index("KNOWN DEAD ENDS:"):]
        self.assertLessEqual(len(block), 20000 + len("KNOWN DEAD ENDS:\n") + 1)
        self.assertIn("big 49", block)

    def test_no_dead_ends_block_without_entries(self):
        c = self.conductor()
        c._call("builder", "PROMPT", None, cwd=self.work)
        self.assertNotIn("KNOWN DEAD ENDS:", c.team.builder.prompts[0])


if __name__ == "__main__":
    unittest.main()
