"""T1B2c: no agent starts while a capability it needs lacks usable evidence; tasks declare needs.

Fakes only. Broken capabilities use details readiness.diagnose marks not troubleshootable, or a need with no
check (vpn), so these tests stay valid once failures are routed (T1B2d). No assertions on emails."""
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import bootstrap
from core import readiness as rd
from core.agents import FakeAgent
from core.bootstrap import Conductor, NotReady, Team, validate_task

try:
    from tests.core.test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes, py_test
except ImportError:  # pragma: no cover
    from test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes, py_test

T0 = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
DOCKER_MISSING = (False, "docker not installed or not on PATH")
GIT_MISSING = (False, "git not installed or not on PATH")
GH_MISSING = (False, "gh not installed or not on PATH")


class GateHarness(Harness):
    def setUp(self):
        super().setUp()
        launch = patch("core.agents.launch", side_effect=AssertionError("a real process was launched"))
        launch.start()
        self.addCleanup(launch.stop)
        self.now = T0
        self.calls = Counter()
        self.results = {}

    def fake_checks(self, **overrides):
        self.results.update(overrides)

        def make(name):
            def check():
                self.calls[name] += 1
                return self.results.get(name, (True, f"{name} fine"))
            return check
        return {name: make(name) for name in HEALTHY_CHECKS}

    def gated(self, *tasks, agents=None, limits=None, checks=None, probes=None):
        c = self.init(*tasks, agents=agents, limits=limits,
                      checks=self.fake_checks() if checks is None else checks, probes=probes)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        return c

    def writer_for_any(self, prompt, cwd):
        name = "tests/core/test_other.py" if "tests/core/test_other.py" in prompt else "tests/core/test_feat.py"
        mod = "other" if "other" in name else "feat"
        f = cwd / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"import unittest\nimport {mod}\nclass T(unittest.TestCase):\n"
                     f" def test_value(self): self.assertEqual({mod}.VALUE, 42)\n", encoding="utf-8")
        return json.dumps({"files": [name]}), 1

    def builder_for_any(self, prompt, cwd):
        mod = "other" if "other.py" in prompt else "feat"
        (cwd / f"{mod}.py").write_text("VALUE = 42\n", encoding="utf-8")
        return '{"status":"done"}', 1

    def contract_status(self, c, cid="T1"):
        return c._ledger().contracts().get(cid, {}).get("status")


class DeclaredNeeds(GateHarness):
    def test_validate_task_needs(self):
        self.assertIsNone(validate_task(self.task()))
        self.assertIsNone(validate_task(self.task(needs=[])))
        self.assertIsNone(validate_task(self.task(needs=["docker", "vpn", "n8n"])))
        for bad in (["Bad Name!"], "docker", [1], ["a"] * 11, [""], ["x" * 41], None):
            with self.subTest(needs=bad):
                self.assertEqual(validate_task(self.task(needs=bad)), "bad needs")

    def test_init_queue_rejects_bad_needs(self):
        c = self.make_conductor(checks=self.fake_checks())
        with self.assertRaises(ValueError):
            c.init_queue(self.layer, [self.task(needs=["Bad Name!"])])

    def test_new_task_defaults_needs(self):
        c = self.gated(self.task(), self.task(id="T2", needs=["docker"]))
        self.assertEqual(c._task("T1")["needs"], [])
        self.assertEqual(c._task("T2")["needs"], ["docker"])

    def test_plan_schema_allows_needs(self):
        item = bootstrap.S_PLAN["properties"]["tasks"]["items"]
        self.assertEqual(item["properties"]["needs"], {"type": "array", "items": {"type": "string"}})
        self.assertNotIn("needs", item["required"])

    def plan(self, child_needs):
        task = {"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan it", "plan_file": "plan.md"}

        def planner(p, cwd):
            (cwd / "plan.md").write_text("plan\n")
            child = self.task(id="T2", section="s" * 600)  # R41: sections >= 600 chars
            del child["kind"]
            if child_needs is not None:
                child["needs"] = child_needs
            return json.dumps({"tasks": [child]}), 1
        c = self.gated(task, agents={"planner": planner})
        c.step()
        return c

    def test_plan_needs_are_queued(self):
        c = self.plan(["docker"])
        self.assertEqual(c._task("P1")["status"], "done")
        self.assertEqual(c._task("T2")["needs"], ["docker"])

    def test_plan_without_needs_defaults_empty(self):
        c = self.plan(None)
        self.assertEqual(c._task("T2")["needs"], [])

    def test_plan_bad_needs_rejected(self):
        c = self.plan(["Bad Name!"])
        t = c._task("P1")
        self.assertNotEqual(t["status"], "done")
        self.assertIn("plan rejected: bad needs", t["notes"])
        self.assertEqual(len(c._queue()["tasks"]), 1)

    def test_planner_prompt_mentions_needs(self):
        c = self.plan([])
        p = c.team.planner.prompts[0]
        self.assertIn('Each task may also list "needs": capability names from the capability map', p)
        self.assertIn("beyond git and its own AI", p)


class Requirements(GateHarness):
    def test_requirements_per_role(self):
        c = self.gated(self.task(needs=["docker"]))
        t = c._task("T1")
        self.assertEqual(c._requirements("builder", t), {"git", "claude", "docker"})
        self.assertEqual(c._requirements("test_writer", t), {"git", "codex"})
        self.assertEqual(c._requirements("reviewer"), {"git", "codex"})
        self.assertEqual(c._requirements("builder"), {"git", "claude"})

    def test_ready_for_blocks_malformed_evidence(self):
        c = self.gated(self.task(needs=["docker"]))
        t = c._task("T1")
        good = {"ok": True, "detail": "ok", "checked_at": T0.isoformat()}
        base = {"git": good, "claude": good}
        cases = {
            "missing": None,
            "ok string": {"ok": "true", "detail": "", "checked_at": T0.isoformat()},
            "ok int": {"ok": 1, "detail": "", "checked_at": T0.isoformat()},
            "no checked_at": {"ok": True, "detail": ""},
            "invalid checked_at": {"ok": True, "detail": "", "checked_at": "yesterday"},
            "naive checked_at": {"ok": True, "detail": "", "checked_at": "2026-09-30T08:00:00"},
            "future": {"ok": True, "detail": "", "checked_at": (T0 + timedelta(hours=1)).isoformat()},
            "stale": {"ok": True, "detail": "", "checked_at": (T0 - timedelta(hours=1)).isoformat()},
            "failing": {"ok": False, "detail": "down", "checked_at": T0.isoformat()},
            "not a dict": "ok",
        }
        for label, entry in cases.items():
            with self.subTest(case=label):
                m = dict(base)
                if entry is not None:
                    m["docker"] = entry
                self.assertEqual(list(c.ready_for("builder", t, cap_map=m)), ["docker"])
                rd.write_map(self.state / "capabilities.json", m)
                self.assertEqual(list(c.ready_for("builder", t)), ["docker"])
        m = dict(base, docker=good)
        self.assertEqual(c.ready_for("builder", t, cap_map=m), {})

    def test_ai_evidence_uses_its_own_max_age(self):
        c = self.gated()
        entry = {"ok": True, "detail": "", "checked_at": (T0 - timedelta(hours=2)).isoformat()}
        good = {"ok": True, "detail": "", "checked_at": T0.isoformat()}
        self.assertEqual(c.ready_for("builder", None, cap_map={"git": good, "claude": entry}), {})


class BuilderGate(GateHarness):
    def test_docker_needed_and_failing_blocks_builder_only(self):
        c = self.gated(self.task(needs=["docker"]), agents={"test_writer": self.write_tests,
                                                            "builder": self.build_feature},
                       checks=self.fake_checks(docker=DOCKER_MISSING))
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.test_writer.prompts), 1)
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [])
        t = c._task("T1")
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(t["waiting_on"], ["docker"])
        self.assertEqual(t["fails_since"], 0)
        self.assertEqual(t["fail_signatures"], [])
        self.assertIsNone(self.contract_status(c))  # no ledger claim, not even a contract
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [])
        self.results["docker"] = (True, "27.0")
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertEqual(c._task("T1")["status"], "done")

    def test_need_without_check_blocks_indefinitely(self):
        c = self.gated(self.task(needs=["vpn"]), agents={"test_writer": self.write_tests,
                                                         "builder": self.build_feature})
        c.step()
        for hours in (0, 1, 30):
            self.now = T0 + timedelta(hours=hours)
            self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [])
        self.assertEqual(c._task("T1")["waiting_on"], ["vpn"])
        self.assertNotIn("vpn", c._cap_map())

    def test_other_work_continues_while_one_task_waits(self):
        t2 = self.task(id="T2", title="Second", files_in_scope=["other.py"],
                       test_files=["tests/core/test_other.py"], test_cmd=py_test("tests/core/test_other.py"))
        c = self.gated(self.task(needs=["docker"]), t2,
                       agents={"test_writer": self.writer_for_any, "builder": self.builder_for_any},
                       checks=self.fake_checks(docker=DOCKER_MISSING))
        for _ in range(6):
            c.step()
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        self.assertEqual(c._task("T1")["waiting_on"], ["docker"])
        self.assertEqual(c._task("T2")["status"], "done")
        self.assertTrue(all("other.py" in p for p in c.team.builder.prompts))
        self.assertEqual(c.step(), "not_ready")  # only the waiting task is left

    def test_pending_troubleshooting_runs_even_when_task_need_is_broken(self):
        c = self.gated(self.task(needs=["docker"]), agents={"test_writer": self.write_tests,
                                                            "builder": self.build_feature},
                       checks=self.fake_checks(docker=DOCKER_MISSING))
        c.step()
        c._update("T1", troubleshoot_pending={"reason": "judge failed", "output": "boom"})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)
        self.assertEqual(c.team.builder.prompts, [])
        self.assertIsNone(c._task("T1").get("troubleshoot_pending"))
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [])

    def test_pending_troubleshooting_waits_for_its_own_requirements(self):
        probes = healthy_probes()
        probes["claude"] = FakeAgent(lambda p, cwd: ("logged out", 0), provider="claude")
        c = self.gated(self.task(), agents={"test_writer": self.write_tests}, probes=probes)
        c.step()
        c._update("T1", troubleshoot_pending={"reason": "judge failed", "output": "boom"})
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertEqual(c._task("T1")["waiting_on"], ["claude"])
        self.assertTrue(c._task("T1")["troubleshoot_pending"])

    def test_git_failing_launches_nothing(self):
        c = self.gated(self.task(), agents={"test_writer": self.write_tests},
                       checks=self.fake_checks(git=GIT_MISSING))
        for _ in range(3):
            self.assertEqual(c.step(), "not_ready")
        self.assertFalse(any(getattr(c.team, r).prompts for r in Team.__dataclass_fields__))
        self.assertEqual(c._task("T1")["waiting_on"], ["git"])

    def test_git_failing_blocks_plan_and_drift(self):
        task = {"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan it", "plan_file": "plan.md"}
        c = self.gated(task, checks=self.fake_checks(git=GIT_MISSING))
        q = c._queue()
        q["drift_due"] = True
        c._save_queue(q)
        self.assertEqual(c.step(), "not_ready")
        self.assertFalse(any(getattr(c.team, r).prompts for r in Team.__dataclass_fields__))
        self.assertTrue(c._queue()["drift_due"])

    def test_drift_skipped_when_keeper_unready_but_tasks_continue(self):
        probes = healthy_probes()
        probes["claude"] = FakeAgent(lambda p, cwd: ("logged out", 0), provider="claude")
        c = self.gated(self.task(), agents={"test_writer": self.write_tests}, probes=probes)
        q = c._queue()
        q["drift_due"] = True
        c._save_queue(q)
        self.assertEqual(c.step(), "worked")  # the test writer (codex) still runs
        self.assertEqual(c.team.drift_keeper.prompts, [])
        self.assertEqual(len(c.team.test_writer.prompts), 1)
        self.assertTrue(c._queue()["drift_due"])
        self.assertEqual(c.step(), "not_ready")  # the builder needs claude too; gate stays closed
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in self.gh_calls))


class StaleEvidenceMidStage(GateHarness):
    LIMITS = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9,
              "readiness_max_age_s": 900, "ai_check_ttl_h": 0.1}

    def setup_flow(self, codex_answers):
        answers = list(codex_answers)

        def codex_probe(prompt, cwd):
            return (answers.pop(0) if len(answers) > 1 else answers[0]), 0

        probes = healthy_probes()
        probes["codex"] = FakeAgent(codex_probe, provider="codex")
        skip = rd.max_age_for("codex", self.LIMITS) + 60

        def slow_builder(prompt, cwd):
            self.now += timedelta(seconds=skip)
            return self.build_feature(prompt, cwd)

        c = self.gated(self.task(), agents={"test_writer": self.write_tests, "builder": slow_builder},
                       limits=dict(self.LIMITS), probes=probes)
        self.assertEqual(c.step(), "worked")
        return c, probes

    def test_reviewer_gate_refreshes_stale_evidence_and_continues(self):
        c, probes = self.setup_flow(["ok"])
        git_before = self.calls["git"]
        self.assertEqual(c.step(), "worked")
        self.assertGreater(self.calls["git"], git_before)
        self.assertEqual(len(probes["codex"].prompts), 2)
        self.assertEqual(len(c.team.reviewer.prompts), 1)
        self.assertEqual(c._task("T1")["status"], "done")

    def test_reviewer_not_launched_when_refresh_finds_codex_failing(self):
        c, probes = self.setup_flow(["ok", "codex login required", "ok"])
        before = c._task("T1")
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.reviewer.prompts, [])
        self.assertEqual(self.contract_status(c), "open")
        t = c._task("T1")
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(t["fails_since"], before["fails_since"])
        self.assertEqual(t["fail_signatures"], before["fail_signatures"])
        self.assertEqual(t["notes"], before["notes"])
        self.assertEqual(bootstrap._git(c.wt, "rev-parse", "HEAD"), t["tests_commit"])
        # a later healthy step (after the failed probe's retry window) finishes the task
        self.now += timedelta(seconds=rd.fail_retry_s("codex", c.limits) + 1)
        c.team.builder = FakeAgent(self.build_feature, provider="claude")
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "done")
        self.assertEqual(self.contract_status(c), "done")

    def test_inline_refresh_rechecks_cap_before_launch(self):
        c, probes = self.setup_flow(["ok"])
        probes["codex"].script = lambda p, cwd: ("ok", 10**9)  # the refresh probe spends the budget
        self.assertEqual(c.step(), "capped")
        self.assertEqual(c.team.reviewer.prompts, [])
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        self.assertEqual(self.contract_status(c), "open")


class CallGate(GateHarness):
    def test_call_raises_not_ready_without_run_record(self):
        c = self.gated(checks=self.fake_checks(git=GIT_MISSING))
        c._refresh_readiness()
        runs_before = set((self.state / "runs").rglob("*")) if (self.state / "runs").exists() else set()
        with self.assertRaises(NotReady) as cm:
            c._call("reviewer", "PROMPT", None, cwd=self.work)
        self.assertIn("git", cm.exception.names)
        self.assertTrue(cm.exception.names["git"].startswith("failing"))
        self.assertEqual(c.team.reviewer.prompts, [])
        after = set((self.state / "runs").rglob("*")) if (self.state / "runs").exists() else set()
        self.assertEqual(after, runs_before)

    def test_call_needs_argument(self):
        c = self.gated()
        c._refresh_readiness()
        with self.assertRaises(NotReady) as cm:
            c._call("builder", "PROMPT", None, cwd=self.work, needs=["vpn"])
        self.assertEqual(set(cm.exception.names), {"vpn"})
        c._call("builder", "PROMPT", None, cwd=self.work, needs=["docker"])
        self.assertEqual(len(c.team.builder.prompts), 1)

    def test_call_refreshes_missing_evidence_once(self):
        c = self.gated()
        self.assertFalse((self.state / "capabilities.json").exists())
        c._call("reviewer", "PROMPT", None, cwd=self.work)
        self.assertEqual(self.calls["git"], 1)
        self.assertEqual(self.calls["docker"], 0)  # only the unready names are refreshed
        self.assertEqual(len(c.team.reviewer.prompts), 1)


class GateNeedsGithub(GateHarness):
    def test_gate_waits_for_github(self):
        c = self.gated(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.step(); c.step(); c.step()
        self.assertEqual(c._task("T1")["status"], "done")
        c.checks["github"] = lambda: GH_MISSING
        self.assertEqual(c.step(), "not_ready")
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in self.gh_calls))
        c.checks["github"] = lambda: (True, "logged in")
        self.assertEqual(c.step(), "gate")
        self.assertTrue(any(a[:2] == ["pr", "create"] for a in self.gh_calls))


class RunLoop(GateHarness):
    def test_run_sleeps_on_not_ready(self):
        c = self.gated(checks=self.fake_checks(git=GIT_MISSING))
        sleeps = []
        c.run(max_steps=2, idle_sleep_s=17, sleep=sleeps.append)
        self.assertEqual(sleeps, [17, 17])


class SmokeGate(GateHarness):
    def test_guarded_smoke_with_git_failing_launches_nothing(self):
        c = self.gated(checks=self.fake_checks(git=GIT_MISSING))
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("not ready: git: failing"), problems)
        self.assertFalse(any(getattr(c.team, r).prompts for r in Team.__dataclass_fields__))
        self.assertFalse((self.state / "smoke_ok.json").exists())
        self.assertTrue((self.state / "smoke_fail.json").exists())


if __name__ == "__main__":
    unittest.main()
