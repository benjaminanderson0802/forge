"""T1C5: the drift keeper's coverage-must-rise rule, the D-026 stall rules and the Manager's re-plan.

Fakes only; real git, real test runs. The layer worktree holds a small spec, so requirements are 1.1, 1.2, 2.1.
Covers the rejected-plan gaps: merge recorded before the active-time check; complete validation at proposal
and at acceptance; partial coverage; adopted baseline; restart safety; ledger-verified coverage."""
import json
import re
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from core import drift
from core.agents import FakeAgent

try:
    from tests.core.test_bootstrap import Harness, git
except ImportError:  # pragma: no cover
    from test_bootstrap import Harness, git

T0 = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
SPEC = "# Toy spec\n\n## 1. Alpha\n\n- first alpha thing\n- second alpha thing\n\n## 2. Beta\n\n- the beta thing\n"
CANARY = "CANARY-7f3a"


class Crash(BaseException):
    """A simulated process death at a named crash point."""


def btask(tid, covers=None):
    t = {"id": tid, "kind": "build", "title": f"Build {tid}", "section": f"Make m_{tid.lower()}.VALUE equal 1.",
         "files_in_scope": [f"m_{tid.lower()}.py"], "test_files": [f"tests/core/test_{tid.lower()}.py"],
         "test_cmd": f"python -m unittest tests/core/test_{tid.lower()}.py"}
    if covers is not None:
        t["covers"] = covers
    return t


def proposal(*ids, covers=("1.2",)):
    return {"tasks": [{k: v for k, v in btask(i, list(covers)).items() if k != "kind"} for i in ids],
            "reasons": ["cover the rest"]}


class DriftHarness(Harness):
    def setUp(self):
        super().setUp()
        (self.repo / "docs" / "specs").mkdir(parents=True)
        (self.repo / "docs" / "specs" / "layer-1-design.md").write_text(SPEC, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "spec")
        self.now = T0
        self.builder_minutes = 0
        self.builder_ok = True
        self.keeper = {"status": "ok", "reasons": []}
        self.manager_answers = []
        self.manager_prompts = []
        self.manager_cwds = []
        self.keeper_prompts = []

    # ---- fake agents
    def writer(self, prompt, cwd):
        f = re.search(r"Write only these files: (\S+?)\. ", prompt).group(1)
        mod = re.search(r"Files you may change: (\S+)\.py", prompt).group(1)
        p = cwd / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"import unittest\nimport {mod}\nclass T(unittest.TestCase):\n"
                     f"    def test_value(self):\n        self.assertEqual({mod}.VALUE, 1)\n", encoding="utf-8")
        return json.dumps({"files": [f]}), 1

    def builder(self, prompt, cwd):
        self.now += timedelta(minutes=self.builder_minutes)
        mod = re.search(r"Files you may change: (\S+)", prompt).group(1)
        (cwd / mod).write_text(f"VALUE = {1 if self.builder_ok else 0}\n", encoding="utf-8")
        return '{"status":"done"}', 1

    def drift_keeper(self, prompt, cwd):
        self.keeper_prompts.append(prompt)
        return json.dumps(self.keeper), 1

    def manager_agent(self, prompt, cwd):
        self.manager_prompts.append(prompt)
        self.manager_cwds.append(cwd)
        a = self.manager_answers.pop(0) if self.manager_answers else proposal("M1")
        if callable(a):
            return a(prompt, cwd)
        return (a if isinstance(a, str) else json.dumps(a)), 1

    def conductor(self, *tasks, manager=True, limits=None, reviewer=None):
        agents = {"test_writer": self.writer, "builder": self.builder, "drift_keeper": self.drift_keeper}
        if reviewer:
            agents["reviewer"] = reviewer
        lim = {"claude_daily_token_cap": 10 ** 9, "codex_daily_token_cap": 10 ** 9}
        lim.update(limits or {})
        c = self.make_conductor(agents, lim)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        if manager:
            c.manager = FakeAgent(self.manager_agent, provider="claude")
        c.init_queue(self.layer, list(tasks))
        return c

    # ---- helpers
    def queue(self):
        return json.loads((self.state / "queue.json").read_text(encoding="utf-8"))

    def task_rec(self, tid):
        return next(t for t in self.queue()["tasks"] if t["id"] == tid)

    def dstate(self):
        return drift.load(self.state)

    def finish(self, c, tid):
        """Run steps until tid is done and its drift check is over."""
        for _ in range(12):
            if self.task_rec(tid)["status"] == "done" and not self.queue().get("drift_due"):
                return
            c.step()
        self.fail(f"{tid} did not finish: {self.task_rec(tid)['status']} {self.task_rec(tid)['notes']}")


class CoverageMustRiseTests(DriftHarness):
    def test_four_tasks_for_one_requirement_each_raise_coverage(self):
        """Partial coverage is credited per merge, so 4 claimants of one requirement never stall."""
        c = self.conductor(*(btask(f"T{i}", ["1.1"]) for i in range(1, 5)))
        seen = []
        for i in range(1, 5):
            self.finish(c, f"T{i}")
            seen.append(self.dstate()["no_gain"])
        self.assertEqual(seen, [0, 0, 0, 0])
        self.assertIsNone(self.dstate()["replan"])
        self.assertEqual(self.manager_prompts, [])

    def test_three_merges_without_gain_bring_in_the_drift_keeper_and_a_replan(self):
        c = self.conductor(btask("T1", ["1.1"]), btask("T2"), btask("T3", ["9.9"]), btask("T4", []), btask("T5"))
        for tid, want in (("T1", 0), ("T2", 1), ("T3", 2)):
            self.finish(c, tid)
            self.assertEqual(self.dstate()["no_gain"], want, tid)
            self.assertIsNone(self.dstate()["replan"])
        self.finish(c, "T4")
        d = self.dstate()
        self.assertEqual(d["replan"]["trigger"], "no coverage gain in 3 merges")
        self.assertIn("no coverage gain in 3 merges", self.keeper_prompts[-1])
        self.assertIn("COVERAGE", self.keeper_prompts[-1])
        self.assertEqual(d["no_gain"], 0)  # acted on: a fresh window
        self.assertEqual(c.step(), "worked")  # the Manager re-plans before any other task runs
        self.assertEqual(len(self.manager_prompts), 1)
        self.assertEqual(self.task_rec("T5")["status"], "todo")
        m1 = self.task_rec("M1")
        self.assertEqual((m1["status"], m1["kind"], m1["covers"]), ("todo", "build", ["1.2"]))
        self.assertIsNone(self.dstate()["replan"])
        self.assertIn(d["replan"]["id"], self.queue()["replans"])

    def test_drift_keeper_replan_without_a_stall_also_goes_to_the_manager(self):
        self.keeper = {"status": "replan", "reasons": ["drifting off 2.1"]}
        c = self.conductor(btask("T1", ["1.1"]))
        self.finish(c, "T1")
        self.assertEqual(self.dstate()["replan"]["reasons"], ["drifting off 2.1"])
        self.assertFalse((self.state / "PAUSED").exists())
        c.step()
        self.assertIn("drifting off 2.1", self.manager_prompts[0])

    def test_coverage_needs_the_ledger_not_only_the_queue(self):
        """A task the queue calls done but the ledger never passed is merged without gain."""
        c = self.conductor(btask("T1", ["1.1"]), btask("T2", ["1.2"]))
        c.step()  # adopts the (empty) baseline
        q = self.queue()
        q["tasks"][0]["status"] = "done"
        q["drift_marks"], q["drift_due"] = ["T1"], True
        c._save_queue(q)
        c.step()
        d = self.dstate()
        self.assertIn("T1", d["counted"])
        self.assertEqual(d["no_gain"], 1)
        self.assertEqual(d["history"][-1], {"tid": "T1", "gain": False, "score": "0"})

    def test_baseline_adopts_completed_work(self):
        """drift.json appears only after T1 was done: T2, which adds nothing, gets no credit for T1."""
        c = self.conductor(btask("T1", ["1.1"]), btask("T2", ["9.9"]))
        self.finish(c, "T1")
        (self.state / drift.DRIFT_FILE).unlink()
        self.finish(c, "T2")
        d = self.dstate()
        self.assertEqual(d["no_gain"], 1)
        self.assertEqual([h["tid"] for h in d["history"]], ["T2"])

    def test_a_failed_drift_check_does_not_count_the_merge_twice(self):
        c = self.conductor(btask("T1", ["9.9"]))
        c.step(); c.step()  # tests, build and merge
        c.team.drift_keeper.script = lambda p, cwd: ("not json", 1)
        c.step()
        self.assertTrue(self.queue()["drift_due"])
        c.team.drift_keeper.script = self.drift_keeper
        c.step()
        self.assertFalse(self.queue()["drift_due"])
        self.assertEqual(self.dstate()["no_gain"], 1)
        self.assertEqual(self.dstate()["counted"].count("T1"), 1)

    def test_drift_keeper_prompt_shows_the_coverage_map(self):
        c = self.conductor(btask("T1", ["1.1"]))
        self.finish(c, "T1")
        p = self.keeper_prompts[-1]
        self.assertIn("1.1 [covered] first alpha thing", p)
        self.assertIn("2.1 [unclaimed] the beta thing", p)


class ActiveTimeStallTests(DriftHarness):
    LIM = {"builder_focus_s": 10 ** 7}

    def test_a_passing_attempt_that_crosses_two_active_hours_is_no_stall(self):
        """Boundary: the merge is recorded before the active-time check, so no 'no merge' re-plan."""
        c = self.conductor(btask("T1", ["1.1"]), btask("T2", ["1.2"]), limits=self.LIM)
        self.builder_minutes = 125
        self.finish(c, "T1")
        self.builder_minutes = 0
        c.step()
        d = self.dstate()
        self.assertIsNone(d["stall"])
        self.assertIsNone(d["replan"])
        self.assertFalse(any("no merge" in p for p in self.keeper_prompts))

    def test_two_active_hours_without_a_merge_bring_in_the_drift_keeper(self):
        c = self.conductor(btask("T1", ["1.1"]), limits=self.LIM)
        c.step()  # tests accepted
        self.builder_ok, self.builder_minutes = False, 70
        c.step()
        self.assertIsNone(self.dstate()["stall"])
        c.step()  # 140 active minutes, no merge
        c.step()  # the stall is noticed and the drift keeper runs
        self.assertTrue(any("no merge in 2 active hours" in p for p in self.keeper_prompts))
        d = self.dstate()
        self.assertEqual(d["replan"]["trigger"], "no merge in 2 active hours")
        self.assertIsNone(d["stall"])

    def test_waiting_never_counts_toward_the_stall(self):
        c = self.conductor(btask("T1", ["1.1"]), limits=self.LIM)
        c.step()
        self.builder_ok, self.builder_minutes = False, 10
        c.step()
        (self.state / "PAUSED").write_text("x")
        for _ in range(3):
            self.now += timedelta(hours=5)
            self.assertEqual(c.step(), "paused")
        (self.state / "PAUSED").unlink()
        self.builder_minutes = 0
        c.step()
        self.assertIsNone(self.dstate()["stall"])
        self.assertEqual(self.keeper_prompts, [])

    def test_no_stall_when_no_build_work_remains(self):
        c = self.conductor(btask("T1", ["1.1"]), limits=self.LIM)
        self.finish(c, "T1")
        c._activity().add(10 * 3600)
        c.step()
        self.assertIsNone(self.dstate()["stall"])


class RestartSafetyTests(DriftHarness):
    def crash_at(self, c, point):
        def crash(p):
            if p == point:
                raise Crash(p)
        c._crash = crash

    def test_crash_between_saving_the_replan_and_clearing_drift_due_loses_nothing(self):
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(btask("T1", ["1.1"]))
        c.step(); c.step()
        self.crash_at(c, "after:replan-pending")
        with self.assertRaises(Crash):
            c.step()
        rid = self.dstate()["replan"]["id"]
        self.assertTrue(self.queue()["drift_due"])
        c._crash = lambda p: None
        c.step()  # the drift check again: same pending re-plan, the merge not counted twice
        self.assertEqual(self.dstate()["replan"]["id"], rid)
        self.assertEqual(self.dstate()["counted"].count("T1"), 1)
        self.assertFalse(self.queue()["drift_due"])
        c.step()
        self.assertEqual(len(self.manager_prompts), 1)
        self.assertIsNone(self.dstate()["replan"])

    def test_crash_after_acceptance_neither_duplicates_nor_reruns(self):
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(btask("T1", ["1.1"]))
        self.finish(c, "T1")
        self.crash_at(c, "after:replan-accepted")
        with self.assertRaises(Crash):
            c.step()
        self.assertIsNotNone(self.dstate()["replan"])
        c._crash = lambda p: None
        c.step()
        self.assertIsNone(self.dstate()["replan"])
        self.assertEqual([t["id"] for t in self.queue()["tasks"]].count("M1"), 1)
        self.assertEqual(len(self.manager_prompts), 1)

    def test_manager_error_keeps_the_replan_and_retries(self):
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(btask("T1", ["1.1"]))
        self.finish(c, "T1")
        self.manager_answers = ["garbage", proposal("M1")]
        c.step()
        self.assertEqual(self.dstate()["replan"]["attempts"], 1)
        c.step()
        self.assertIsNone(self.dstate()["replan"])
        self.assertEqual(self.task_rec("M1")["status"], "todo")

    def test_git_error_during_replan_is_an_error_step_and_keeps_the_replan(self):
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(btask("T1", ["1.1"]))
        self.finish(c, "T1")
        real = c.trees.__class__.throwaway

        def broken(self_, sha):
            raise RuntimeError("git worktree add failed")
        c.trees.__class__.throwaway = broken
        try:
            self.assertEqual(c.step(), "error")
        finally:
            c.trees.__class__.throwaway = real
        self.assertIsNotNone(self.dstate()["replan"])
        self.assertEqual(self.dstate()["replan"]["attempts"], 0)
        c.step()
        self.assertIsNone(self.dstate()["replan"])


class ManagerTests(DriftHarness):
    def replan_ready(self, *tasks):
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(*(tasks or [btask("T1", ["1.1"])]))
        self.finish(c, "T1")
        self.keeper = {"status": "ok", "reasons": []}
        return c

    def test_a_fresh_manager_sees_the_ledger_and_spec_only(self):
        c = self.replan_ready(btask("T1", ["1.1"]), btask("T2", ["2.1"]))
        q = self.queue()
        for t in q["tasks"]:
            t["notes"].append(f"note {CANARY}")
            t["trouble_notes"].append(f"trouble {CANARY}")
            t["review_feedback"] = [f"review {CANARY}"]
            t["section"] += f" section {CANARY}"
        q["notes"] = [f"Ben: {CANARY}"]
        c._save_queue(q)
        (self.state / "dead_ends.jsonl").write_text(json.dumps({"task": "T1", "notes": CANARY}) + "\n")
        c._ask("blocked", f"question {CANARY}", f"body {CANARY}", task="T2")
        (self.state / "runs" / "planted").mkdir(parents=True, exist_ok=True)
        (self.state / "runs" / "planted" / "prompt.md").write_text(CANARY)
        c.step()
        self.assertEqual(len(self.manager_prompts), 1)
        p = self.manager_prompts[0]
        self.assertNotIn(CANARY, p)
        for part in ("SPEC", "first alpha thing", "LEDGER", "T1 [done, completed]", "1.1 [covered]",
                     "2.1 [open]", "off course"):
            self.assertIn(part, p)
        self.assertNotEqual(self.manager_cwds[0].resolve(), c.wt.resolve())  # a throwaway checkout
        self.assertFalse(self.manager_cwds[0].exists())

    def test_a_manager_that_writes_is_rejected(self):
        c = self.replan_ready()

        def writes(prompt, cwd):
            (cwd / "sneaky.py").write_text("x = 1\n")
            return json.dumps(proposal("M1")), 1
        self.manager_answers = [writes]
        c.step()
        self.assertEqual(self.dstate()["replan"]["attempts"], 1)
        self.assertTrue(any("read-only" in n for n in self.dstate()["replan"]["notes"]))
        self.assertNotIn("M1", [t["id"] for t in self.queue()["tasks"]])

    def test_tests_outside_tests_core_and_bad_types_are_rejected_then_ben_is_asked(self):
        c = self.replan_ready()
        bad1 = proposal("M1")
        bad1["tasks"][0]["test_files"] = ["tests/test_m1.py"]
        bad1["tasks"][0]["test_cmd"] = "python -m unittest tests/test_m1.py"
        bad2 = proposal("M2")
        bad2["tasks"][0]["covers"] = "1.2"
        self.manager_answers = [bad1, bad2]
        c.step()
        self.assertEqual(self.dstate()["replan"]["attempts"], 1)
        c.step()
        self.assertIsNone(self.dstate()["replan"])
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(sum(s.startswith("[Forge] FYI:") for s, b in self.mails), 1)
        self.assertFalse(any(q.get("kind") == "replan" and q.get("status") == "open"
                             for q in c._read("questions.json", {}).values()))
        ids = [t["id"] for t in self.queue()["tasks"]]
        self.assertNotIn("M1", ids)
        self.assertNotIn("M2", ids)

    def test_ids_that_clash_with_the_queue_or_ledger_are_rejected(self):
        c = self.replan_ready(btask("T1", ["1.1"]), btask("T2", ["1.2"]))
        self.manager_answers = [proposal("T2"), proposal("T1")]
        c.step()
        self.assertEqual(self.dstate()["replan"]["attempts"], 1)
        c.step()
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(sum(s.startswith("[Forge] FYI:") for s, b in self.mails), 1)
        self.assertFalse(any(q.get("kind") == "replan" and q.get("status") == "open"
                             for q in c._read("questions.json", {}).values()))

    def test_acceptance_revalidates_against_the_current_queue(self):
        c = self.replan_ready()
        rp = self.dstate()["replan"]
        self.assertEqual(c._accept_replan(rp, proposal("T1")["tasks"]), ["task 'T1': id already used"])
        bad = proposal("M9")["tasks"]
        bad[0]["test_files"] = ["tests/acceptance/test_m9.py"]
        self.assertTrue(c._accept_replan(rp, bad))
        self.assertNotIn("M9", [t["id"] for t in self.queue()["tasks"]])

    def test_reviewer_must_pass_the_replan(self):
        reviews = []

        def reviewer(p, cwd):
            reviews.append(p)
            if "RE-PLAN" in p:
                return '{"verdict":"fail","reasons":["too vague"]}', 1
            return '{"verdict":"pass","reasons":[]}', 1
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(btask("T1", ["1.1"]), reviewer=reviewer)
        self.finish(c, "T1")
        c.step()
        self.assertTrue(any("RE-PLAN" in p for p in reviews))
        self.assertEqual(self.dstate()["replan"]["attempts"], 1)
        self.assertIn("too vague", " ".join(self.dstate()["replan"]["notes"]))

    def test_repeated_replans_without_gain_go_to_ben(self):
        c = self.replan_ready()
        d = self.dstate()
        d["auto_replans"] = 2
        drift.save(self.state, d)
        c.step()
        self.assertEqual(self.manager_prompts, [])
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(sum(s.startswith("[Forge] FYI:") for s, b in self.mails), 1)
        self.assertFalse(any(q.get("kind") == "replan" and q.get("status") == "open"
                             for q in c._read("questions.json", {}).values()))
        self.assertIsNone(self.dstate()["replan"])

    def test_no_spec_means_ben_decides(self):
        c = self.replan_ready()
        c.limits["spec_file"] = "docs/specs/missing.md"
        c.step()
        self.assertEqual(self.manager_prompts, [])
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(sum(s.startswith("[Forge] FYI:") for s, b in self.mails), 1)
        self.assertFalse(any(q.get("kind") == "replan" and q.get("status") == "open"
                             for q in c._read("questions.json", {}).values()))

    def test_capped_manager_changes_nothing(self):
        c = self.replan_ready()
        c.meter.add("claude", 10 ** 10)
        self.assertEqual(c.step(), "capped")
        self.assertEqual(self.manager_prompts, [])
        self.assertEqual(self.dstate()["replan"]["attempts"], 0)

    def test_gate_stays_closed_while_a_replan_is_pending(self):
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c = self.conductor(btask("T1", ["1.1"]))
        self.finish(c, "T1")
        c.manager = None  # can't run: the re-plan waits and the gate stays shut
        for _ in range(2):
            self.assertEqual(c.step(), "worked" if _ == 0 else "gate")
        self.assertTrue(any(a[:2] == ["pr", "create"] for a in self.gh_calls))

    def test_without_a_manager_a_stall_pauses_and_asks_ben(self):
        c = self.conductor(btask("T1"), btask("T2"), btask("T3"), manager=False)
        for tid in ("T1", "T2", "T3"):
            self.finish(c, tid)
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(sum(s.startswith("[Forge] FYI:") for s, b in self.mails), 1)
        self.assertFalse(any(q.get("kind") == "replan" and q.get("status") == "open"
                             for q in c._read("questions.json", {}).values()))
        self.assertIsNone(self.dstate()["replan"])


if __name__ == "__main__":
    unittest.main()
