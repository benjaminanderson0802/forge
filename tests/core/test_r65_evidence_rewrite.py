"""R65: failed evidence returns to the test writer, with bounded feedback."""
import json
import unittest
from unittest.mock import patch

from core.bootstrap import Capped, NotReady
from core.ledger import Ledger
from core.mutation import MutationResult
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

    def assert_interrupted_troubleshooter_returns_evidence(self, interruption, *, review=False):
        reasons = ["Assert the integer return type."] if review else None
        c = self.start_evidence(strong=review, review_reasons=reasons)
        # The next failure must invoke the real failure/handoff bookkeeping.
        c._update("T1", fails_since=1)
        with patch.object(c, "_troubleshoot", side_effect=interruption) as trouble:
            c.step()
        trouble.assert_called_once()
        self.assertEqual(trouble.call_args.args[0], "T1")
        failures = [e for e in Ledger(self.state).events()
                    if e["action"] == "fail" and e["contract_id"] == "T1"]
        self.assertEqual(len(failures), 1)
        payload = failures[0]["payload"]
        self.assertEqual(payload["gate"], "review" if review else "mutation")
        self.assertEqual(payload["verdict"], "fail" if review else "pass")
        self.assertEqual(payload["mutation"]["passed"], review)
        self.assertEqual(self.passes("T1"), [])
        task = self.queue_task("T1")
        with self.subTest(check="return to tests despite interruption"):
            self.assertEqual(task["status"], "todo")
        with self.subTest(check="feedback survives interruption"):
            self.assertTrue(self.feedback_text(task))
        with self.subTest(check="deferred builder troubleshooting preserved"):
            self.assertEqual(task.get("troubleshoot_pending"), {
                "reason": trouble.call_args.args[1],
                "output": trouble.call_args.args[2],
            })
        with self.subTest(check="accepted tests cleared"):
            self.assertFalse(task.get("tests_commit"))

        # Accept a rewrite, then service the deferred handoff before building.
        self.assertion = "self.assertTrue(feat.value() == 42)"
        self.assertEqual(c.step(), "worked")
        accepted = self.queue_task("T1")
        self.assertEqual(accepted["status"], "tests_ok")
        self.assertEqual(accepted.get("troubleshoot_pending"), task.get("troubleshoot_pending"))
        self.assertEqual(accepted["fails_since"], 2)
        self.assertEqual(accepted["fail_signatures"], task["fail_signatures"])
        builder_runs = len(self.team.builder.prompts)
        with patch.object(c, "_troubleshoot", wraps=c._troubleshoot) as resumed:
            self.assertEqual(c.step(), "worked")
        resumed.assert_called_once_with("T1", *trouble.call_args.args[1:])
        self.assertEqual(len(self.team.builder.prompts), builder_runs)
        self.assertEqual(len(self.team.troubleshooter.prompts), 1)
        self.assertFalse(self.queue_task("T1").get("troubleshoot_pending"))

    def test_mutation_failure_returns_to_tests_when_troubleshooter_capped(self):
        self.assert_interrupted_troubleshooter_returns_evidence(Capped("claude"))

    def test_mutation_failure_returns_to_tests_when_troubleshooter_not_ready(self):
        self.assert_interrupted_troubleshooter_returns_evidence(
            NotReady({"claude": "probe unavailable"}))

    def test_review_failure_returns_to_tests_when_troubleshooter_capped(self):
        self.assert_interrupted_troubleshooter_returns_evidence(Capped("claude"), review=True)

    def test_review_failure_returns_to_tests_when_troubleshooter_not_ready(self):
        self.assert_interrupted_troubleshooter_returns_evidence(
            NotReady({"claude": "probe unavailable"}), review=True)

    def test_identical_mutation_failures_across_rewrite_invoke_troubleshooter(self):
        c = self.start_evidence()
        first_result, _ = self.build_attempt(c)
        failed = self.queue_task("T1")
        self.assertEqual(failed["status"], "todo")
        self.assertEqual(failed["fails_since"], 1)
        self.assertEqual(len(failed["fail_signatures"]), 1)
        self.assertEqual(self.team.troubleshooter.prompts, [])

        # A real, accepted rewrite that still misses the same mutants.
        self.assertion = "self.assertTrue(feat.value() is not None)"
        self.assertEqual(c.step(), "worked")
        accepted = self.queue_task("T1")
        self.assertEqual(accepted["status"], "tests_ok")
        with self.subTest(check="failure count survives acceptance"):
            self.assertEqual(accepted["fails_since"], failed["fails_since"])
        with self.subTest(check="failure signatures survive acceptance"):
            self.assertEqual(accepted["fail_signatures"], failed["fail_signatures"])

        with patch.object(c, "_mutation_judge", wraps=c._mutation_judge) as judge, \
                patch.object(c, "_troubleshoot", wraps=c._troubleshoot) as trouble:
            self.assertEqual(c.step(), "worked")
        judge.assert_called_once()
        trouble.assert_called_once()
        self.assertEqual(trouble.call_args.args[0], "T1")
        self.assertEqual(len(self.team.builder.prompts), 2)
        self.assertEqual(len(self.team.troubleshooter.prompts), 1)
        failures = [e for e in Ledger(self.state).events()
                    if e["action"] == "fail" and e["contract_id"] == "T1"]
        self.assertEqual(len(failures), 2)
        self.assertTrue(first_result.survivors)
        for failure in failures:
            self.assertEqual(failure["payload"]["gate"], "mutation")
            self.assertEqual(failure["payload"]["mutation"]["survivors"],
                             first_result.as_dict()["survivors"])
        task = self.queue_task("T1")
        self.assertEqual(task["fail_signatures"], failed["fail_signatures"] * 2)
        self.assertEqual(task["troubleshoots"], 1)
        self.assertEqual(task["status"], "todo")

    def test_failure_recording_runtime_error_does_not_return_to_writer(self):
        c = self.start_evidence()
        accepted = self.queue_task("T1")
        real_apply = c._apply

        def apply(pid, action, cid, ident, payload=None):
            if action == "fail":
                raise RuntimeError("failure ledger write unavailable")
            return real_apply(pid, action, cid, ident, payload)

        with patch.object(c, "_apply", side_effect=apply) as ledger_apply:
            self.assertEqual(c.step(), "error")
        attempted_failures = [call for call in ledger_apply.call_args_list
                              if call.args[1] == "fail"]
        self.assertEqual(len(attempted_failures), 1)
        self.assertEqual(attempted_failures[0].args[2], "T1")
        self.assertEqual(attempted_failures[0].args[4]["gate"], "mutation")
        self.assertFalse(any(e["action"] == "fail" and e["contract_id"] == "T1"
                             for e in Ledger(self.state).events()))
        task = self.queue_task("T1")
        with self.subTest(check="status unchanged"):
            self.assertEqual(task["status"], accepted["status"])
        with self.subTest(check="accepted tests retained"):
            self.assertEqual(task["tests_commit"], accepted["tests_commit"])
        with self.subTest(check="no rewrite requested"):
            self.assertEqual(task.get("evidence_rewrites", 0), 0)
            self.assertFalse(task.get("test_feedback"))
        self.assertEqual(len(self.team.test_writer.prompts), 1)

    def assert_mutation_feedback_without_survivors(self, result):
        c = self.start_evidence()
        self.team.reviewer.script = lambda p, cwd: ('{"verdict":"pass","reasons":[]}', 1)
        # Keep the mutation gate real, including its zero-mutants rejection.
        with patch.object(c, "_run_mutation", return_value=result) as run:
            self.assertEqual(c.step(), "worked")
        run.assert_called_once()
        self.assertFalse(result.passed)
        self.assertEqual(result.survivors, [])
        failures = [e for e in Ledger(self.state).events()
                    if e["action"] == "fail" and e["contract_id"] == "T1"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["payload"]["gate"], "mutation")
        self.assertEqual(failures[0]["payload"]["mutation"], result.as_dict())
        task = self.queue_task("T1")
        self.assertEqual(task["status"], "todo")
        feedback = self.feedback_text(task)
        with self.subTest(check="nonempty feedback"):
            self.assertTrue(feedback)
        with self.subTest(check="mutation reason reaches writer"):
            self.assertTrue(result.reason)
            self.assertIn(result.reason, feedback)

    def test_incomplete_mutation_budget_without_survivors_reports_gate_reason(self):
        self.assert_mutation_feedback_without_survivors(MutationResult(
            total=1, killed=0, not_run=["feat.py:2:constant"], complete=False,
            passed=False, reason="incomplete: budget ran out, 1 of 1 mutants not run"))

    def test_zero_mutants_reports_gate_reason(self):
        self.assert_mutation_feedback_without_survivors(MutationResult(total=0, killed=0))

    def test_review_feedback_identifies_failed_gate_and_reasons(self):
        reasons = ["Assert the integer return type.", "Prove covers 1.1."]
        c = self.start_evidence(strong=True, review_reasons=reasons)
        self.build_attempt(c, mutation_passes=True, verdict="fail")
        feedback = self.feedback_text(self.queue_task("T1"))
        with self.subTest(check="gate identified"):
            self.assertIn("review failed", feedback.lower())
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(reason, feedback)

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
