"""T1B2e: a builder's blocker claim needs evidence, may not contradict the capability map, and is reviewed.

Fakes only (core.agents.launch raises); the clock is controlled."""
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import bootstrap
from core import readiness as rd
from core.agents import FakeAgent
from core.usage import Meter

try:
    from tests.core.test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes
except ImportError:  # pragma: no cover
    from test_bootstrap import HEALTHY_CHECKS, Harness, healthy_probes

T0 = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
DOCKER_MISSING = (False, "docker not installed or not on PATH")
EASY = "blocker rejected: no evidence (easy out)"


def claim(**kw):
    d = {"status": "blocked", "summary": "cannot reach docker",
         "tried": ["started Docker Desktop from the CLI", "used the docker context for WSL"],
         "error": "error during connect: open //./pipe/docker_engine: The system cannot find the file",
         "capability": "docker", "meanwhile": "write the pure-Python parts of the feature"}
    d.update(kw)
    return {k: v for k, v in d.items() if v is not ...}


class BlockerHarness(Harness):
    def setUp(self):
        super().setUp()
        launch = patch("core.agents.launch", side_effect=AssertionError("a real process was launched"))
        launch.start()
        self.addCleanup(launch.stop)
        self.now = T0
        self.results = {}
        self.calls = Counter()

    def fake_checks(self, **overrides):
        self.results.update(overrides)

        def make(name):
            def check():
                self.calls[name] += 1
                return self.results.get(name, (True, f"{name} fine"))
            return check
        return {name: make(name) for name in HEALTHY_CHECKS}

    def blocked_build(self, answer, reviewer=None, limits=None, probes=None, builder_extra=None, **checks):
        def builder(prompt, cwd):
            (cwd / "feat.py").write_text("# partial attempt\nVALUE = None\n", encoding="utf-8")
            if builder_extra:
                builder_extra()
            return json.dumps(answer), 1
        agents = {"test_writer": self.write_tests, "builder": builder}
        if reviewer is not None:
            agents["reviewer"] = reviewer
        c = self.init(agents=agents, limits=limits, checks=self.fake_checks(**checks), probes=probes)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        return c

    def easy_outs(self):
        p = self.state / "easy_outs.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def events(self, c, action=None):
        return [e for e in c._ledger().events() if action is None or e["action"] == action]

    def assert_easy_out(self, c, reason, capability):
        outs = self.easy_outs()
        self.assertEqual(len(outs), 1)
        o = outs[0]
        self.assertEqual({k: o[k] for k in ("task", "agent", "kind", "reason")},
                         {"task": "T1", "agent": "builder", "kind": "easy_out", "reason": reason})
        self.assertEqual(o["capability"], capability)
        self.assertIn("summary", o)
        self.assertTrue(datetime.fromisoformat(o["at"]))
        reports = self.events(c, "run_report")
        self.assertEqual(len(reports), 1)
        payload = reports[0]["payload"]
        self.assertEqual(payload["claim"], "blocked")
        self.assertIsNone(payload["commit"])
        self.assertEqual((payload["changed"], payload["violations"], payload["out_of_scope"]), ([], [], []))
        self.assertEqual(payload["easy_out"], {"reason": reason, "capability": capability})
        self.assertIsInstance(payload["run_id"], str)
        actions = [e["action"] for e in self.events(c)]
        self.assertLess(actions.index("run_report"), actions.index("release"))  # reported before release
        t = c._task("T1")
        self.assertIn(reason, t["notes"])
        self.assertEqual(t["fail_signatures"][-1], "easy-out")
        self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")
        self.assertEqual(bootstrap._git(c.wt, "rev-parse", "HEAD"), t["tests_commit"])
        self.assertFalse((c.wt / "feat.py").exists())


class MissingEvidence(BlockerHarness):
    CASES = {
        "no tried": claim(tried=...),
        "tried not a list": claim(tried="restarted it twice"),
        "one route": claim(tried=["restarted docker"]),
        "two identical routes": claim(tried=["restarted docker", "restarted docker"]),
        "identical after trimming": claim(tried=["restarted docker", "  restarted docker "]),
        "one blank route": claim(tried=["restarted docker", "   "]),
        "no error": claim(error=...),
        "blank error": claim(error="  "),
        "no capability": claim(capability=...),
        "invalid capability": claim(capability="Docker Desktop!"),
        "no meanwhile": claim(meanwhile=...),
        "blank meanwhile": claim(meanwhile=""),
    }

    def test_each_missing_piece_is_an_easy_out(self):
        for label, answer in self.CASES.items():
            with self.subTest(case=label):
                self.tearDown(); self.setUp()
                c = self.blocked_build(answer, docker=DOCKER_MISSING)
                self.assertEqual(c.step(), "worked")
                self.assertEqual(c.team.reviewer.prompts, [])
                cap = answer.get("capability")
                self.assert_easy_out(c, EASY, cap if isinstance(cap, str) else None)


class ContradictsMap(BlockerHarness):
    def test_claim_against_ok_evidence_is_rejected_without_review(self):
        c = self.blocked_build(claim())  # docker healthy
        c.step()
        self.assertEqual(c.team.reviewer.prompts, [])
        entry = c._cap_map()["docker"]
        reason = (f"blocker rejected: contradicts capability map (docker is ok: docker fine, "
                  f"checked {entry['checked_at']})")
        self.assert_easy_out(c, reason, "docker")
        self.assertEqual(c._task("T1")["needs"], [])


class Reviewed(BlockerHarness):
    def test_reviewer_sees_claim_and_map_entry_and_can_reject(self):
        def reviewer(prompt, cwd):
            return '{"verdict":"fail","reasons":["only restarted things, never read the logs"]}', 1
        c = self.blocked_build(claim(), reviewer=reviewer, docker=DOCKER_MISSING)
        c.step()
        self.assertEqual(len(c.team.reviewer.prompts), 1)
        p = c.team.reviewer.prompts[0]
        for piece in ("started Docker Desktop from the CLI", "used the docker context for WSL",
                      "error during connect", "write the pure-Python parts", "cannot reach docker",
                      "docker not installed or not on PATH", "feat.py", "partial attempt", "TASK T1"):
            self.assertIn(piece, p)
        self.assertIn("real attempts", p)
        self.assert_easy_out(c, "blocker rejected by reviewer: only restarted things, never read the logs", "docker")
        self.assertEqual(c._task("T1")["needs"], [])

    def test_unusable_review_rejects(self):
        c = self.blocked_build(claim(), reviewer=lambda p, cwd: ("not json", 1), docker=DOCKER_MISSING)
        c.step()
        outs = self.easy_outs()
        self.assertEqual(len(outs), 1)
        self.assertTrue(outs[0]["reason"].startswith("blocker rejected by reviewer: "))

    def test_accepted_claim_adds_need_notes_hands_off_and_gates_builder(self):
        c = self.blocked_build(claim(), docker=DOCKER_MISSING)
        self.assertEqual(c.step(), "worked")
        t = c._task("T1")
        self.assertEqual(t["needs"], ["docker"])
        self.assertIn("blocker accepted: needs docker; meanwhile: write the pure-Python parts of the feature",
                      t["notes"])
        self.assertTrue(any(n.startswith("blocker: cannot reach docker (tried: started Docker Desktop from the CLI; "
                                         "used the docker context for WSL; error: error during connect")
                            for n in t["notes"]))
        self.assertEqual(self.easy_outs(), [])
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")
        # the first accepted blocker is handed to the Troubleshooter at once, not stranded behind the gate
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)
        self.assertIn("blocker: cannot reach docker", c.team.troubleshooter.prompts[0])
        self.assertEqual(t["troubleshoots"], 1)
        for _ in range(3):
            self.now += timedelta(minutes=5)
            self.assertEqual(c.step(), "not_ready")
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertEqual(c._task("T1")["waiting_on"], ["docker"])
        self.results["docker"] = (True, "27.1")
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.builder.prompts), 2)

    def test_handoff_waits_when_troubleshooter_unready(self):
        probes = healthy_probes()
        c = self.blocked_build(claim(), docker=DOCKER_MISSING, probes=probes,
                               limits={"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9,
                                       "ai_check_ttl_h": 0.1},
                               builder_extra=lambda: setattr(self, "now", self.now + timedelta(hours=1)))
        probes["claude"].script = lambda p, cwd: ("please log in", 0)  # claude goes bad during the attempt
        self.assertEqual(c.step(), "not_ready")
        t = c._task("T1")
        self.assertEqual(t["needs"], ["docker"])
        self.assertTrue(t["troubleshoot_pending"])
        self.assertEqual(c.team.troubleshooter.prompts, [])
        probes["claude"].script = lambda p, cwd: ("ok", 0)
        self.now += timedelta(hours=1)
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)  # runs although docker is still broken
        self.assertEqual(len(c.team.builder.prompts), 1)

    def test_unknown_capability_is_persisted_and_gates(self):
        c = self.blocked_build(claim(capability="vpn", summary="needs the office VPN"))
        c.step()
        self.assertIn("no automatic check exists", c.team.reviewer.prompts[0])
        self.assertEqual(c._task("T1")["needs"], ["vpn"])
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertEqual(c._task("T1")["waiting_on"], ["vpn"])

    def test_capability_already_needed_is_not_duplicated(self):
        c = self.blocked_build(claim(), docker=DOCKER_MISSING)
        c._update("T1", needs=["docker"])
        self.results["docker"] = (True, "ok")  # let the builder start, then docker breaks during the attempt

        def breaks():
            self.results["docker"] = DOCKER_MISSING
            self.now += timedelta(seconds=rd.max_age_for("docker", c.limits) + 1)
        c.team.builder.script = lambda p, cwd: (breaks(), (json.dumps(claim()), 1))[1]
        c.step()
        self.assertEqual(c._task("T1")["needs"], ["docker"])


class CappedReview(BlockerHarness):
    def test_capped_reviewer_undoes_attempt_without_failure(self):
        cap = 10
        c = self.blocked_build(claim(), docker=DOCKER_MISSING,
                               limits={"claude_daily_token_cap": 10**9, "codex_daily_token_cap": cap},
                               builder_extra=lambda: c.meter.add("codex", cap + 1))
        c.meter = Meter(self.state.parent / "shared-usage", c.clock)  # usage outside guarded state
        before = c._task("T1")
        self.assertEqual(c.step(), "capped")
        self.assertEqual(c.team.reviewer.prompts, [])
        t = c._task("T1")
        for field in ("status", "fails_since", "notes", "fail_signatures", "needs"):
            self.assertEqual(t[field], before[field], field)
        self.assertEqual(self.easy_outs(), [])
        self.assertEqual(self.events(c, "run_report"), [])
        self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")
        self.assertEqual(bootstrap._git(c.wt, "rev-parse", "HEAD"), t["tests_commit"])
        self.assertEqual(bootstrap._git(c.wt, "status", "--porcelain"), "")

    def test_not_ready_reviewer_undoes_attempt_without_failure(self):
        probes = healthy_probes()
        limits = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9, "ai_check_ttl_h": 0.1}

        def codex_goes_stale():
            self.now += timedelta(seconds=rd.max_age_for("codex", limits) + 60)
            probes["codex"].script = lambda p, cwd: ("please log in", 0)
        c = self.blocked_build(claim(), docker=DOCKER_MISSING, probes=probes, limits=limits,
                               builder_extra=codex_goes_stale)
        before = c._task("T1")
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.reviewer.prompts, [])
        t = c._task("T1")
        for field in ("status", "fails_since", "notes", "fail_signatures", "needs"):
            self.assertEqual(t[field], before[field], field)
        self.assertEqual(self.easy_outs(), [])
        self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")
        self.assertEqual(bootstrap._git(c.wt, "rev-parse", "HEAD"), t["tests_commit"])


class PromptAndSchema(BlockerHarness):
    def test_builder_prompt_names_all_four_fields(self):
        c = self.blocked_build(claim(), docker=DOCKER_MISSING)
        c.step()
        p = c.team.builder.prompts[0]
        closing = p[p.index("Answer with JSON"):p.index("CAPABILITY MAP")]
        for field in ('"tried"', '"error"', '"capability"', '"meanwhile"'):
            self.assertIn(field, closing)
        self.assertIn("at least 2 different routes", closing)
        self.assertIn("easy way out", closing)

    def test_schema_has_optional_capability_and_meanwhile(self):
        props = bootstrap.S_BUILD["properties"]
        self.assertEqual(props["capability"], {"type": "string"})
        self.assertEqual(props["meanwhile"], {"type": "string"})
        self.assertEqual(bootstrap.S_BUILD["required"], ["status"])


if __name__ == "__main__":
    unittest.main()
