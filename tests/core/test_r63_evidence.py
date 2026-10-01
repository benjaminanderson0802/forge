"""R63: evidence tasks prove existing code without weakening the ordinary gates."""
import json
import unittest
from unittest.mock import patch

from core import coverage
from core.agents import schema_ok
from core.bootstrap import S_PLAN
from core.ledger import Ledger
from tests.core import test_r62_plan_covers as r62
from tests.core.test_bootstrap import Harness, git
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
    def run_empty_diff_mutation(self, assertion):
        self.agents["builder"] = lambda prompt, cwd: ('{"status":"done"}', 1)
        c = self.conductor()
        task = self.task(evidence=True, covers=["1.1"])
        c.init_queue(self.layer, [task])
        (c.wt / "feat.py").write_bytes(b"def value():\n    return 42\n")
        path = c.wt / task["test_files"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("import unittest\nimport feat\n"
                          "class T(unittest.TestCase):\n"
                          "    def test_value(self):\n"
                          f"        {assertion}\n").encode())
        git(c.wt, "add", "feat.py", task["test_files"][0])
        git(c.wt, "commit", "-q", "-m", "existing function and accepted evidence tests")
        head = git(c.wt, "rev-parse", "HEAD")
        c._update("T1", status="tests_ok", tests_commit=head)
        git(self.repo, "push", "-q", "origin", self.layer)
        results = []
        real_judge = c._mutation_judge

        def judge(t, base, sha, baseline, root):
            self.assertEqual(base, head)
            self.assertEqual(sha, head)
            self.assertEqual(git(root, "diff", base, sha), "")
            result = real_judge(t, base, sha, baseline, root)
            results.append(result)
            return result

        with patch.object(c, "_mutation_judge", side_effect=judge):
            self.assertEqual(c.step(), "worked")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.review_log), 1)  # the fake reviewer passes
        self.assertEqual(self.review_log[0]["head"], head)
        return results[0], head

    def test_empty_diff_weak_evidence_runs_surviving_mutant_and_cannot_finish(self):
        result, head = self.run_empty_diff_mutation("self.assertIsNotNone(feat.value())")
        with self.subTest(check="real mutation run"):
            self.assertGreater(result.total, 0)
            self.assertTrue(result.complete)
            self.assertEqual(result.not_run, [])
            self.assertEqual(result.killed, 0)
            self.assertEqual(len(result.survivors), result.total)
            self.assertEqual({(m.file, m.line) for m in result.survivors}, {("feat.py", 2)})
            self.assertFalse(result.passed)
        with self.subTest(check="mutation overrides passing reviewer"):
            self.assertNotEqual(self.queue_task("T1")["status"], "done")
            self.assertEqual(self.passes("T1"), [])
            failures = [e for e in Ledger(self.state).events() if e["action"] == "fail"]
            self.assertTrue(failures)
            self.assertEqual(failures[-1]["payload"]["verdict"], "pass")
            self.assertEqual(failures[-1]["payload"]["gate"], "mutation")
            self.assertEqual(failures[-1]["payload"]["mutation"], result.as_dict())

    def test_empty_diff_strong_evidence_kills_nonzero_mutants_and_finishes(self):
        result, head = self.run_empty_diff_mutation("self.assertEqual(feat.value(), 42)")
        with self.subTest(check="real mutation run"):
            self.assertGreater(result.total, 0)
            self.assertEqual(result.killed, result.total)
            self.assertEqual(result.survivors, [])
            self.assertEqual(result.not_run, [])
            self.assertTrue(result.complete)
            self.assertTrue(result.passed)
        with self.subTest(check="verified completion"):
            self.assertEqual(self.queue_task("T1")["status"], "done")
            completion = Ledger(self.state).completion("T1")
            self.assertIsNotNone(completion)
            self.assertEqual(completion["payload"]["mutation"], result.as_dict())
            self.assertEqual(self.task_commit("T1"), head)
            self.check_invariants("T1")

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


class EvidenceMutationTargetTests(Harness):
    SOURCE = ("TOP = 10\n"                     # 1
              "def value():\n"                # 2
              "    answer = 42\n"             # 3
              "    return answer\n"           # 4
              "\n"                            # 5
              "def unused():\n"               # 6
              "    return 99\n"               # 7
              "\n"                            # 8
              "class Box:\n"                  # 9
              "    def read(self):\n"         # 10
              "        return 7\n")           # 11

    def target_fixture(self, *, evidence=True, names="value read"):
        c = self.make_conductor()
        # Broad scope deliberately includes test files and a non-Python file.
        task = self.task(files_in_scope=["*.py", "notes.txt"])
        if evidence:
            task["evidence"] = True
        files = {"feat.py": self.SOURCE,
                 "extra.py": "def unmentioned():\n    return 5\n",
                 "outside.txt": "value read\n",
                 "notes.txt": "value read\n",
                 task["test_files"][0]: f"# {names}\ndef test_proof():\n    assert True\n"}
        for name, source in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.encode())
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "target selection fixture")
        head = git(self.repo, "rev-parse", "HEAD")
        # A matching untracked file must never be selected.
        (self.repo / "untracked.py").write_bytes(b"def value():\n    return 8\n")
        return c, task, head

    def targets(self, c, task, base, sha):
        # R63's selection seam; keep this separate from subprocess mutation runs.
        helper = getattr(c, "_mutation_targets", None)
        self.assertTrue(callable(helper), "Conductor._mutation_targets(t, base, sha, root) is missing")
        return helper(task, base, sha, self.repo)

    def change_targets(self, task):
        (self.repo / "feat.py").write_bytes(self.SOURCE.replace("TOP = 10", "TOP = 11").encode())
        (self.repo / task["test_files"][0]).write_bytes(b"# value read\ndef test_proof():\n    assert 2\n")
        git(self.repo, "add", "feat.py", task["test_files"][0])
        git(self.repo, "commit", "-q", "-m", "change code and tests")
        return git(self.repo, "rev-parse", "HEAD")

    def test_evidence_targets_named_functions_and_methods_as_whole_words_only(self):
        c, task, head = self.target_fixture(names="value read unused_suffix prefix_unused")
        self.assertEqual(self.targets(c, task, head, head), {"feat.py": {2, 3, 4, 10, 11}})

    def test_evidence_targets_union_changed_lines_and_named_definitions_excluding_tests(self):
        c, task, base = self.target_fixture()
        sha = self.change_targets(task)
        self.assertEqual(self.targets(c, task, base, sha), {"feat.py": {1, 2, 3, 4, 10, 11}})

    def test_evidence_without_named_functions_targets_every_in_scope_python_line(self):
        c, task, head = self.target_fixture(names="value_suffix prefix_read unused_suffix")
        self.assertEqual(self.targets(c, task, head, head),
                         {"feat.py": set(range(1, 12)), "extra.py": {1, 2}})

    def test_ordinary_targets_only_changed_lines_even_when_tests_name_functions(self):
        c, task, base = self.target_fixture(evidence=False)
        with self.subTest(diff="empty"):
            self.assertEqual(self.targets(c, task, base, base), {})
        sha = self.change_targets(task)
        with self.subTest(diff="code and tests"):
            self.assertEqual(self.targets(c, task, base, sha), {"feat.py": {1}})


if __name__ == "__main__":
    unittest.main()
