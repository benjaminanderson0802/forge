"""Layer-1 integration: P1B1 (weak tests, mutation judge), P1B2 (readiness, gating, routing, blocker claims) and
P1B3 (role files, per-task worktrees, crash-safe finalization) working together."""
import json
import unittest

from core.finalize import Journal
from core.ledger import Ledger
from tests.core.test_bootstrap import HEALTHY_CHECKS, Harness, git
from tests.core.test_merge_pipeline import CrashingConductor, Crash, Pipeline

CLAIM = {"status": "blocked", "summary": "cannot proceed", "tried": ["route one", "route two"],
         "error": "docker: command not found", "capability": "docker", "meanwhile": "write the docs"}


class BlockerClaimInTaskWorktreeTests(Harness):
    """T1B2e's blocker review sees the attempt where it happened: the task worktree (T1B3b)."""

    def partial_then_blocked(self, prompt, cwd):
        (cwd / "feat.py").write_text("PARTIAL_WORK = 'half done'\n", encoding="utf-8")
        return json.dumps(CLAIM), 1

    def test_claim_review_sees_task_worktree_changes_and_role_text(self):
        (self.repo / "agents").mkdir(exist_ok=True)
        (self.repo / "agents" / "reviewer.md").write_text("You are the REVIEWER (read-only).\nMARKER-RV-77\n",
                                                          encoding="utf-8")
        seen = []

        def reviewer(prompt, cwd):
            seen.append(cwd)
            return '{"verdict":"fail","reasons":["no real attempt"]}', 1

        checks = dict(HEALTHY_CHECKS, docker=lambda: (False, "docker not installed or not on PATH"))
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.partial_then_blocked,
                              "reviewer": reviewer}, checks=checks)
        self.assertEqual(c.step(), "worked")
        layer_tip = git(c.wt, "rev-parse", "HEAD")
        c.step()
        prompts = c.team.reviewer.prompts
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertTrue(prompt.startswith("You are the REVIEWER (read-only).\nMARKER-RV-77\n\n"), prompt[:200])
        self.assertIn("BLOCKER CLAIM:", prompt)
        changes = prompt.split("THE ATTEMPT'S CHANGES:", 1)[1]
        self.assertIn("PARTIAL_WORK = 'half done'", changes)
        self.assertNotIn("(no changes)", changes.split("Answer with JSON", 1)[0])
        self.assertEqual(seen, [c.work / "tasks" / "T1"])
        t = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))["tasks"][0]
        self.assertTrue(any(n.startswith("blocker rejected by reviewer: no real attempt") for n in t["notes"]))
        # the layer never saw the attempt, and the task worktree was reset to its base
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), layer_tip)
        self.assertFalse((c.wt / "feat.py").exists())
        self.assertFalse((c.work / "tasks" / "T1" / "feat.py").exists())


class FinalizationReadinessTests(Pipeline):
    """T1B3e's finalizer runs only after P1B2's readiness refresh, and pushes only with github ready."""

    def crashed_after_journal_begin(self):
        c = self.start("T1", cls=CrashingConductor, crash_at="after:journal-begin")
        self.assertEqual(c.step(), "worked")  # tests
        with self.assertRaises(Crash):
            c.step()  # build, reviewed, journal begun, then the process dies
        self.assertIsNotNone(Journal(self.state).load("T1"))
        return self.task_commit("T1")

    def test_active_finalization_waits_for_github_and_readiness_runs_first(self):
        s = self.crashed_after_journal_begin()
        calls = []
        c = self.conductor()
        c.checks = dict(c.checks, github=lambda: (calls.append("github") or (False, "gh: not logged in")))
        before = self.origin_tip()
        r = c.step()
        self.assertEqual(r, "not_ready")
        self.assertEqual(calls, ["github"])  # refreshed this cycle, before anything else
        self.assertEqual(self.origin_tip(), before)  # nothing pushed
        self.assertFalse(self.origin_contains(s))
        self.assertEqual(Ledger(self.state).completion("T1"), None)
        self.assertEqual(Journal(self.state).load("T1")["status"], "active")
        self.check_invariants("T1")
        c.checks = dict(c.checks, github=lambda: (True, "ok"))
        self.run_until_settled(c, ["T1"])
        self.assertEqual(self.queue_task("T1")["status"], "done")
        self.assertTrue(self.origin_contains(s))
        self.check_invariants("T1")

    def test_pass_evidence_after_crash_keeps_mutation_evidence(self):
        s = self.crashed_after_journal_begin()
        c = self.conductor()
        self.run_until_settled(c, ["T1"])
        comp = Ledger(self.state).completion("T1")
        self.assertIsNotNone(comp)
        payload = comp["payload"]
        self.assertEqual(payload["task_commit"], s)
        self.assertEqual(payload["verdict"], "pass")
        self.assertTrue(payload["mutation"]["passed"])
        self.assertTrue(payload["mutation"]["complete"])
        self.assertEqual(payload["mutation"]["survivors"], [])

    def test_waiting_finalization_is_progress_so_items_stay_held(self):
        """D-023: an instant email only when Ben alone can unblock all progress. A finalization that can run
        is progress, so an unrelated broken capability stays a held item."""
        self.crashed_after_journal_begin()
        c = self.conductor(crash_at=None)
        c.checks = dict(c.checks, docker=lambda: (False, "docker not installed or not on PATH"))
        c.step()
        qs = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))
        items = [q for q in qs.values() if q.get("kind") == "capability"]
        self.assertEqual([q["capability"] for q in items], ["docker"])
        self.assertFalse(any("docker" in s for s, _ in self.mails), self.mails)


if __name__ == "__main__":
    unittest.main()
