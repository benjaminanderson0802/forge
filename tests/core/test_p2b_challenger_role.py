"""T2Bb: challenger contracts, using local git repositories and fake agents only.

P2A interfaces are absent from this checkout and its phase-2a refs. Extra roles
are deliberately not pinned to a singleton: an auditor can share the mechanism.
"""
import json
import unittest
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from core import agents, bootstrap, roles
from tests.core.test_bootstrap import Harness, git
from tests.core.test_blocker_claims import BlockerHarness


LAYER_ONE = ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper", "planner")
SMOKE = {"verdict": "stands", "route": "smoke", "tried": ["a", "b"],
         "error": "smoke", "proof": "patch"}
FIRST = "You are the CHALLENGER."


class ChallengerSchema(unittest.TestCase):
    def test_shape_and_strict_conversion(self):
        schema = bootstrap.S_CHALLENGE
        self.assertEqual(schema["required"], ["verdict"])
        self.assertEqual(set(schema["properties"]), set(SMOKE))
        answers = [SMOKE, {"verdict": "stands"}, {"verdict": "overturned"}]
        for proof in ("patch", "capability"):
            answers.append(dict(SMOKE, verdict="overturned", route="use datetime.strptime", proof=proof))
        for answer in answers:
            with self.subTest(answer=answer):
                self.assertTrue(agents.schema_ok(answer, schema))
        for answer in ({}, dict(SMOKE, verdict="maybe"), dict(SMOKE, proof="claim"),
                       dict(SMOKE, tried="a, b"), dict(SMOKE, tried=[1]),
                       dict(SMOKE, route=1), dict(SMOKE, error=[])):
            with self.subTest(invalid=answer):
                self.assertFalse(agents.schema_ok(answer, schema))
        strict = agents.strict_schema(schema)
        self.assertTrue(agents.schema_ok(SMOKE, strict))
        self.assertFalse(strict["additionalProperties"])
        self.assertEqual(set(strict["required"]), set(SMOKE))
        self.assertEqual(schema["required"], ["verdict"])


class ChallengerRoles(Harness):
    def test_extra_roles_preserve_the_six_role_contract(self):
        self.assertEqual(roles.ROLE_NAMES, LAYER_ONE)
        self.assertEqual(set(roles.DEFAULTS), set(LAYER_ONE))
        self.assertIn("challenger", roles.EXTRA_ROLE_NAMES)
        for name in roles.EXTRA_ROLE_NAMES:
            self.assertEqual(roles.role_text(self.repo, name), roles.EXTRA_DEFAULTS[name])
        with self.assertRaises(ValueError):
            roles.role_text(self.repo, "unknown-role")

    def test_file_override_and_fallback_follow_existing_role_rules(self):
        default = roles.role_text(self.repo, "challenger")
        self.assertEqual(default, roles.EXTRA_DEFAULTS["challenger"])
        self.assertEqual(default.splitlines()[0], FIRST)
        path = self.repo / "agents" / "challenger.md"
        path.parent.mkdir()
        path.write_bytes((FIRST + "\r\nCustom route rule.\r\n\r\n").encode())
        self.assertEqual(roles.role_text(self.repo, "challenger"), FIRST + "\nCustom route rule.")
        for raw in (b"", b" \n\t", b"\xff", b"x" * (roles.MAX_CHARS + 1)):
            with self.subTest(raw=raw[:20]):
                path.write_bytes(raw)
                self.assertEqual(roles.role_text(self.repo, "challenger"), default)
        path.unlink()
        path.mkdir()
        self.assertEqual(roles.role_text(self.repo, "challenger"), default)

    def test_repository_role_and_default_carry_the_challenger_rules(self):
        path = Path(__file__).resolve().parents[2] / "agents" / "challenger.md"
        self.assertTrue(path.is_file(), "the protected challenger role file is required")
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw)
        for text in (raw.decode("utf-8"), roles.EXTRA_DEFAULTS["challenger"]):
            self.assertEqual(text.splitlines()[0], FIRST)
            lower = text.lower()
            for term in ("working route", "plain code", "scratch", "discard", "test files",
                         "overturned", "stands", "patch", "capability", "readiness", "error", "claimant"):
                self.assertIn(term, lower)
            self.assertRegex(lower, r"(?:at least 2|at least two)")
            self.assertRegex(lower, r"(?:never|must not|do not) edit test files")

    def test_team_remains_six_positional_fields_and_real_challenger_is_codex(self):
        fakes = [agents.FakeAgent(lambda p, c: ('{}', 0)) for _ in LAYER_ONE]
        team = bootstrap.Team(*fakes)
        self.assertIsNone(team.challenger)
        self.assertEqual(set(vars(team)), set(LAYER_ONE))
        self.assertNotIn("challenger", {f.name for f in fields(team)})
        with patch("core.agents.launch", side_effect=AssertionError("real launch forbidden")):
            for limits, timeout in (({}, 1800), ({"agent_timeout_s": 123}, 123)):
                real = bootstrap.real_team(limits)
                self.assertIsInstance(real.challenger, agents.CodexAgent)
                self.assertEqual(real.challenger.provider, "codex")
                self.assertEqual(real.challenger.sandbox, "workspace-write")
                self.assertEqual(real.challenger.timeout_s, timeout)
                self.assertEqual(real.builder.provider, "claude")
                self.assertEqual(real.troubleshooter.provider, "claude")


class ChallengerSmoke(Harness):
    def smoke_team(self, challenger=True, write=True, extra=False):
        seen = []

        def fake(name):
            def script(prompt, cwd):
                seen.append((name, cwd))
                self.assertTrue((cwd / ".git").is_dir())
                self.assertEqual(git(cwd, "status", "--porcelain"), "")
                self.assertFalse((cwd / "smoke.txt").exists())
                if name == "challenger":
                    example, writes = json.dumps(SMOKE), write
                    self.assertIn("Change nothing else", prompt)
                else:
                    _, writes, example = bootstrap.SMOKE_ROLES[name]
                if writes:
                    (cwd / "smoke.txt").write_text("ok\n", encoding="utf-8")
                if name == "challenger" and extra:
                    (cwd / "extra.txt").write_text("unexpected", encoding="utf-8")
                return example, 0
            return agents.FakeAgent(script)

        team = bootstrap.Team(*(fake(name) for name in LAYER_ONE))
        if challenger:
            team.challenger = fake("challenger")
        return team, seen

    def test_challenger_smoke_contract(self):
        schema, writes, example = bootstrap.SMOKE_ROLES["challenger"]
        self.assertEqual(schema, bootstrap.S_CHALLENGE)
        self.assertIs(writes, True)
        self.assertEqual(json.loads(example), SMOKE)

    def test_writer_runs_once_in_an_independent_fresh_repo(self):
        team, seen = self.smoke_team()
        self.assertEqual(bootstrap.smoke(team, self.work), [])
        self.assertEqual([n for n, _ in seen].count("challenger"), 1)
        self.assertEqual(len(seen), 7)
        self.assertEqual(len({p for _, p in seen}), 7)

    def test_no_write_is_reported(self):
        team, _ = self.smoke_team(write=False)
        self.assertEqual(bootstrap.smoke(team, self.work), [
            "challenger: could not write smoke.txt containing ok (no write access?)"])

    def test_extra_write_is_rejected(self):
        team, _ = self.smoke_team(extra=True)
        problems = bootstrap.smoke(team, self.work)
        self.assertTrue(any("challenger:" in p and "extra.txt" in p for p in problems), problems)

    def test_absent_extra_members_are_skipped_with_and_without_callback(self):
        for callback in (False, True):
            with self.subTest(callback=callback):
                team, seen = self.smoke_team(challenger=False)
                calls = []

                def call(role, prompt, schema, cwd):
                    calls.append(role)
                    return getattr(team, role).run(prompt, cwd, schema)

                self.assertEqual(bootstrap.smoke(team, self.work, call=call if callback else None), [])
                self.assertEqual(len(seen), 6)
                if callback:
                    self.assertEqual(set(calls), set(LAYER_ONE))
                    self.assertEqual(len(calls), 6)


class ChallengerPlumbing(BlockerHarness):
    def attach_challenger(self, c, script=None):
        c.team.challenger = agents.FakeAgent(script or (lambda p, w: (json.dumps(SMOKE), 1)), provider="codex")
        return c.team.challenger

    def check_challenger_unavailable(self, held):
        c = self.init(limits={"codex_daily_token_cap": 10, "claude_daily_token_cap": 10**9})
        challenger = self.attach_challenger(c)
        if held:
            c.meter.hold("codex", c.clock() + timedelta(hours=1))
        else:
            c.meter.add("codex", 11)
        with self.assertRaises(bootstrap.Capped):
            c._call("challenger", "Find a route", bootstrap.S_CHALLENGE)
        self.assertEqual(challenger.prompts, [])
        self.assertEqual(c.team.builder.prompts, [])
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertIs(c.team.challenger, challenger)

    def test_cap_never_launches_or_substitutes_challenger(self):
        self.check_challenger_unavailable(held=False)

    def test_hold_never_launches_or_substitutes_challenger(self):
        self.check_challenger_unavailable(held=True)

    def test_call_includes_dead_ends_and_sets_run_id_before_launch(self):
        c = self.init()
        observed = []

        def script(prompt, cwd):
            observed.append(getattr(c, "last_run_id", None))
            return json.dumps(SMOKE), 1

        challenger = self.attach_challenger(c, script)
        (self.state / "dead_ends.jsonl").write_text(json.dumps({
            "task": "T1", "notes": "legacy parser cannot handle offsets", "alternative": "strptime"}) + "\n",
            encoding="utf-8")
        result = c._call("challenger", "Find a route", bootstrap.S_CHALLENGE)
        self.assertTrue(result.ok, result.error)
        self.assertIn("KNOWN DEAD ENDS", challenger.prompts[0])
        self.assertIn("legacy parser cannot handle offsets", challenger.prompts[0])
        self.assertEqual(observed, [c.last_run_id])
        run = self.state / "runs" / c.last_run_id
        self.assertTrue(run.is_dir())
        self.assertIn("-challenger-", run.name)
        self.assertEqual((run / "prompt.md").read_text(encoding="utf-8"), challenger.prompts[0])
        self.assertTrue((run / "output.json").is_file())

    def prepare_failed_build(self):
        def builder(prompt, cwd):
            (cwd / "feat.py").write_text("VALUE = 0\n", encoding="utf-8")
            return '{"status":"done"}', 1
        c = self.init(agents={"test_writer": self.write_tests, "builder": builder})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        return c

    def run_failing_judges(self, c):
        with patch.object(c, "_run_tests", return_value=(1, "AssertionError: 0 != 42", "")):
            self.assertEqual(c.step(), "worked")

    def test_builder_receives_challenger_route_after_troubleshooter_notes(self):
        c = self.prepare_failed_build()
        c._update("T1", challenger_routes=["use datetime.strptime"], trouble_notes=["inspect the parser"])
        self.run_failing_judges(c)
        prompt = c.team.builder.prompts[0]
        self.assertIn("CHALLENGER ROUTE:\n- use datetime.strptime", prompt)
        self.assertLess(prompt.index("TROUBLESHOOTER NOTES"), prompt.index("CHALLENGER ROUTE"))

    def test_builder_keeps_only_last_three_routes_and_caps_each(self):
        c = self.prepare_failed_build()
        routes = ["obsolete route", "use datetime.strptime", "x" * (bootstrap.NOTE_CAP + 20), "use ISO parser"]
        c._update("T1", challenger_routes=routes)
        self.run_failing_judges(c)
        prompt = c.team.builder.prompts[0]
        expected = "CHALLENGER ROUTE:\n" + "\n".join("- " + r[:bootstrap.NOTE_CAP] for r in routes[-3:])
        self.assertIn(expected, prompt)
        self.assertNotIn("obsolete route", prompt)
        self.assertNotIn("x" * (bootstrap.NOTE_CAP + 1), prompt)

    def test_failed_attempt_diff_survives_worktree_reset(self):
        c = self.prepare_failed_build()
        base = git(c.wt, "rev-parse", "HEAD")
        self.run_failing_judges(c)
        task = c._task("T1")
        self.assertIn("feat.py", task["last_attempt_diff"])
        self.assertIn("VALUE = 0", task["last_attempt_diff"])
        twt = c.trees.task_path("T1")
        self.assertEqual(git(twt, "rev-parse", "HEAD"), base)
        self.assertEqual(git(twt, "status", "--porcelain"), "")
        self.assertFalse((twt / "feat.py").exists())
        self.assertNotIn("CHALLENGER ROUTE", c.team.builder.prompts[0])

    def test_saved_diff_is_capped(self):
        c = self.prepare_failed_build()
        with patch.object(c, "_attempt_diff", return_value="diff feat.py\n" + "x" * 25000):
            self.run_failing_judges(c)
        self.assertEqual(c._task("T1")["last_attempt_diff"], ("diff feat.py\n" + "x" * 25000)[:20000])

    def check_unavailable_diff(self, error):
        c = self.prepare_failed_build()
        with patch.object(c, "_attempt_diff", side_effect=error):
            self.run_failing_judges(c)
        self.assertEqual(c._task("T1")["last_attempt_diff"], f"(diff unavailable: {error})")
        self.assertEqual(c._task("T1")["fails_since"], 1)
        self.assertFalse((c.trees.task_path("T1") / "feat.py").exists())

    def test_runtime_error_reading_diff_still_records_failure(self):
        self.check_unavailable_diff(RuntimeError("git diff failed"))

    def test_os_error_reading_diff_still_records_failure(self):
        self.check_unavailable_diff(OSError("diff access denied"))

    def test_suppression_records_both_failures_without_handoff(self):
        c = self.init()
        for reason in ("first failure", "second failure"):
            c._after_failure("T1", reason, "same-signature", "judge output", suppress_handoff=True)
        task = c._task("T1")
        self.assertEqual(task["fail_signatures"], ["same-signature", "same-signature"])
        self.assertEqual(task["notes"], ["first failure", "second failure"])
        self.assertEqual(task["fails_since"], 2)
        self.assertEqual(task["status"], "todo")
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertFalse(task.get("troubleshoot_pending"))

    def test_suppression_prevents_blocking_even_when_rounds_are_spent(self):
        c = self.init()
        c._update("T1", troubleshoots=3, fails_since=1)
        c._after_failure("T1", "still failed", "same", "output", handoff=True, focus=True,
                         suppress_handoff=True)
        task = c._task("T1")
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["fails_since"], 2)
        self.assertEqual(task["notes"], ["still failed"])
        self.assertEqual(c.team.troubleshooter.prompts, [])

    def test_default_failure_policy_still_hands_off_on_second_failure(self):
        c = self.init()
        c._after_failure("T1", "first failure", "same", "judge output")
        self.assertEqual(c.team.troubleshooter.prompts, [])
        c._after_failure("T1", "second failure", "same", "judge output")
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)
        self.assertIn("second failure", c.team.troubleshooter.prompts[0])
        self.assertEqual(c._task("T1")["fail_signatures"], ["same", "same"])


if __name__ == "__main__":
    unittest.main()
