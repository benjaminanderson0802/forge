"""Codex review P1 findings on Layer 1B (integration fixes A and B).

A. A reviewer that is not ready (NotReady) or capped after a build was submitted must undo the attempt without
   counting it: no ledger attempt, no false claim, no failure signature. And a builder is not started while the
   reviewer it will need is unready.
B. An active finalization counts as runnable progress only if its next phase's needs are ready. A candidate that
   still awaits the merge reviewer needs the reviewer: while that is down, the capability email goes out and other
   tasks run.
"""
import json
import unittest
from pathlib import Path

from core.agents import FakeAgent
from core.bootstrap import NotReady
from core.finalize import Journal
from core.ledger import Ledger, Rejected
from tests.core.test_bootstrap import Harness
from tests.core.test_merge_pipeline import Crash, CrashingConductor, Pipeline

DOWN = lambda p, cwd: ("logged out", 0)  # noqa: E731 - a probe answer that is not usable evidence


class ReviewerNotReadyAfterSubmit(Harness):
    def build_until_review(self, exc):
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        before = c._task("T1")
        real_call = c._call

        def call(role, prompt, schema, cwd=None, needs=None, timeout_s=None):
            if role == "reviewer":  # Codex goes down between the builder's run and the review
                raise exc
            return real_call(role, prompt, schema, cwd=cwd, needs=needs, timeout_s=timeout_s)

        c._call = call
        return c, before

    def assert_undone_without_counting(self, c, before, result, expected):
        self.assertEqual(result, expected)
        led = Ledger(self.state)
        con = led.contracts()["T1"]
        self.assertEqual(con["status"], "open")
        self.assertEqual(con["attempts"], 0, "an unready reviewer must not cost the contract an attempt")
        self.assertEqual(led.false_claims("T1"), 0, "an unready reviewer is not a false 'done' claim")
        self.assertNotIn("fail", [e["action"] for e in led.events() if e["contract_id"] == "T1"])
        t = c._task("T1")
        self.assertEqual(t["status"], "tests_ok")
        self.assertEqual(t.get("fail_signatures"), before.get("fail_signatures"))
        self.assertEqual(t.get("fails_since"), before.get("fails_since"))
        self.assertNotIn("feat.py", self.branch_files())
        self.assertIsNone(Journal(self.state).load("T1"))

    def test_reviewer_not_ready_releases_the_claim_and_counts_nothing(self):
        c, before = self.build_until_review(NotReady({"codex": "probe failed"}))
        self.assert_undone_without_counting(c, before, c.step(), "not_ready")

    def test_repeated_reviewer_outages_never_park_the_contract(self):
        c, before = self.build_until_review(NotReady({"codex": "probe failed"}))
        for _ in range(8):  # more than max_attempts (6)
            self.assertEqual(c.step(), "not_ready")
        self.assertEqual(Ledger(self.state).contracts()["T1"]["status"], "open")
        self.assertEqual(Ledger(self.state).contracts()["T1"]["attempts"], 0)

    def test_the_next_attempt_after_an_outage_can_still_pass(self):
        c, before = self.build_until_review(NotReady({"codex": "probe failed"}))
        self.assertEqual(c.step(), "not_ready")
        del c._call  # Codex is back
        self.assertEqual(c.step(), "worked")
        self.assertEqual(Ledger(self.state).contracts()["T1"]["status"], "done")
        self.assertIn("feat.py", self.branch_files())


class BuilderWaitsForTheReviewer(Harness):
    def test_builder_not_started_while_the_reviewer_is_unready(self):
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.limits["cap_trouble_max"] = 0  # no Troubleshooter job for codex: the step only waits
        c.probes["codex"] = FakeAgent(DOWN, provider="codex")
        c._write("readiness_force.json", ["codex"])  # re-check codex now, like a reply from Ben would
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [], "the builder ran although its reviewer could not review")
        self.assertIn("codex", c._task("T1")["waiting_on"])
        self.assertNotIn(Ledger(self.state).contracts().get("T1", {}).get("status"), ("claimed", "submitted"))

    def test_capability_email_goes_out_when_the_only_build_waits_for_the_reviewer(self):
        """The capability-email hold uses the same predicate as task selection: a build task whose reviewer is
        down is not runnable progress, so Ben hears about it instead of the item staying held forever."""
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.limits["cap_trouble_max"] = 0  # no Troubleshooter job for codex: only Ben can fix it
        c.probes["codex"] = FakeAgent(DOWN, provider="codex")
        c._write("readiness_force.json", ["codex"])
        for _ in range(3):
            self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [])
        cap_mails = [s for s, b in self.mails if "codex" in (s + b).lower() and "Q-capability" in s]
        self.assertEqual(len(cap_mails), 1, [s for s, _ in self.mails])


class FinalizationAwaitingReview(Pipeline):
    """T1's merge candidate was judged and awaits the merge reviewer when Codex goes down."""

    def awaiting_review(self):
        c = self.start("T1", cls=CrashingConductor, crash_at="before:review")
        self.assertEqual(c.step(), "worked")  # T1 tests
        self.push_from_other("notes.txt", "from elsewhere\n")
        with self.assertRaises(Crash):
            c.step()  # T1 built and reviewed; its merge candidate judged; stopped before the merge review
        rec = Journal(self.state).load("T1")
        self.assertEqual((rec["phase"], rec["candidates"][-1]["state"]), ("candidate", "judged"))
        c2 = self.conductor()
        c2.limits["cap_trouble_max"] = 0  # no Troubleshooter job for codex: only Ben can fix it
        c2.probes["codex"] = FakeAgent(DOWN, provider="codex")
        c2._write("readiness_force.json", ["codex"])
        return c2

    def test_capability_email_goes_out_when_the_candidate_needs_the_reviewer(self):
        c = self.awaiting_review()
        for _ in range(3):
            self.assertEqual(c.step(), "not_ready")
        rec = Journal(self.state).load("T1")
        self.assertEqual((rec["phase"], rec["candidates"][-1]["state"]), ("candidate", "judged"))
        qs = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))
        items = [q for q in qs.values() if q.get("kind") == "capability" and q.get("capability") == "codex"]
        self.assertEqual(len(items), 1, qs)
        cap_mails = [s for s, b in self.mails if "codex" in (s + b).lower() and "Q-capability" in s]
        self.assertEqual(len(cap_mails), 1, [s for s, _ in self.mails])

    def test_other_tasks_run_while_the_candidate_waits_for_the_reviewer(self):
        c = self.awaiting_review()
        planned = []

        def planner(prompt, cwd):
            planned.append(prompt)
            (cwd / "plan.md").write_text("plan\n", encoding="utf-8")
            return json.dumps({"tasks": [self.task(id="T9", section="s" * 600)]}), 1

        c.team.planner = FakeAgent(planner, provider="claude")
        q = c._queue()
        q["tasks"].append({"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan it", "plan_file": "plan.md",
                           "status": "todo", "notes": [], "trouble_notes": [], "fail_signatures": [],
                           "fails_since": 0, "troubleshot": False, "troubleshoots": 0, "test_rejects": 0,
                           "plan_rejects": 0, "needs": []})
        c._save_queue(q)
        c.step()
        self.assertEqual(len(planned), 1, "step() kept selecting the finalization instead of other work")
        rec = Journal(self.state).load("T1")
        self.assertEqual((rec["phase"], rec["candidates"][-1]["state"]), ("candidate", "judged"))


class LedgerWithdraw(unittest.TestCase):
    """The ledger side of fix A: "withdraw" undoes an attempt without counting it, and only the core may do it."""
    ROLES = {"mgr": "manager", "exe": "executor", "aud": "auditor", "ci": "ci", "core": "core"}
    SHA = "1" * 40

    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp(prefix="forge-withdraw-"))
        (self.d / "roles.json").write_text(json.dumps(self.ROLES), encoding="utf-8")
        self.led = Ledger(self.d)
        self.do("create", "mgr", {"title": "t", "spec_ref": "s", "acceptance": "python -c pass",
                                  "files_in_scope": ["x.py"], "max_attempts": 2, "token_budget": 1000})

    def do(self, action, who, payload=None, pid=None):
        return self.led.apply({"proposal_id": pid or f"{action}-{len(self.led.events())}", "action": action,
                               "contract_id": "C1", "payload": payload or {}}, who)

    def submitted(self):
        self.do("claim", "exe")
        self.do("run_report", "core", {"run_id": "r1", "claim": "done", "commit": self.SHA, "changed": ["x.py"],
                                       "violations": [], "out_of_scope": []})
        self.do("submit", "exe", {"commit": self.SHA})
        self.do("test_run", "ci", {"run_id": "ci1", "commit": self.SHA, "passed": True})

    def test_withdraw_is_not_an_attempt_and_not_a_false_claim(self):
        for _ in range(5):  # more than max_attempts: an outage never parks the contract
            self.submitted()
            self.do("withdraw", "core")
            c = self.led.contracts()["C1"]
            self.assertEqual((c["status"], c["attempts"], c["commit"]), ("open", 0, None))
        self.assertEqual(self.led.false_claims("C1"), 0)
        self.assertFalse(self.led.reconcile())  # the cache replays from the log exactly

    def test_a_withdrawn_report_cannot_back_a_later_pass(self):
        self.submitted()
        self.do("withdraw", "core")
        self.do("claim", "exe")
        self.do("submit", "exe", {"commit": self.SHA})  # same commit, no new run report
        with self.assertRaises(Rejected):
            self.do("pass", "aud", {"run_id": "ci1"})

    def test_only_the_core_may_withdraw_and_only_an_open_attempt(self):
        self.submitted()
        for who in ("exe", "aud", "mgr"):
            with self.assertRaises(Rejected):
                self.do("withdraw", who)
        self.do("withdraw", "core")
        with self.assertRaises(Rejected):  # already open: nothing to withdraw
            self.do("withdraw", "core")


if __name__ == "__main__":
    unittest.main()
