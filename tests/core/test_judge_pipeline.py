"""T1B1e: mutation judge in Stage B; survivors shown to Reviewer and Builder; verdicts recorded in the ledger."""
import hashlib
import json
import unittest
from unittest.mock import patch

import core.bootstrap as bootstrap
from tests.core.test_bootstrap import Harness, git

CAPS = {"claude_daily_token_cap": 10 ** 9, "codex_daily_token_cap": 10 ** 9}
DOUBLE = "def double(x):\n    return x * 2\n"
WEAK_TESTS = ("import unittest\nimport feat\n"
              "class T(unittest.TestCase):\n"
              " def test_int(self): self.assertIsInstance(feat.double(2), int)\n")
SURVIVOR = "feat.py:2:15:constant:2->3"


class JudgePipelineTests(Harness):
    def queue_task(self):
        return json.loads((self.state / "queue.json").read_text(encoding="utf-8"))["tasks"][0]

    def events(self, action=None):
        path = self.state / "ledger" / "events.jsonl"
        evs = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [e for e in evs if action is None or e["action"] == action]

    def weak_writer(self, prompt, cwd):
        p = cwd / "tests/core/test_feat.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(WEAK_TESTS, encoding="utf-8")
        return '{"files":["tests/core/test_feat.py"]}', 1

    def build_double(self, prompt, cwd):
        (cwd / "feat.py").write_text(DOUBLE, encoding="utf-8")
        return '{"status":"done"}', 1

    def reviewer(self, verdict="pass", reasons=None, seen=None):
        def run(prompt, cwd):
            if seen is not None:
                seen.append({"feat": (cwd / "feat.py").read_text(encoding="utf-8") if (cwd / "feat.py").exists()
                             else None, "status": git(cwd, "status", "--porcelain", "-uall")})
            return json.dumps({"verdict": verdict, "reasons": reasons or []}), 1
        return run

    def weak_conductor(self, limits=None, reviewer=None):
        c = self.init(agents={"test_writer": self.weak_writer, "builder": self.build_double,
                              "reviewer": reviewer or self.reviewer()},
                      limits=dict(CAPS, **(limits or {})))
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.queue_task()["status"], "tests_ok")
        return c

    # happy path
    def test_happy_path_done_with_ledger_verdict_and_mutation(self):
        seen = []
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature,
                              "reviewer": self.reviewer(reasons=["looks right"], seen=seen)})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertEqual(t["status"], "done")
        passes = self.events("pass")
        self.assertEqual(len(passes), 1)
        payload = passes[0]["payload"]
        self.assertEqual(payload["verdict"], "pass")
        self.assertEqual(payload["reasons"], ["looks right"])
        self.assertTrue(payload["mutation"]["passed"])
        self.assertTrue(payload["mutation"]["complete"])
        self.assertGreaterEqual(payload["mutation"]["total"], 1)
        self.assertEqual(payload["mutation"]["survivors"], [])
        self.assertTrue(payload["run_id"].endswith("-ci"))
        # reviewer saw the builder's text and a clean worktree, i.e. right after mutation
        self.assertEqual(seen, [{"feat": "VALUE = 42\n", "status": ""}])
        prompt = c.team.reviewer.prompts[-1]
        self.assertIn("MUTATION EVIDENCE:", prompt)
        self.assertIn("- survivors: none", prompt)
        self.assertIn("complete: yes", prompt)
        self.assertIn("DIFF:", prompt)
        # successful path: worktree holds the builder's text, clean
        self.assertEqual((c.wt / "feat.py").read_text(encoding="utf-8"), "VALUE = 42\n")
        self.assertEqual(git(c.wt, "status", "--porcelain", "-uall"), "")

    # weak tests: survivors
    def test_survivors_fail_attempt_even_with_reviewer_pass(self):
        seen = []
        c = self.weak_conductor(reviewer=self.reviewer(reasons=["fine by me"], seen=seen))
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertNotEqual(t["status"], "done")
        self.assertEqual(t["status"], "tests_ok")
        # reviewer was still called, after mutation restored the builder's text
        self.assertEqual(len(c.team.reviewer.prompts), 1)
        self.assertEqual(seen, [{"feat": DOUBLE, "status": ""}])
        prompt = c.team.reviewer.prompts[0]
        self.assertIn("MUTATION EVIDENCE:", prompt)
        self.assertIn(f"- {SURVIVOR}: 2 -> 3 survived (line 2)", prompt)
        self.assertIn("complete: yes", prompt)
        self.assertIn("0.80", prompt)
        # attempt failed with the mutation gate reason and signature
        self.assertTrue(t["notes"][-1].startswith("mutation gate: "), t["notes"])
        self.assertIn("survivors: " + SURVIVOR, t["notes"][-1])
        expected_sig = hashlib.sha256(("mutation:" + SURVIVOR).encode()).hexdigest()
        self.assertEqual(t["fail_signatures"][-1], expected_sig)
        self.assertEqual(t["review_feedback"][0],
                         f"surviving mutant {SURVIVOR}: 2 -> 3 at line 2: add or strengthen code so the tests catch it")
        self.assertIn("fine by me", t["review_feedback"])
        # ledger fail event records the reviewer verdict and the survivors
        fails = [e for e in self.events("fail") if e["payload"].get("gate")]
        self.assertEqual(len(fails), 1)
        payload = fails[0]["payload"]
        self.assertEqual(payload["gate"], "mutation")
        self.assertEqual(payload["verdict"], "pass")
        self.assertEqual(payload["reasons"], ["fine by me"])
        self.assertFalse(payload["mutation"]["passed"])
        self.assertIn(SURVIVOR, [s["id"] for s in payload["mutation"]["survivors"]])
        self.assertEqual(self.events("pass"), [])
        # next builder prompt carries the survivor
        c.step()
        self.assertEqual(len(c.team.builder.prompts), 2)
        nxt = c.team.builder.prompts[-1]
        self.assertIn("REVIEW FEEDBACK:", nxt)
        self.assertIn(SURVIVOR, nxt.split("REVIEW FEEDBACK:", 1)[1])
        self.assertNotEqual(self.queue_task()["status"], "done")

    def test_survivor_listing_goes_to_troubleshooter(self):
        c = self.weak_conductor()
        c.step(); c.step()  # two identical mutation failures -> troubleshooter
        self.assertTrue(c.team.troubleshooter.prompts)
        tail = c.team.troubleshooter.prompts[-1].split("LAST JUDGE OUTPUT:", 1)[1]
        self.assertIn(f"- {SURVIVOR}: 2 -> 3 survived (line 2)", tail)

    def test_budget_zero_is_incomplete_and_never_done(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature},
                      limits=dict(CAPS, mutation_budget_s=0))
        c.step()
        for _ in range(3):
            c.step()
            t = self.queue_task()
            self.assertNotEqual(t["status"], "done")
        t = self.queue_task()
        self.assertIn("mutation-incomplete", t["fail_signatures"])
        self.assertTrue(any(n.startswith("mutation gate: incomplete") for n in t["notes"]), t["notes"])
        self.assertIn("complete: no", c.team.reviewer.prompts[0])
        fails = [e for e in self.events("fail") if e["payload"].get("gate") == "mutation"]
        self.assertTrue(fails)
        self.assertFalse(fails[0]["payload"]["mutation"]["complete"])
        self.assertEqual(self.events("pass"), [])

    def test_incomplete_with_mutation_min_zero_still_fails(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature},
                      limits=dict(CAPS, mutation_budget_s=0, mutation_min=0.0))
        c.step(); c.step()
        t = self.queue_task()
        self.assertNotEqual(t["status"], "done")
        self.assertEqual(t["fail_signatures"][-1], "mutation-incomplete")

    def test_reviewer_fail_verdict_recorded_in_ledger(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature,
                              "reviewer": self.reviewer("fail", ["missing edge case", "no docs"])})
        c.step(); c.step()
        t = self.queue_task()
        self.assertNotEqual(t["status"], "done")
        self.assertEqual(t["review_feedback"], ["missing edge case", "no docs"])
        self.assertEqual(t["fail_signatures"][-1], "review:missing edge case|no docs")
        fails = [e for e in self.events("fail") if e["payload"].get("gate")]
        self.assertEqual(len(fails), 1)
        payload = fails[0]["payload"]
        self.assertEqual(payload["gate"], "review")
        self.assertEqual(payload["verdict"], "fail")
        self.assertEqual(payload["reasons"], ["missing edge case", "no docs"])
        self.assertTrue(payload["mutation"]["passed"])

    def test_reviewer_unusable_output_recorded(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature,
                              "reviewer": lambda p, cwd: ("not json", 1)})
        c.step(); c.step()
        t = self.queue_task()
        self.assertTrue(t["notes"][-1].startswith("reviewer output unusable: "))
        fails = [e for e in self.events("fail") if e["payload"].get("gate")]
        self.assertEqual(fails[0]["payload"]["gate"], "reviewer_error")
        self.assertIsNone(fails[0]["payload"]["verdict"])
        self.assertIn("mutation", fails[0]["payload"])

    def test_reviewer_reject_keeps_survivors_in_feedback_when_gate_passes(self):
        c = self.weak_conductor(limits={"mutation_min": 0.0},
                                reviewer=self.reviewer("fail", ["tests too weak"]))
        c.step()
        t = self.queue_task()
        self.assertNotEqual(t["status"], "done")
        self.assertIn("tests too weak", t["review_feedback"])
        self.assertTrue(any(SURVIVOR in x for x in t["review_feedback"]), t["review_feedback"])
        payload = [e for e in self.events("fail") if e["payload"].get("gate")][0]["payload"]
        self.assertEqual(payload["gate"], "review")
        self.assertTrue(payload["mutation"]["passed"])
        self.assertEqual([s["id"] for s in payload["mutation"]["survivors"]], [SURVIVOR])
        c.step()
        self.assertIn(SURVIVOR, c.team.builder.prompts[-1].split("REVIEW FEEDBACK:", 1)[1])

    def test_mutation_min_zero_lets_complete_survivor_pass(self):
        c = self.weak_conductor(limits={"mutation_min": 0.0})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.queue_task()["status"], "done")
        payload = self.events("pass")[0]["payload"]
        self.assertTrue(payload["mutation"]["passed"])
        self.assertTrue(payload["mutation"]["complete"])
        self.assertEqual([s["id"] for s in payload["mutation"]["survivors"]], [SURVIVOR])
        self.assertEqual((c.wt / "feat.py").read_text(encoding="utf-8"), DOUBLE)
        self.assertEqual(git(c.wt, "status", "--porcelain", "-uall"), "")

    def test_judge_failure_skips_mutation_and_reviewer(self):
        def bad(prompt, cwd):
            (cwd / "feat.py").write_text("VALUE = 41\n", encoding="utf-8")
            return '{"status":"done"}', 1
        c = self.init(agents={"test_writer": self.write_tests, "builder": bad})
        c.step()
        with patch.object(bootstrap, "run_mutation") as rm:
            c.step()
        rm.assert_not_called()
        self.assertEqual(c.team.reviewer.prompts, [])
        self.assertTrue(self.queue_task()["notes"][-1].startswith("judge failed"))

    def test_mutation_arguments(self):
        calls = []
        real = bootstrap.run_mutation

        def spy(root, changed, argv, **kw):
            calls.append((root, changed, argv, kw, git(root, "rev-parse", "HEAD"),
                          (root / "feat.py").read_text(encoding="utf-8")))
            return real(root, changed, argv, **kw)

        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature},
                      limits=dict(CAPS, mutation_min=0.5, mutation_budget_s=100, test_timeout_s=50))
        c.step()
        with patch.object(bootstrap, "run_mutation", side_effect=spy):
            c.step()
        self.assertEqual(len(calls), 1)
        root, changed, argv, kw, head, feat = calls[0]
        # T1B3b: the mutation judge runs where the code is, a throwaway worktree at exactly the task commit S,
        # never the layer checkout; the throwaway is gone afterwards
        self.assertNotEqual(root, c.wt)
        self.assertEqual(root.parent, c.work / "tmp")
        self.assertEqual(head, self.queue_task()["done_commit"])
        self.assertEqual(feat, "VALUE = 42\n")
        self.assertFalse(root.exists())
        self.assertEqual(changed, {"feat.py": {1}})
        self.assertEqual(argv[1:], ["-m", "unittest", "tests/core/test_feat.py"])
        self.assertEqual(kw["mutation_min"], 0.5)
        self.assertEqual(kw["budget_s"], 100.0)
        self.assertGreaterEqual(kw["per_mutant_timeout_s"], 5.0)
        self.assertLessEqual(kw["per_mutant_timeout_s"], 50.0)

    def test_mutation_left_changes_is_stage_error(self):
        from core.mutation import MutationResult

        def dirty(root, changed, argv, **kw):
            (root / "feat.py").write_text("VALUE = 0\n", encoding="utf-8")
            (root / "junk.txt").write_text("x", encoding="utf-8")
            return MutationResult(total=0, killed=0, reason="no mutation sites on changed lines")

        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.step()
        with patch.object(bootstrap, "run_mutation", side_effect=dirty):
            self.assertEqual(c.step(), "error")
        self.assertEqual(c.team.reviewer.prompts, [])
        t = self.queue_task()
        self.assertNotEqual(t["status"], "done")
        self.assertTrue(any("mutation left changes" in n for n in t["notes"]), t["notes"])
        self.assertFalse((c.wt / "junk.txt").exists())
        tmp = c.work / "tmp"
        self.assertEqual(list(tmp.iterdir()) if tmp.exists() else [], [])  # the throwaway is gone too

    # integration of P1B1 with the worktrees and the finalizer (T1B3b/T1B3e)
    def test_task_tests_run_at_exact_commit_in_throwaway(self):
        seen = []
        real = bootstrap.Conductor._exec_tests

        def spy(self_, t, cwd=None):
            where = cwd or self_.wt
            seen.append((where, git(where, "rev-parse", "HEAD"), (where / "feat.py").exists()))
            return real(self_, t, cwd=cwd)

        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.step()
        stage_a = git(c.wt, "rev-parse", "HEAD")
        with patch.object(bootstrap.Conductor, "_exec_tests", spy):
            c.step()
        t = self.queue_task()
        self.assertEqual(t["status"], "done")
        build_runs = [s for s in seen if s[0] != c.wt]
        self.assertTrue(build_runs)
        where, head, has_feat = build_runs[0]
        self.assertEqual(where.parent, c.work / "tmp")
        self.assertEqual(head, t["done_commit"])
        self.assertNotEqual(head, stage_a)
        self.assertTrue(has_feat)
        self.assertFalse(where.exists())

    def test_finalizer_pass_evidence_keeps_mutation_evidence(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature,
                              "reviewer": self.reviewer(reasons=["ok"])})
        c.step()
        c.step()
        t = self.queue_task()
        self.assertEqual(t["status"], "done")
        passes = self.events("pass")
        self.assertEqual(len(passes), 1)
        payload = passes[0]["payload"]
        self.assertEqual(payload["verdict"], "pass")
        self.assertEqual(payload["reasons"], ["ok"])
        self.assertTrue(payload["mutation"]["passed"])
        self.assertEqual(payload["mutation"]["survivors"], [])
        self.assertEqual(payload["task_commit"], t["done_commit"])
        self.assertTrue(payload["final_sha"])
        self.assertEqual(payload["merges"], [])


if __name__ == "__main__":
    unittest.main()
