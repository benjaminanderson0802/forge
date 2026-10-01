"""R65: failed evidence returns to the test writer, with bounded feedback."""
import json
import unittest
from unittest.mock import patch

from core.ledger import Ledger
from tests.core.test_bootstrap import git
from tests.core.test_merge_pipeline import Pipeline


class EvidenceRewriteTests(Pipeline):
    # R63's Pipeline harness: local bare origin, real worktrees and judges,
    # FakeAgents for the writer/builder/reviewer, and no live services.
    WEAK = "self.assertIsNotNone(feat.value())"
    STRONG = "self.assertEqual(feat.value(), 42)"

    def start_evidence(self, *, evidence=True, strong=False, review_reasons=None):
        source = "def value():\n    return 42\n"
        (self.repo / "feat.py").write_bytes(
            (source if evidence else "def value():\n    return None\n").encode())
        git(self.repo, "add", "feat.py")
        git(self.repo, "commit", "-q", "-m", "existing function")
        self.assertion = self.STRONG if strong else self.WEAK

        def writer(prompt, cwd):
            path = cwd / "tests/core/test_feat.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("import unittest\nimport feat\n"
                              "class T(unittest.TestCase):\n"
                              "    def test_value(self):\n"
                              f"        {self.assertion}\n").encode())
            return json.dumps({"files": ["tests/core/test_feat.py"]}), 1

        def builder(prompt, cwd):
            if not evidence:
                (cwd / "feat.py").write_bytes(source.encode())
            return '{"status":"done"}', 1

        def reviewer(prompt, cwd):
            passing = self.reviewer(prompt, cwd)  # retain Pipeline's review log
            if review_reasons is not None:
                return json.dumps({"verdict": "fail", "reasons": review_reasons}), 1
            return passing

        self.agents.update(test_writer=writer, builder=builder, reviewer=reviewer)
        c = self.conductor()
        task = self.task(covers=["1.1"])
        if evidence:
            task["evidence"] = True
        c.init_queue(self.layer, [task])
        # Acceptance uses real R63 runs: assertIsNotNone passes on 42, but
        # fails when the function body is emptied (returns None).
        with patch.object(c, "_run_tests_on_stub", wraps=c._run_tests_on_stub) as stub:
            self.assertEqual(c.step(), "worked")
        stub.assert_called_once()
        accepted = self.queue_task("T1")
        self.assertEqual(accepted["status"], "tests_ok", accepted["notes"])
        self.assertTrue(accepted["tests_commit"])
        git(self.repo, "push", "-q", "origin", self.layer)
        return c

    def build_attempt(self, c, *, mutation_passes=False, verdict="pass"):
        results = []
        real_judge = c._mutation_judge

        def judge(*args):
            result = real_judge(*args)
            results.append(result)
            return result

        with patch.object(c, "_mutation_judge", side_effect=judge):
            self.assertEqual(c.step(), "worked")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertGreater(result.total, 0)
        self.assertTrue(result.complete, result.as_dict())
        self.assertEqual(result.not_run, [])
        self.assertEqual(result.passed, mutation_passes, result.as_dict())
        self.assertEqual(len(self.review_log), 1)
        # The ordinary test run passed; only mutation or review failed.
        ledger = Ledger(self.state)
        self.assertTrue(any(r["passed"] for r in ledger.test_runs().values()))
        failures = [e for e in ledger.events()
                    if e["action"] == "fail" and e["contract_id"] == "T1"]
        self.assertEqual(len(failures), 1)
        payload = failures[0]["payload"]
        self.assertEqual(payload["gate"], "review" if mutation_passes else "mutation")
        self.assertEqual(payload["verdict"], verdict)
        self.assertEqual(payload["mutation"], result.as_dict())
        self.assertEqual(self.passes("T1"), [])
        return result, payload

    @staticmethod
    def survivor_lines(result):
        return [f"{m.file}:{m.line} {m.original} -> {m.replacement}"
                for m in result.survivors]

    @staticmethod
    def feedback_text(task):
        feedback = task.get("test_feedback") or []
        return feedback if isinstance(feedback, str) else "\n".join(feedback)

    def test_mutation_failure_records_attempt_and_returns_evidence_with_survivors(self):
        c = self.start_evidence()
        c._update("T1", test_rejects=1)  # a previous writer rejection must be reset
        result, payload = self.build_attempt(c)
        self.assertEqual(result.killed, 0)
        self.assertEqual(len(result.survivors), result.total)
        task = self.queue_task("T1")
        with self.subTest(check="return to writer"):
            self.assertEqual(task["status"], "todo")
        with self.subTest(check="clear accepted commit"):
            self.assertFalse(task.get("tests_commit"))
        with self.subTest(check="reset writer rejections"):
            self.assertEqual(task.get("test_rejects", 0), 0)
        with self.subTest(check="count return"):
            self.assertEqual(task.get("evidence_rewrites", 0), 1)
        for line in self.survivor_lines(result) + payload["reasons"]:
            with self.subTest(feedback=line):
                self.assertIn(line, self.feedback_text(task))

    def test_review_failure_returns_evidence_with_reviewer_reasons(self):
        reasons = ["Tests do not prove covers 1.1's integer return contract.",
                   "Add an assertion for the result type."]
        c = self.start_evidence(strong=True, review_reasons=reasons)
        result, payload = self.build_attempt(c, mutation_passes=True, verdict="fail")
        self.assertEqual(result.survivors, [])
        self.assertEqual(payload["reasons"], reasons)
        task = self.queue_task("T1")
        with self.subTest(check="return to writer"):
            self.assertEqual(task["status"], "todo")
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(reason, self.feedback_text(task))

    def test_next_writer_gets_survivors_and_acceptance_clears_feedback(self):
        c = self.start_evidence()
        old_commit = self.queue_task("T1")["tests_commit"]
        result, payload = self.build_attempt(c)
        lines = self.survivor_lines(result)
        self.assertTrue(lines)
        # Seed the R65 stage boundary to test prompt/acceptance independently
        # of the return transition checked above (missing on pre-R65 code).
        c._update("T1", status="todo", tests_commit=None, test_rejects=0,
                  evidence_rewrites=1, test_feedback=lines + payload["reasons"])
        self.assertion = self.STRONG
        current_runs, stub_runs = [], []
        real_current, real_stub = c._run_tests, c._run_tests_on_stub

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
        self.assertEqual(len(self.team.test_writer.prompts), 2)
        prompt = self.team.test_writer.prompts[-1]
        with self.subTest(check="feedback prompt"):
            self.assertIn("TEST FEEDBACK", prompt)
            for line in lines:
                self.assertIn(line, prompt)
            self.assertRegex(prompt.lower(), r"(?:stronger|strengthen|kill)")
        self.assertEqual(len(current_runs), 1)
        self.assertEqual(current_runs[0][0], 0, current_runs[0][1])
        self.assertFalse(current_runs[0][2])
        self.assertEqual(len(stub_runs), 1)
        self.assertNotEqual(stub_runs[0][0], 0, stub_runs[0][1])
        self.assertFalse(stub_runs[0][2])
        self.assertIn("None != 42", stub_runs[0][1])
        task = self.queue_task("T1")
        self.assertEqual(task["status"], "tests_ok", task["notes"])
        self.assertTrue(task["tests_commit"])
        self.assertNotEqual(task["tests_commit"], old_commit)
        self.assertEqual(git(c.wt, "diff", "--name-only", old_commit, task["tests_commit"]),
                         "tests/core/test_feat.py")
        self.assertEqual(task["evidence_rewrites"], 1)
        with self.subTest(check="clear accepted feedback"):
            self.assertFalse(task.get("test_feedback"))

    def test_fourth_evidence_failure_blocks_after_three_returns(self):
        c = self.start_evidence()
        # Prior returns are queue state, separate from the ledger attempt cap
        # and focus/troubleshooter counters: this must be R65's own bound.
        c._update("T1", evidence_rewrites=3)
        result, _ = self.build_attempt(c)
        task = self.queue_task("T1")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["evidence_rewrites"], 3)
        self.assertTrue(any("mutation gate" in n for n in task["notes"]), task["notes"])
        self.assertTrue(any("blocked" in subject.lower() and result.reason in body
                            for subject, body in self.mails), self.mails)

    def test_ordinary_mutation_failure_keeps_tests_for_another_builder(self):
        c = self.start_evidence(evidence=False)
        old_commit = self.queue_task("T1")["tests_commit"]
        result, _ = self.build_attempt(c)
        self.assertTrue(result.survivors)
        task = self.queue_task("T1")
        self.assertNotIn("evidence", task)
        self.assertEqual(task["status"], "tests_ok")
        self.assertEqual(task["tests_commit"], old_commit)
        self.assertFalse(task.get("test_feedback"))
        self.assertEqual(task.get("evidence_rewrites", 0), 0)
        self.assertEqual(len(self.team.test_writer.prompts), 1)


if __name__ == "__main__":
    unittest.main()
