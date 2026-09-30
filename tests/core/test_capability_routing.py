"""T1B2d: every failed readiness check goes to a Troubleshooter job or to Ben's queue with the exact fix.

Fakes only: core.agents.launch raises; diagnosis probes are injected; the clock is controlled."""
import io
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from core import bootstrap
from core import readiness as rd
from core.agents import FakeAgent
from core.bootstrap import Conductor, Team

try:
    from tests.core.test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes, py_test
except ImportError:  # pragma: no cover
    from test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes, py_test

T0 = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
GIT_MISSING = (False, "git not installed or not on PATH")
GH_MISSING = (False, "gh not installed or not on PATH")
GH_LOGGED_OUT = (False, "You are not logged into any GitHub hosts. To log in, run: gh auth login")
DOCKER_MISSING = (False, "docker not installed or not on PATH")
DOCKER_DOWN = (False, "error during connect: cannot connect to the Docker daemon")
GMAIL_NO_PW = (False, "no app password in Windows Credential Manager (forge-gmail)")
N8N_DOWN = (False, "URLError: <urlopen error [Errno 111] Connection refused>")


class RoutingHarness(Harness):
    def setUp(self):
        super().setUp()
        launch = patch("core.agents.launch", side_effect=AssertionError("a real process was launched"))
        launch.start()
        self.addCleanup(launch.stop)
        self.now = T0
        self.calls = Counter()
        self.results = {}
        self.fix_on_job = None
        self.job_answer = {"kind": "suggestion", "notes": "tried restarting"}

    def fake_checks(self, **overrides):
        self.results.update(overrides)

        def make(name):
            def check():
                self.calls[name] += 1
                return self.results.get(name, (True, f"{name} fine"))
            return check
        return {name: make(name) for name in HEALTHY_CHECKS}

    def troubleshooter(self, prompt, cwd):
        if "CAPABILITY FIX JOB" in prompt:
            if self.fix_on_job:
                self.results[self.fix_on_job] = (True, "fixed")
            return json.dumps(self.job_answer), 1
        return '{"kind":"suggestion","notes":"task advice"}', 1

    def routed(self, *tasks, agents=None, checks=None, probes=None, limits=None, **overrides):
        agents = dict(agents or {})
        agents.setdefault("troubleshooter", self.troubleshooter)
        agents.setdefault("test_writer", self.write_tests)
        agents.setdefault("builder", self.build_feature)
        c = self.init(*tasks, agents=agents, limits=limits,
                      checks=self.fake_checks(**overrides) if checks is None else checks, probes=probes)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        return c

    def items(self, status="open"):
        qs = json.loads((self.state / "questions.json").read_text()) if (self.state / "questions.json").exists() else {}
        return {k: v for k, v in qs.items() if v["kind"] == "capability" and (status is None or v["status"] == status)}

    def item_for(self, name, status="open"):
        found = [(k, v) for k, v in self.items(status).items() if v["capability"] == name]
        self.assertEqual(len(found), 1, f"expected exactly one {status} item for {name}: {self.items(None)}")
        return found[0]

    def reply(self, c, qid, text):
        q = self.items(None)[qid]
        self.messages[:] = [{"from": "ben@example.com", "subject": f"Re: [Forge Q-{qid} {q['code']}] x", "body": text}]
        result = c.step()
        self.messages[:] = []
        return result

    def team_prompts(self, c):
        return {r: list(getattr(c.team, r).prompts) for r in Team.__dataclass_fields__}

    def to_tests_ok(self, c):
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "tests_ok")


class GitFailing(RoutingHarness):
    def test_git_missing_is_emailed_at_once_and_nothing_launches(self):
        c = self.routed(git=GIT_MISSING)
        self.assertEqual(c.step(), "not_ready")
        self.assertFalse(any(self.team_prompts(c).values()))
        qid, item = self.item_for("git")
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(item["condition"], "missing")
        self.assertIs(item["hold"], False)
        self.assertIs(item["delivered"], True)
        self.assertEqual(item["subject"], "Forge needs git fixed (missing)")
        mails = [(s, b) for s, b in self.mails if "Forge needs git fixed" in s]
        self.assertEqual(len(mails), 1)
        body = mails[0][1]
        self.assertIn("git not installed or not on PATH", body)
        self.assertIn("Paste this into PowerShell:\nwinget install --id Git.Git -e", body)
        self.assertNotIn("PowerShell: winget", body)
        self.assertIn("If you don't answer, Forge keeps skipping work that needs git and re-checks it every cycle.",
                      body)
        self.assertIn("T1", body)
        # the next cycle neither duplicates the item nor emails again
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(len([s for s, b in self.mails if "Forge needs git fixed" in s]), 1)

    def test_git_and_github_failing_make_two_items(self):
        c = self.routed(git=GIT_MISSING, github=GH_MISSING)
        self.assertEqual(c.step(), "not_ready")
        self.item_for("git")
        self.item_for("github")
        self.assertEqual(len(self.items()), 2)
        self.assertFalse(any(self.team_prompts(c).values()))

    def test_win_r_fix_line(self):
        c = self.routed(docker=DOCKER_DOWN, git=GIT_MISSING)
        c.step()
        # docker is troubleshootable, but the troubleshooter needs git: so Ben gets it too
        _, item = self.item_for("docker")
        self.assertIn("Press Win + R and paste:\nC:\\Program Files\\Docker\\Docker\\Docker Desktop.exe", item["body"])


class Startup(RoutingHarness):
    def setUp(self):
        super().setUp()
        self.state = self.repo / "state" / "bootstrap"
        self.state.mkdir(parents=True)
        self.make_conductor()
        self.c._write("smoke_ok.json", {"at": (self.now - timedelta(hours=25)).isoformat()})
        self.events = []
        real_init = Conductor.__init__

        def timed_init(conductor, *args, **kwargs):
            kwargs["clock"] = lambda: self.now
            real_init(conductor, *args, **kwargs)

        real_smoke = bootstrap._guarded_smoke

        def smoke(*a, **k):
            self.events.append(("smoke", len(self.mails)))
            return real_smoke(*a, **k)

        patches = [
            patch("core.bootstrap.__file__", str(self.repo / "core" / "bootstrap.py")),
            patch("core.agents.load_limits", return_value=self.c.limits),
            patch("core.bootstrap.real_team", return_value=self.team),
            patch("core.bootstrap.gmail_mailer", return_value=self.c.mailer),
            patch("core.bootstrap.gmail_inbox", return_value=Mock(return_value=[])),
            patch("core.bootstrap.gh_cli", return_value=self.gh),
            patch.object(Conductor, "__init__", new=timed_init),
            patch("core.bootstrap.acquire_lock", side_effect=lambda state: Mock()),
            patch("core.bootstrap._guarded_smoke", side_effect=smoke),
            patch.object(Conductor, "run", side_effect=lambda *a, **k: self.events.append(("run", 0)) or "idle"),
            patch("core.bootstrap.real_checks", side_effect=lambda: self.fake_checks(git=GIT_MISSING)),
            patch("core.bootstrap.real_probes", side_effect=lambda limits: healthy_probes()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_main_run_emails_git_item_before_smoke(self):
        self.assertEqual(bootstrap.main(["run"]), 0)
        git_mails = [i for i, (s, b) in enumerate(self.mails) if "Forge needs git fixed" in s]
        self.assertEqual(len(git_mails), 1)
        self.assertEqual(self.events[0][0], "smoke")
        self.assertGreater(self.events[0][1], git_mails[0])  # the item went out before the smoke test ran
        self.assertFalse(any(getattr(self.team, r).prompts for r in Team.__dataclass_fields__))
        self.item_for("git")


class DockerDaemonDown(RoutingHarness):
    def setup_docker(self, **kw):
        c = self.routed(self.task(needs=["docker"]), **kw)
        self.to_tests_ok(c)
        self.results["docker"] = DOCKER_DOWN
        return c

    def test_job_fixes_docker_then_builder_runs(self):
        (self.state / "dead_ends.jsonl").write_text('{"capability": "docker", "notes": "old try"}\n', encoding="utf-8")
        c = self.setup_docker()
        self.fix_on_job = "docker"
        before = self.calls["docker"]
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.calls["docker"], before + 2)  # the cycle's check and the forced re-check
        prompts = c.team.troubleshooter.prompts
        self.assertEqual(len(prompts), 1)
        p = prompts[0]
        self.assertTrue(p.startswith("You are the TROUBLESHOOTER. CAPABILITY FIX JOB: docker"))
        self.assertIn("error during connect", p)
        self.assertIn("daemon_down", p)
        self.assertIn("Docker Desktop.exe", p)
        self.assertIn('"kind": "fix" | "dead_end" | "suggestion"', p)
        self.assertIn("CAPABILITY MAP", p)
        self.assertIn("KNOWN DEAD ENDS:", p)
        for rule in ("D-032", "paid", "secrets", "screen"):
            self.assertIn(rule, p)
        self.assertEqual(c.team.builder.prompts, [])
        self.assertIs(c._cap_map()["docker"]["ok"], True)
        self.assertEqual(self.items(), {})
        routing = json.loads((self.state / "cap_routing.json").read_text())
        self.assertNotIn("docker", routing)  # resolved: routing state reset
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertEqual(c._task("T1")["status"], "done")

    def test_job_runs_in_capfix_folder(self):
        seen = []

        def ts(prompt, cwd):
            seen.append(cwd)
            return self.troubleshooter(prompt, cwd)
        c = self.setup_docker(agents={"troubleshooter": ts})
        c.step()
        self.assertEqual(seen, [self.work / "_capfix"])
        self.assertNotEqual(seen[0], c.wt)

    def test_unfixed_docker_three_rounds_then_item_emailed(self):
        c = self.setup_docker()
        self.job_answer = {"kind": "dead_end", "notes": "Docker Desktop will not start", "alternative": "use WSL"}
        self.assertEqual(c.step(), "worked")
        ts = c.team.troubleshooter
        self.assertEqual(len(ts.prompts), 1)
        for minutes in (1, 30, 59):
            self.now = T0 + timedelta(minutes=minutes)
            self.assertEqual(c.step(), "not_ready")
        self.assertEqual(len(ts.prompts), 1)  # no second job inside cap_retry_h
        self.assertEqual(self.items(), {})
        self.assertFalse([s for s, b in self.mails if "Forge needs" in s])  # a job is still possible
        self.now = T0 + timedelta(minutes=61)
        self.assertEqual(c.step(), "worked")
        self.now = T0 + timedelta(minutes=122)
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(ts.prompts), 3)
        self.assertEqual(json.loads((self.state / "cap_routing.json").read_text())["docker"]["rounds"], 3)
        self.now = T0 + timedelta(minutes=300)
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(len(ts.prompts), 3)
        self.assertEqual(c.team.builder.prompts, [])
        _, item = self.item_for("docker")
        self.assertEqual(item["tasks"], ["T1"])
        # docker blocks the only task and no troubleshooting remains: Ben alone can unblock progress
        self.assertIs(item["hold"], False)
        self.assertIs(item["delivered"], True)
        self.assertEqual(len([s for s, b in self.mails if "Forge needs docker fixed (daemon_down)" in s]), 1)
        dead = [json.loads(x) for x in (self.state / "dead_ends.jsonl").read_text().splitlines()]
        self.assertEqual(len(dead), 3)
        self.assertEqual(dead[0], {"capability": "docker", "notes": "Docker Desktop will not start",
                                   "alternative": "use WSL"})

    def test_exhausted_rounds_item_held_while_other_work_is_possible(self):
        t2 = self.task(id="T2", title="Second", files_in_scope=["other.py"],
                       test_files=["tests/core/test_other.py"], test_cmd=py_test("tests/core/test_other.py"))
        c = self.routed(self.task(needs=["docker"]), t2, docker=DOCKER_DOWN)
        c._write("cap_routing.json", {"docker": {"rounds": 3, "last_job": T0.isoformat()}})
        self.assertEqual(c.step(), "worked")  # T1's test writer
        self.assertEqual(c.team.troubleshooter.prompts, [])
        _, item = self.item_for("docker")
        self.assertIs(item["hold"], True)
        self.assertIs(item["delivered"], False)
        self.assertFalse([s for s, b in self.mails if "Forge needs" in s])

    def test_unusable_job_answer_still_forces_recheck(self):
        c = self.setup_docker(agents={"troubleshooter": lambda p, cwd: ("not json", 1)})
        before = self.calls["docker"]
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.calls["docker"], before + 2)
        self.assertEqual(json.loads((self.state / "cap_routing.json").read_text())["docker"]["rounds"], 1)
        self.assertFalse((self.state / "dead_ends.jsonl").exists())

    def test_other_failures_routed_before_job_returns(self):
        c = self.setup_docker()
        self.results["gmail"] = GMAIL_NO_PW
        self.now += timedelta(seconds=rd.ok_ttl_s("gmail", c.limits) + 1)  # past gmail's ok cache
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)
        _, item = self.item_for("gmail")
        self.assertIs(item["hold"], True)

    def test_capped_troubleshooter_launches_no_job(self):
        c = self.setup_docker(limits={"claude_daily_token_cap": 10, "codex_daily_token_cap": 10**9})
        m = c._refresh_readiness()
        c.meter.add("claude", 10)
        self.assertIsNone(c._route_capabilities(m))
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertFalse((self.state / "cap_routing.json").exists()
                         and json.loads((self.state / "cap_routing.json").read_text()).get("docker", {}).get("rounds"))


class HeldItems(RoutingHarness):
    def test_gmail_without_password_is_held_and_work_proceeds(self):
        c = self.routed(gmail=GMAIL_NO_PW)
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.test_writer.prompts), 1)
        qid, item = self.item_for("gmail")
        self.assertIs(item["hold"], True)
        self.assertIs(item["delivered"], False)
        self.assertEqual(item["condition"], "no_password")
        self.assertEqual(self.mails, [])
        self.assertEqual(c.step(), "worked")  # builder
        self.assertEqual(c.step(), "worked")  # drift keeper
        self.assertEqual(self.mails, [])  # held items are never retried by _handle_inbox
        self.assertIs(self.items()[qid]["delivered"], False)

    def test_handle_inbox_skips_held(self):
        c = self.routed()
        qid = c._ask("capability", "held", "body", hold=True, capability="x")
        self.assertEqual(self.mails, [])
        c._handle_inbox()
        self.assertEqual(self.mails, [])
        self.assertIs(self.items()[qid]["delivered"], False)

    def test_status_lists_open_capability_items(self):
        self.state = self.repo / "state" / "bootstrap"
        self.state.mkdir(parents=True)
        c = self.make_conductor()
        c._ask("capability", "Forge needs docker fixed (missing)", "b", hold=True, capability="docker",
               condition="missing", tasks=["T1"])
        out = io.StringIO()
        with patch("core.bootstrap.__file__", str(self.repo / "core" / "bootstrap.py")), \
                patch("core.agents.load_limits", return_value=c.limits), patch("sys.stdout", out):
            self.assertEqual(bootstrap.main(["status"]), 0)
        self.assertIn("docker", out.getvalue())
        self.assertIn("missing", out.getvalue())


class EmptyMap(RoutingHarness):
    def test_no_checks_or_probes_files_items_for_git_and_both_ais(self):
        c = self.routed(checks={}, probes={})
        self.assertEqual(c.step(), "not_ready")
        self.assertFalse(any(self.team_prompts(c).values()))
        for name in ("git", "claude", "codex"):
            _, item = self.item_for(name)
            self.assertEqual(item["condition"], "no_evidence")
        self.assertEqual(len(self.items()), 3)


class DependenciesAndFixText(RoutingHarness):
    def test_n8n_left_to_failing_docker(self):
        c = self.routed(docker=DOCKER_MISSING, n8n=N8N_DOWN)
        c.step()
        self.item_for("docker")
        self.assertEqual([v for v in self.items().values() if v["capability"] == "n8n"], [])
        self.assertEqual(c.team.troubleshooter.prompts, [])

    def n8n_body(self, probe_out):
        probes = healthy_probes()
        probes["claude"] = FakeAgent(lambda p, cwd: ("nope", 0), provider="claude")  # no troubleshooter
        c = self.routed(n8n=N8N_DOWN, probes=probes)
        c.diagnose_probe = lambda args: (0, probe_out)
        c.step()
        return self.item_for("n8n")[1]

    def test_n8n_container_missing_vs_stopped(self):
        missing = self.n8n_body("")
        self.tearDown(); self.setUp()
        stopped = self.n8n_body("Exited (0) 2 hours ago")
        self.assertEqual(missing["condition"], "container_missing")
        self.assertEqual(stopped["condition"], "container_stopped")
        self.assertIn("docker run -d --name n8n", missing["body"])
        self.assertIn("docker start n8n", stopped["body"])
        self.assertNotEqual(missing["body"], stopped["body"])

    def test_gh_missing_vs_logged_out(self):
        c = self.routed(github=GH_MISSING)
        c.step()
        missing = self.item_for("github")[1]
        self.tearDown(); self.setUp()
        c = self.routed(github=GH_LOGGED_OUT)
        c.step()
        logged_out = self.item_for("github")[1]
        self.assertEqual(missing["condition"], "missing")
        self.assertEqual(logged_out["condition"], "logged_out")
        self.assertIn("winget install --id GitHub.cli -e", missing["body"])
        self.assertIn("gh auth login --hostname github.com", logged_out["body"])

    def test_condition_change_updates_item(self):
        c = self.routed(github=GH_MISSING)
        c.step()
        qid, _ = self.item_for("github")
        self.results["github"] = GH_LOGGED_OUT
        c.step()
        qid2, item = self.item_for("github")
        self.assertEqual(qid, qid2)
        self.assertEqual(item["condition"], "logged_out")
        self.assertEqual(item["subject"], "Forge needs github fixed (logged_out)")
        self.assertIn("gh auth login", item["body"])


class UnknownNeed(RoutingHarness):
    def setup_vpn(self):
        c = self.routed(self.task(needs=["vpn"]))
        self.to_tests_ok(c)
        self.assertEqual(c.step(), "not_ready")
        return c

    def test_vpn_gets_no_check_item_with_reply_instruction(self):
        c = self.setup_vpn()
        _, item = self.item_for("vpn")
        self.assertEqual(item["condition"], "no_check")
        self.assertEqual(item["tasks"], ["T1"])
        self.assertIn("not needed", item["body"])
        self.assertNotIn("Paste this into PowerShell", item["body"])
        self.assertNotIn("Win + R", item["body"])
        self.assertEqual(c.team.troubleshooter.prompts, [])

    def test_still_broken_reply_keeps_item_open_and_task_waiting(self):
        c = self.setup_vpn()
        qid, _ = self.item_for("vpn")
        self.assertEqual(self.reply(c, qid, "still broken"), "not_ready")
        item = self.items()[qid]
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["replies"], ["still broken"])
        self.assertEqual(c._task("T1")["needs"], ["vpn"])
        self.assertEqual(c._task("T1")["waiting_on"], ["vpn"])
        self.assertEqual(c.team.builder.prompts, [])

    def test_not_needed_reply_removes_need_and_closes(self):
        c = self.setup_vpn()
        qid, _ = self.item_for("vpn")
        self.reply(c, qid, "Not needed.\n\nthanks")
        item = self.items(None)[qid]
        self.assertEqual(item["status"], "answered")
        self.assertEqual(c._task("T1")["needs"], [])
        self.assertEqual(len(c.team.builder.prompts), 1)  # the same step built it


class RepliesCannotFakeReadiness(RoutingHarness):
    def test_reply_to_git_item_never_makes_git_ready(self):
        c = self.routed(git=GIT_MISSING)
        c.step()
        qid, _ = self.item_for("git")
        for text in ("fixed it", "not needed", "ok"):
            self.assertEqual(self.reply(c, qid, text), "not_ready")
            self.assertEqual(self.items(None)[qid]["status"], "open")
            self.assertIn("git", c.ready_for("test_writer"))
        self.assertFalse(any(self.team_prompts(c).values()))
        self.assertEqual(len(self.items()[qid]["replies"]), 3)

    def test_reply_forces_the_next_refresh(self):
        answers = ["please log in", "please log in again"]  # logged_out: not troubleshootable
        probes = healthy_probes()
        probes["codex"] = FakeAgent(lambda p, cwd: (answers.pop(0), 0), provider="codex")
        c = self.routed(probes=probes)
        c.step()
        qid, _ = self.item_for("codex")
        self.now += timedelta(seconds=60)  # well inside the failed probe's retry window
        c.step()
        self.assertEqual(len(probes["codex"].prompts), 1)
        self.reply(c, qid, "I logged in again")
        self.assertEqual(len(probes["codex"].prompts), 2)
        self.assertFalse((self.state / "readiness_force.json").exists()
                         and json.loads((self.state / "readiness_force.json").read_text()))
        self.assertEqual(self.items(None)[qid]["status"], "open")  # still failing: stays open


class Housekeeping(RoutingHarness):
    def test_item_resolved_when_check_passes(self):
        c = self.routed(git=GIT_MISSING)
        c.step()
        qid, _ = self.item_for("git")
        self.results["git"] = (True, "git version 2.45")
        self.assertEqual(c.step(), "worked")
        item = self.items(None)[qid]
        self.assertEqual(item["status"], "resolved")
        self.assertTrue(item["closed_at"])
        self.assertEqual(len(c.team.test_writer.prompts), 1)

    def test_route_returns_none_when_all_healthy(self):
        c = self.routed()
        self.assertIsNone(c._route_capabilities(c._refresh_readiness()))
        self.assertEqual(self.items(), {})


if __name__ == "__main__":
    unittest.main()
