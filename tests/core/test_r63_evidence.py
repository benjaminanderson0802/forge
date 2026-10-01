"""R63: evidence tasks prove existing code without weakening the ordinary gates."""
import json
import unittest
from unittest.mock import patch

from core import coverage
from core.agents import schema_ok
from core.bootstrap import S_PLAN
from core.ledger import Ledger
from tests.core import test_r62_plan_covers as r62
from tests.core.test_bootstrap import git
from tests.core.test_merge_pipeline import Pipeline
from tests.core.test_planning_drift import DriftHarness, SPEC, btask


class EvidencePlanTests(DriftHarness):
    # Reuse R62's valid long sections and planner, without inheriting its tests.
    planned_task = r62.PlanCoversTests.planned_task
    run_plan = r62.PlanCoversTests.run_plan
    assert_accepted = r62.PlanCoversTests.assert_accepted

    def test_plan_preserves_optional_evidence_and_explains_when_to_use_it(self):
        evidence = self.planned_task(covers=["1.1"], evidence=True)
        ordinary = self.planned_task("T2", covers=["1.2"])
        self.run_plan(evidence, ordinary)
        self.assert_accepted("T1", "T2")
        with self.subTest(check="queue"):
            self.assertIs(self.task_rec("T1").get("evidence"), True)
            self.assertNotIn("evidence", self.task_rec("T2"))
        with self.subTest(check="schema"):
            item = S_PLAN["properties"]["tasks"]["items"]
            self.assertEqual(item["properties"].get("evidence"), {"type": "boolean"})
            self.assertNotIn("evidence", item["required"])
            self.assertTrue(schema_ok({"tasks": [evidence, ordinary]}, S_PLAN))
        with self.subTest(check="planner instructions"):
            prompt = self.team.planner.prompts[0].lower()
            self.assertIn("evidence", prompt)
            self.assertRegex(prompt, r"already (?:meets?|satisf(?:y|ies)|implements?|works?|exists?)")
            self.assertRegex(prompt, r"ordinary|non-evidence|normal (?:build )?tasks?")


class EvidenceTestsStageTests(DriftHarness):
    def start_existing(self, *, value=1, evidence=True, body=None):
        # A function is essential: the real empty-implementation check replaces
        # function bodies, but deliberately preserves module-level constants.
        (self.repo / "m_t1.py").write_bytes(f"def value():\n    return {value}\n".encode())
        git(self.repo, "add", "m_t1.py")
        git(self.repo, "commit", "-q", "-m", "existing implementation")
        task = btask("T1", ["1.1"])
        task["section"] = "Prove m_t1.value() returns the integer 1."
        if evidence:
            task["evidence"] = True

        def writer(prompt, cwd):
            path = cwd / task["test_files"][0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((body if body is not None else
                              "import unittest\nimport m_t1\n"
                              "class T(unittest.TestCase):\n"
                              "    def test_value(self):\n"
                              "        self.assertEqual(m_t1.value(), 1)\n").encode())
            return json.dumps({"files": task["test_files"]}), 1

        c = self.make_conductor({"test_writer": writer})
        c.init_queue(self.layer, [task])
        return c

    def assert_rejected(self, c, head, prefix):
        task = self.task_rec("T1")
        with self.subTest(check="rejection note"):
            self.assertTrue(any(n.startswith(prefix) for n in task["notes"]), task["notes"])
        with self.subTest(check="rejected without committing"):
            self.assertEqual(task["status"], "todo")
            self.assertEqual(task.get("test_rejects", 0), 1)
            self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)
            self.assertNotIn("tests/core/test_t1.py", self.branch_files())
            self.assertEqual(git(c.wt, "status", "--porcelain", "-uall"), "")

    def test_writer_prompt_requires_passing_proof_of_existing_covers(self):
        c = self.start_existing()
        self.assertEqual(c.step(), "worked")
        prompt = self.team.test_writer.prompts[0].lower()
        self.assertRegex(prompt, r"(?:code|implementation|feature) already exists|already implemented")
        self.assertIn("covers", prompt)
        self.assertIn("1.1", prompt)
        self.assertRegex(prompt, r"prov(?:e|ing)|demonstrat")
        self.assertRegex(prompt, r"must pass|pass (?:now|on the current|against the existing)")

    def test_passing_current_failing_empty_tests_are_committed(self):
        c = self.start_existing()
        head = git(c.wt, "rev-parse", "HEAD")
        current_runs = []
        stub_runs = []
        real_current = c._run_tests
        real_stub = c._run_tests_on_stub

        def current(task):
            result = real_current(task)
            current_runs.append(result)
            return result

        def stub(task):
            result = real_stub(task)
            stub_runs.append(result)
            return result

        with patch.object(c, "_run_tests", side_effect=current), \
                patch.object(c, "_run_tests_on_stub", side_effect=stub):
            self.assertEqual(c.step(), "worked")
        self.assertEqual(len(current_runs), 1)
        self.assertEqual(current_runs[0][0], 0, current_runs[0][1])
        self.assertFalse(current_runs[0][2])
        self.assertIn("Ran 1 test", current_runs[0][1])
        task = self.task_rec("T1")
        self.assertEqual(task["status"], "tests_ok", task["notes"])
        self.assertEqual(task.get("test_rejects", 0), 0)
        self.assertEqual(len(stub_runs), 1)
        code, output, timed_out = stub_runs[0]
        self.assertNotEqual(code, 0)
        self.assertFalse(timed_out)
        self.assertIn("Ran 1 test", output)
        self.assertIn("None != 1", output)
        sha = task["tests_commit"]
        self.assertNotEqual(sha, head)
        self.assertEqual(sha, git(c.wt, "rev-parse", "HEAD"))
        self.assertEqual(git(c.wt, "diff", "--name-only", head, sha), "tests/core/test_t1.py")
        self.assertIn("self.assertEqual(m_t1.value(), 1)",
                      git(c.wt, "show", f"{sha}:tests/core/test_t1.py"))
        self.assertEqual((c.wt / "m_t1.py").read_text(), "def value():\n    return 1\n")

    def test_failing_current_tests_are_rejected_with_output_tail(self):
        c = self.start_existing(value=0)
        head = git(c.wt, "rev-parse", "HEAD")
        self.assertEqual(c.step(), "worked")
        prefix = "tests rejected: evidence tests fail on the current code"
        self.assert_rejected(c, head, prefix)
        notes = "\n".join(self.task_rec("T1")["notes"])
        self.assertIn("AssertionError: 0 != 1", notes)
        self.assertIn("FAILED (failures=1)", notes)

    def test_passing_current_and_empty_tests_are_rejected_as_weak(self):
        c = self.start_existing(body="import unittest\nimport m_t1\n"
                               "class T(unittest.TestCase):\n"
                               "    def test_import(self): self.assertTrue(callable(m_t1.value))\n")
        head = git(c.wt, "rev-parse", "HEAD")
        with patch.object(c, "_run_tests_on_stub", wraps=c._run_tests_on_stub) as stub:
            self.assertEqual(c.step(), "worked")
        self.assert_rejected(c, head, "tests rejected: weak (they pass on an empty implementation)")
        stub.assert_called_once()

    def test_timeout_or_no_tests_is_rejected_without_a_real_passing_run(self):
        c = self.start_existing()
        head = git(c.wt, "rev-parse", "HEAD")
        # Exercise both runner outcomes without sleeping. Reset only the attempt
        # counter so each subcase is the first rejection, not the R4 block path.
        for result in ((124, "test command timed out", True), (0, "Ran 0 tests\n\nOK", False)):
            with self.subTest(result=result):
                c._update("T1", status="todo", test_rejects=0, notes=[])
                with patch.object(c, "_run_tests", return_value=result), \
                        patch.object(c, "_run_tests_on_stub", wraps=c._run_tests_on_stub) as stub:
                    self.assertEqual(c.step(), "worked")
                self.assert_rejected(c, head, "tests rejected: no real passing run")
                stub.assert_not_called()

    def test_ordinary_task_still_rejects_tests_passing_current_code(self):
        c = self.start_existing(evidence=False)
        head = git(c.wt, "rev-parse", "HEAD")
        self.assertEqual(c.step(), "worked")
        self.assertNotIn("evidence", self.task_rec("T1"))
        self.assert_rejected(c, head, "tests rejected: weak (they pass before the feature exists)")


class EvidenceBuildTests(Pipeline):
    def test_no_change_build_is_judged_reviewed_submitted_and_credited(self):
        self.agents["builder"] = lambda prompt, cwd: ('{"status":"done"}', 1)
        c = self.conductor(judge_cmds=['python -c "print(\'drill judge\')"',
                                      'python -c "print(\'suite judge\')"'])
        task = self.task(evidence=True, covers=["1.1"])
        c.init_queue(self.layer, [task])
        # Seed the accepted-tests boundary so a broken Stage A cannot mask the
        # independently specified builder/reviewer prompts and no-diff behavior.
        (c.wt / "feat.py").write_bytes(b"VALUE = 42\n")
        self.writer(c._task_prompt(task), c.wt)
        git(c.wt, "add", "feat.py", "tests/core/test_feat.py")
        git(c.wt, "commit", "-q", "-m", "existing code and accepted evidence tests")
        head = git(c.wt, "rev-parse", "HEAD")
        c._update("T1", status="tests_ok", tests_commit=head)
        git(self.repo, "push", "-q", "origin", self.layer)

        with patch.object(c, "_run_cmd", wraps=c._run_cmd) as judges, \
                patch.object(c, "_mutation_judge", wraps=c._mutation_judge) as mutation:
            self.assertEqual(c.step(), "worked")
        self.assertEqual(judges.call_count, 2)
        mutation.assert_called_once()
        self.assertEqual(mutation.call_args.args[2], head)
        runs = self.log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(line.endswith("|" + head) for line in runs), runs)
        self.assertEqual(len(self.review_log), 1)
        self.assertEqual(self.review_log[0]["head"], head)
        self.assertEqual(len(self.team.builder.prompts), 1)
        self.assertEqual(self.queue_task("T1")["status"], "done")
        self.assertEqual(self.task_commit("T1"), head)
        self.assertEqual(self.origin_tip(), head)
        ledger = Ledger(self.state)
        submits = [e for e in ledger.events() if e["action"] == "submit"]
        self.assertEqual([e["payload"]["commit"] for e in submits], [head])
        self.assertEqual(ledger.contracts()["T1"]["status"], "done")
        self.assertEqual(len(self.passes("T1")), 1)
        self.assertTrue(any(r["passed"] and r["commit"] == head for r in ledger.test_runs().values()))
        self.assertEqual(ledger.completion("T1")["payload"]["verdict"], "pass")
        self.check_invariants("T1")
        credited = coverage.compute(coverage.parse_requirements(SPEC), [self.queue_task("T1")], {"T1"})
        self.assertGreater(credited.score, 0)
        with self.subTest(check="builder instructions"):
            prompt = self.team.builder.prompts[0].lower()
            self.assertRegex(prompt, r"tests already pass|tests (?:are|have) already pass")
            self.assertRegex(prompt, r"change nothing|no changes|do not change")
            self.assertRegex(prompt, r"unless.*(?:test|judge).*fail")
        with self.subTest(check="reviewer instructions"):
            prompt = self.review_log[0]["prompt"].lower()
            self.assertIn("evidence task", prompt)
            self.assertIn("covers", prompt)
            self.assertIn("1.1", prompt)
            self.assertRegex(prompt, r"(?:fail|reject).*tests.*(?:prove|demonstrat|cover)")


if __name__ == "__main__":
    unittest.main()
