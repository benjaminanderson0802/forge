"""Pure Challenger evidence, proof, and bounded-decision contracts (T2Bc)."""
import hashlib
import json
import unittest
from unittest.mock import Mock

from core import challenge


def summary(ran=5, ending="OK"):
    noun = "test" if ran == 1 else "tests"
    return f".....\n----------------------------------------------------------------------\nRan {ran} {noun} in 0.003s\n\n{ending}\n"


class RouteEvidenceTests(unittest.TestCase):
    def test_normalization(self):
        for text in ("Use  Datetime.", "use datetime", " \tUSE\nDatetime.;:,! ",
                     '"Use Datetime."', "`Use Datetime.`", "'Use Datetime.'"):
            with self.subTest(text=text):
                self.assertEqual(challenge.norm_route(text), "use datetime")
        self.assertEqual(challenge.norm_route("Straße"), "strasse")
        self.assertEqual(challenge.norm_route(" \n\t"), "")

    def test_two_fresh_routes_and_error_are_evidence(self):
        answer = {"tried": ["Use dateutil", "Parse manually"], "error": " ValueError "}
        self.assertIsNone(challenge.stands_evidence(answer, ["Use datetime"]))

    def test_copies_duplicates_and_non_routes_do_not_count(self):
        for tried in (["USE DATETIME", "use  datetime.", "Use dateutil"],
                      ["Use dateutil"],
                      ["Use dateutil", "USE  DATEUTIL."],
                      ["Use dateutil", "", "  ", None, 42],
                      [], ["", "  "]):
            with self.subTest(tried=tried):
                self.assertEqual(
                    challenge.stands_evidence({"tried": tried, "error": "failed"},
                                              [" Use\tDatetime. "]),
                    "stands without evidence: fewer than 2 distinct routes not copied from the claimant")

    def test_missing_tried_is_not_evidence(self):
        self.assertEqual(challenge.stands_evidence({"error": "failed"}, []),
                         "stands without evidence: fewer than 2 distinct routes not copied from the claimant")

    def test_error_must_be_a_nonempty_string(self):
        for fields in ({}, {"error": ""}, {"error": " \n\t"},
                       {"error": None}, {"error": 1}):
            with self.subTest(fields=fields):
                self.assertEqual(challenge.stands_evidence(
                    {"tried": ["route a", "route b"], **fields}, []),
                    "stands without evidence: no error")


class UnittestSummaryTests(unittest.TestCase):
    def test_real_summary_shapes(self):
        cases = [
            (summary(1), {"ran": 1, "failures": 0, "errors": 0}),
            (summary(5, "FAILED (failures=2, errors=1)"),
             {"ran": 5, "failures": 2, "errors": 1}),
            (summary(5, "FAILED (errors=1, skipped=3)"),
             {"ran": 5, "failures": 0, "errors": 1}),
            ("FAILED (failures=2)\n", {"ran": None, "failures": 2, "errors": 0}),
            ("launcher failed\n", {"ran": None, "failures": 0, "errors": 0}),
            (summary(0), {"ran": 0, "failures": 0, "errors": 0}),
        ]
        for output, expected in cases:
            with self.subTest(output=output):
                self.assertEqual(challenge.parse_unittest(output), expected)

    def test_last_anchored_summary_wins(self):
        output = (summary(9, "FAILED (failures=3, errors=2)")
                  + summary(4, "FAILED (errors=1, skipped=3)")
                  + "quoted Ran 99 tests in 1s\nquoted FAILED (failures=99)\n")
        self.assertEqual(challenge.parse_unittest(output),
                         {"ran": 4, "failures": 0, "errors": 1})

    def test_incomplete_summary_has_no_failure_totals(self):
        self.assertEqual(challenge.parse_unittest(summary(5, "FAILED (failures=2")),
                         {"ran": 5, "failures": 0, "errors": 0})

    def test_pass_requires_success_tests_and_no_timeout(self):
        for code, output, timed_out, expected in (
            (0, summary(1), False, True),
            (0, summary(0), False, False),
            (0, "OK\n", False, False),
            (0, summary(5), True, False),
            (1, summary(5), False, False),
        ):
            with self.subTest(code=code, output=output, timed_out=timed_out):
                self.assertIs(challenge.run_passed(code, output, timed_out), expected)


class PatchProofTests(unittest.TestCase):
    def verify(self, patched_result, baseline_result):
        patched = Mock(return_value=patched_result)
        baseline = Mock(return_value=baseline_result)
        result = challenge.verify_patch(["core/route.py"], ["core/*.py"],
                                        ["tests/core/test_route.py"], patched, baseline)
        patched.assert_called_once_with()
        self.assertLessEqual(baseline.call_count, 1)
        return result, baseline

    def test_scope_rejections_never_run_tests(self):
        cases = [
            ([], "no changes"),
            (["./tests/core/test_a.py", "tests\\core\\test_b.py"],
             "touched test files: tests/core/test_a.py, tests/core/test_b.py"),
            (["./docs/a.md", "docs\\b.md"], "out of scope: docs/a.md, docs/b.md"),
            (["tests/core/test_a.py", "outside.py"],
             "touched test files: tests/core/test_a.py"),
        ]
        for changed, reason in cases:
            with self.subTest(changed=changed):
                patched, baseline = Mock(), Mock()
                tests = ["tests\\core\\test_a.py", "./tests/core/test_b.py"]
                self.assertEqual(challenge.scope_problem(changed, ["core/*.py"], tests), reason)
                self.assertEqual(challenge.verify_patch(changed, ["core/*.py"], tests,
                                                        patched, baseline), (False, reason))
                patched.assert_not_called()
                baseline.assert_not_called()

    def test_scope_normalizes_patterns_and_accepts_any_matching_pattern(self):
        self.assertIsNone(challenge.scope_problem(
            ["./core/route.py", "core\\other.py", "README.md"],
            ["./core\\*.py", "README.md"], ["tests/test_route.py"]))

    def test_patched_pass_short_circuits_baseline(self):
        result, baseline = self.verify((0, summary(5), False), None)
        self.assertEqual(result, (True, "tests pass (Ran 5)"))
        baseline.assert_not_called()

    def test_progress_requires_fewer_failures_and_at_least_as_many_tests(self):
        for patched_count, patched_ran, baseline_count, expected in (
            (1, 5, 3, True), (1, 6, 3, True),
            (1, 5, 1, False), (2, 5, 1, False), (1, 4, 3, False),
        ):
            with self.subTest(patched=patched_count, ran=patched_ran, baseline=baseline_count):
                result, baseline = self.verify(
                    (1, summary(patched_ran, f"FAILED (errors={patched_count})"), False),
                    (1, summary(5, f"FAILED (failures={baseline_count - 1}, errors=1)"), False))
                prefix = "progress" if expected else "no progress"
                self.assertEqual(result, (expected,
                    f"{prefix}: failures+errors {baseline_count} -> {patched_count}"))
                baseline.assert_called_once_with()

    def test_unusable_patched_runs_do_not_run_baseline(self):
        for patched_result in ((1, summary(5, "FAILED (errors=1)"), True),
                               (0, summary(5), True), (0, summary(0), False),
                               (1, "launcher failed", False)):
            with self.subTest(patched_result=patched_result):
                result, baseline = self.verify(patched_result, None)
                self.assertIs(result[0], False)
                self.assertIsInstance(result[1], str)
                self.assertTrue(result[1])
                baseline.assert_not_called()

    def test_unusable_baseline_cannot_prove_progress(self):
        for baseline_result in ((1, summary(5, "FAILED (errors=3)"), True),
                                (0, summary(0), False), (1, "no test summary", False)):
            with self.subTest(baseline_result=baseline_result):
                result, baseline = self.verify(
                    (1, summary(5, "FAILED (errors=1)"), False), baseline_result)
                self.assertEqual(result, (False, "baseline run unusable"))
                baseline.assert_called_once_with()

    def test_missing_or_malformed_failure_summary_is_not_zero_failure_progress(self):
        # A Ran line alone cannot turn a crashed/truncated nonzero run into proof.
        for ending in ("", "OK", "FAILED", "FAILED (failures=1",
                       "FAILED ()", "FAILED (skipped=3)",
                       "FAILED (failures=oops)", "FAILED (errors=?)"):
            with self.subTest(ending=ending):
                result, baseline = self.verify(
                    (1, summary(5, ending), False),
                    (1, summary(5, "FAILED (failures=2, errors=1)"), False))
                self.assertIs(result[0], False,
                              "Missing failure evidence must not be credited as progress")
                self.assertIsInstance(result[1], str)
                self.assertTrue(result[1])


class CapabilityProofTests(unittest.TestCase):
    def test_only_literal_true_in_a_dict_verifies(self):
        for entry, expected in (({"ok": True, "detail": "connected"}, True),
                                ({"ok": False, "detail": "offline"}, False),
                                ({"ok": 1, "detail": "not boolean"}, False),
                                ({"ok": "true"}, False), ({}, False),
                                (True, False), ([], False)):
            with self.subTest(entry=entry):
                check = Mock(return_value=entry)
                result = challenge.verify_capability("browser", check)
                self.assertIs(result[0], expected)
                self.assertIsInstance(result[1], str)
                if entry == {"ok": False, "detail": "offline"}:
                    self.assertEqual(result, (False, "browser still broken: offline"))
                check.assert_called_once_with("browser")

    def test_missing_check(self):
        check = Mock(return_value=None)
        self.assertEqual(challenge.verify_capability("browser", check),
                         (False, "no check exists for browser"))
        check.assert_called_once_with("browser")

    def test_invalid_names_do_not_call_check(self):
        for name in (None, 12, [], "", "Browser", "two words", "../git", "a" * 41):
            with self.subTest(name=name):
                check = Mock()
                self.assertEqual(challenge.verify_capability(name, check),
                                 (False, "no capability named"))
                check.assert_not_called()

    def test_valid_name_boundaries(self):
        for name in ("a", "python_libs", "n8n", "a" * 40):
            with self.subTest(name=name):
                check = Mock(return_value={"ok": True})
                self.assertIs(challenge.verify_capability(name, check)[0], True)
                check.assert_called_once_with(name)


class RunJudgmentTests(unittest.TestCase):
    def assert_shape(self, result):
        self.assertEqual(set(result), {"outcome", "verdict", "route", "tried",
                                       "error", "proof", "detail"})

    def test_outcome_vocabulary(self):
        self.assertEqual(challenge.RUN_OUTCOMES, (
            "verified_overturn", "unverified_overturn", "stands",
            "stands_no_evidence", "unusable", "interrupted"))

    def test_overturn_requires_plain_code_verification(self):
        for proof in ("patch", "capability"):
            for verified in (True, False):
                with self.subTest(proof=proof, verified=verified):
                    verify = Mock(return_value=(verified, "plain-code detail"))
                    result = challenge.judge_run(True, {
                        "verdict": "overturned", "proof": proof, "route": "  new route  ",
                        "tried": ["first", None, 7, "second"], "error": "old error",
                    }, ["old route"], verify)
                    self.assertEqual(result, {
                        "outcome": "verified_overturn" if verified else "unverified_overturn",
                        "verdict": "overturned", "route": "new route",
                        "tried": ["first", "second"], "error": "old error",
                        "proof": proof, "detail": "plain-code detail",
                    })
                    verify.assert_called_once_with(proof)

    def test_unknown_proof_is_not_sent_to_verifier(self):
        for fields in ({}, {"proof": "agent says so"}, {"proof": None}):
            with self.subTest(fields=fields):
                verify = Mock()
                result = challenge.judge_run(True, {"verdict": "overturned", **fields}, [], verify)
                self.assert_shape(result)
                self.assertEqual(result["outcome"], "unverified_overturn")
                self.assertEqual(result["detail"], "unknown proof")
                verify.assert_not_called()

    def test_stands_requires_evidence_without_verification_callback(self):
        for tried, expected in ((["fresh a", "fresh b"], "stands"),
                                (["old route", "fresh a"], "stands_no_evidence")):
            with self.subTest(tried=tried):
                verify = Mock()
                answer = {"verdict": "stands", "tried": tried, "error": "still fails"}
                result = challenge.judge_run(True, answer, ["old route"], verify)
                self.assert_shape(result)
                self.assertEqual(result["outcome"], expected)
                self.assertEqual(result["verdict"], "stands")
                self.assertEqual(result["tried"], tried)
                self.assertEqual(result["error"], "still fails")
                if expected == "stands_no_evidence":
                    self.assertEqual(result["detail"], challenge.stands_evidence(answer, ["old route"]))
                verify.assert_not_called()

    def test_unusable_answers_do_not_verify(self):
        for ok, answer in ((False, {"verdict": "overturned", "proof": "patch"}),
                           (True, None), (True, []), (True, "stands"),
                           (True, {}), (True, {"verdict": "maybe"})):
            with self.subTest(ok=ok, answer=answer):
                verify = Mock()
                result = challenge.judge_run(ok, answer, [], verify)
                self.assert_shape(result)
                self.assertEqual(result["outcome"], "unusable")
                verify.assert_not_called()

    def test_route_is_stripped_bounded_and_defaults_to_empty(self):
        for fields, expected in (({}, ""), ({"route": None}, ""),
                                 ({"route": 123}, ""),
                                 ({"route": "  " + "x" * 2001 + "  "}, "x" * 2000)):
            with self.subTest(fields=fields):
                result = challenge.judge_run(True, {"verdict": "overturned", **fields}, [], Mock())
                self.assertEqual(result["route"], expected)
                self.assertEqual(result["tried"], [])

    def test_verification_exception_propagates(self):
        failure = RuntimeError("check failed to run")
        verify = Mock(side_effect=failure)
        with self.assertRaises(RuntimeError) as caught:
            challenge.judge_run(True, {"verdict": "overturned", "proof": "patch"}, [], verify)
        self.assertIs(caught.exception, failure)
        verify.assert_called_once_with("patch")


class DecisionAndLedgerTests(unittest.TestCase):
    def test_decision_bounds_and_precedence(self):
        for outcomes, limit, expected in (
            ([], 2, None), (["unusable"], 2, None),
            (["unusable", "unverified_overturn"], 2, "unconfirmed"),
            (["stands_no_evidence", "stands"], 2, "stands"),
            (["unusable", "verified_overturn"], 2, "overturned"),
            (["unusable"], 0, "unconfirmed"), (["unusable"], -2, "unconfirmed"),
            (["interrupted", "unusable"], 2, "unconfirmed"),
            (["stands_no_evidence"], 1, "unconfirmed"),
            (["unusable"], "2", None),
            (["stands", "verified_overturn"], 1, "overturned"),
            (["verified_overturn", "stands"], 5, "overturned"),
        ):
            with self.subTest(outcomes=outcomes, limit=limit):
                self.assertEqual(challenge.decide(outcomes, limit), expected)

    def test_claim_key_exact_hash_and_stability(self):
        claim = {"why": "blocked", "nested": {"b": 2, "a": 1}}
        reordered = {"nested": {"a": 1, "b": 2}, "why": "blocked"}
        expected = hashlib.sha256(json.dumps(
            ["blocked", "task-1", claim], sort_keys=True, default=str).encode("utf-8")).hexdigest()
        key = challenge.claim_key("blocked", "task-1", claim)
        self.assertEqual(key, expected)
        self.assertEqual(key, challenge.claim_key("blocked", "task-1", reordered))
        self.assertNotEqual(key, challenge.claim_key("exhausted", "task-1", claim))
        self.assertNotEqual(key, challenge.claim_key("blocked", "task-2", claim))
        self.assertNotEqual(key, challenge.claim_key("blocked", "task-1", {"why": "different"}))

    def test_claim_key_uses_default_str(self):
        class Subject:
            def __str__(self):
                return "subject-as-text"
        self.assertEqual(challenge.claim_key("blocked", Subject(), {"why": "x"}),
                         challenge.claim_key("blocked", "subject-as-text", {"why": "x"}))

    def test_ledger_payload_selects_first_supporting_run(self):
        runs = [
            {"run_id": "r1", "outcome": "unverified_overturn", "route": "unsupported", "proof": "patch"},
            {"run_id": "r2", "outcome": "stands_no_evidence", "route": "no evidence", "proof": None},
            {"run_id": "r3", "outcome": "stands", "route": "still broken", "proof": "ignored"},
            {"run_id": "r4", "outcome": "verified_overturn", "route": "x" * 2001, "proof": "capability"},
            {"run_id": "r5", "outcome": "verified_overturn", "route": "later proof", "proof": "patch"},
            {"run_id": "r6", "outcome": "stands", "route": "later stands", "proof": None},
        ]
        for verdict, proof, route in (("overturned", "capability", "x" * 2000),
                                      ("stands", None, "still broken"),
                                      ("unconfirmed", None, "")):
            with self.subTest(verdict=verdict):
                self.assertEqual(challenge.ledger_payload("blocked", "builder", verdict, runs), {
                    "target": "blocked", "claimant": "builder", "verdict": verdict,
                    "proof": proof, "route": route,
                    "run_ids": ["r1", "r2", "r3", "r4", "r5", "r6"],
                    "outcomes": [run["outcome"] for run in runs],
                })

    def test_stands_ledger_route_is_also_bounded(self):
        self.assertEqual(challenge.ledger_payload("exhausted", "reviewer", "stands", [
            {"run_id": "r1", "outcome": "stands", "route": "s" * 2001, "proof": None},
        ]), {"target": "exhausted", "claimant": "reviewer", "verdict": "stands",
             "proof": None, "route": "s" * 2000, "run_ids": ["r1"], "outcomes": ["stands"]})

    def test_empty_unconfirmed_ledger_payload(self):
        self.assertEqual(challenge.ledger_payload("blocked", "builder", "unconfirmed", []), {
            "target": "blocked", "claimant": "builder", "verdict": "unconfirmed",
            "proof": None, "route": "", "run_ids": [], "outcomes": [],
        })


if __name__ == "__main__":
    unittest.main()
