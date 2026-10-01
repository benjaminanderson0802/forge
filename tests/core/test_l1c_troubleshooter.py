"""L1C_2: usable troubleshooting answers and isolated, real conductor runs."""
import json
import unittest
from datetime import datetime, timezone

from core import bootstrap
from core.agents import ClaudeAgent
from tests.core.test_bootstrap import Harness


class TroubleProblemTests(unittest.TestCase):
    def test_usable_answers(self):
        for answer in (
            {"kind": "fix", "notes": "n"},
            {"kind": "suggestion", "notes": "n"},
            {"kind": "dead_end", "notes": "n", "alternative": "a"},
            {"kind": "fix", "notes": " n ", "alternative": None},
            {"kind": "suggestion", "notes": "n", "alternative": ""},
        ):
            with self.subTest(answer=answer):
                self.assertIsNone(bootstrap.trouble_problem(answer))

    def test_reasons_and_validation_order(self):
        cases = [([], "answer is not an object"),
                 (None, "answer is not an object"),
                 ({}, "unknown kind"),
                 ({"kind": "other", "notes": "n"}, "unknown kind"),
                 ({"kind": "other", "notes": ""}, "unknown kind")]
        for notes in (None, "", " \t\n", 7, [], {}):
            cases.append(({"kind": "dead_end", "notes": notes}, "notes are empty"))
        cases.append(({"kind": "fix"}, "notes are empty"))
        cases.append(({"kind": "dead_end", "notes": "n"}, "dead end without an alternative"))
        for alternative in (None, "", " \t\n", 7, [], {}):
            cases.append(({"kind": "dead_end", "notes": "n", "alternative": alternative},
                          "dead end without an alternative"))
        for answer, reason in cases:
            with self.subTest(answer=answer):
                self.assertEqual(bootstrap.trouble_problem(answer), reason)

    def test_real_troubleshooter_engine_and_research_tools(self):
        agent = bootstrap.real_team({"agent_timeout_s": 60}).troubleshooter
        self.assertIsInstance(agent, ClaudeAgent)
        self.assertEqual(agent.permission_mode, "acceptEdits")
        self.assertTrue({"WebSearch", "WebFetch"}.issubset(agent.allowed_tools))


class TroubleshooterConductorTests(Harness):
    def dead_ends(self):
        path = self.state / "dead_ends.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()] if path.exists() else []

    def failing_tests(self, prompt, cwd):
        path = cwd / "tests/core/test_feat.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import unittest\nclass T(unittest.TestCase):\n"
                        " def test_fail(self): self.fail('same judge failure')\n", encoding="utf-8")
        return json.dumps({"files": ["tests/core/test_feat.py"]}), 1

    def run_until_troubleshot(self, answer):
        self.scratch_cwds, self.builder_runs = [], []

        def troubleshoot(prompt, cwd):
            self.scratch_cwds.append(cwd)
            (cwd / "scratch.txt").write_text("private investigation", encoding="utf-8")
            return json.dumps(answer), 1

        def builder(prompt, cwd):
            self.builder_runs.append((cwd, (cwd / "scratch.txt").exists(), prompt))
            return '{"status":"done"}', 1

        c = self.init(agents={"test_writer": self.failing_tests, "builder": builder,
                              "troubleshooter": troubleshoot})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        for _ in range(8):
            self.assertEqual(c.step(), "worked")
            if self.scratch_cwds:
                break
        self.assertEqual(len(self.scratch_cwds), 1)
        task = c._task("T1")
        self.assertTrue(task["troubleshot"])
        self.assertEqual(task["fails_since"], 0)
        self.assertEqual(task["focus_s"], 0)
        self.assertEqual(task["troubleshoots"], 1)
        self.assertIsNone(task["troubleshoot_pending"])
        return c

    def test_missing_alternative_is_failed_answer_and_not_a_dead_end(self):
        c = self.run_until_troubleshot({"kind": "dead_end", "notes": "blocked"})
        self.assertEqual(c._task("T1")["trouble_notes"][-1],
                         "(troubleshooter failed: dead end without an alternative)")
        self.assertEqual(self.dead_ends(), [])

    def test_blank_notes_are_failed_even_with_an_alternative(self):
        c = self.run_until_troubleshot({"kind": "dead_end", "notes": " \t", "alternative": "table"})
        self.assertEqual(c._task("T1")["trouble_notes"][-1], "(troubleshooter failed: notes are empty)")
        self.assertEqual(self.dead_ends(), [])

    def test_valid_dead_end_reaches_next_builder_but_scratch_edits_do_not(self):
        c = self.run_until_troubleshot({"kind": "dead_end", "notes": "blocked", "alternative": "use a table"})
        self.assertEqual(self.dead_ends(), [{"task": "T1", "notes": "blocked", "alternative": "use a table"}])
        self.assertEqual(c._task("T1")["trouble_notes"][-1], "blocked")
        self.assertNotEqual(self.scratch_cwds[0].resolve(), c.wt.resolve())
        self.assertFalse((c.wt / "scratch.txt").exists())
        previous_runs = len(self.builder_runs)
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(self.builder_runs), previous_runs + 1)
        cwd, has_scratch, prompt = self.builder_runs[-1]
        self.assertNotEqual(cwd.resolve(), self.scratch_cwds[0].resolve())
        self.assertFalse(has_scratch)
        self.assertFalse((cwd / "scratch.txt").exists())
        self.assertIn("TROUBLESHOOTER NOTES:", prompt)
        self.assertIn("KNOWN DEAD ENDS:", prompt)
        self.assertIn("use a table", prompt.split("KNOWN DEAD ENDS:", 1)[1])

    def capability_job(self, answer, recovers=False):
        events = []
        job_ran = False

        def docker_check():
            events.append("check_after" if job_ran else "check_before")
            return (True, "fixed") if job_ran and recovers else (
                False, "error during connect: cannot connect to the Docker daemon")

        def troubleshoot(prompt, cwd):
            nonlocal job_ran
            events.append("job")
            job_ran = True
            return json.dumps(answer), 1

        c = self.init(self.task(needs=["docker"]), agents={"troubleshooter": troubleshoot})
        c.checks["docker"] = docker_check
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        c.clock = lambda: now
        c.meter.clock = c.clock
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)
        self.assertIn("CAPABILITY FIX JOB: docker", c.team.troubleshooter.prompts[0])
        self.assertIn("check_after", events[events.index("job") + 1:])
        routing = json.loads((self.state / "cap_routing.json").read_text(encoding="utf-8"))
        if recovers:
            self.assertNotIn("docker", routing)
        else:
            self.assertEqual(routing["docker"]["rounds"], 1)
            self.assertEqual(routing["docker"]["last_job"], now.isoformat())
            c.step()
            self.assertEqual(len(c.team.troubleshooter.prompts), 1, "retry cooldown must still apply")
        return c

    def test_capability_job_rejects_missing_alternative_and_keeps_bookkeeping(self):
        self.capability_job({"kind": "dead_end", "notes": "blocked"})
        self.assertEqual(self.dead_ends(), [])

    def test_capability_job_rejects_blank_notes(self):
        self.capability_job({"kind": "dead_end", "notes": "  ", "alternative": "use another runtime"})
        self.assertEqual(self.dead_ends(), [])

    def test_capability_job_preserves_valid_alternative_and_bookkeeping(self):
        self.capability_job({"kind": "dead_end", "notes": "blocked", "alternative": "use another runtime"})
        self.assertEqual(self.dead_ends(), [{"capability": "docker", "notes": "blocked",
                                            "alternative": "use another runtime"}])

    def test_rejected_capability_answer_still_refreshes_and_resolves_recovery(self):
        self.capability_job({"kind": "dead_end", "notes": "blocked"}, recovers=True)
        self.assertEqual(self.dead_ends(), [])


if __name__ == "__main__":
    unittest.main()
