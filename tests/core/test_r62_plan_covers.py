"""R62: planner coverage claims survive validation, review and merge bookkeeping."""
import json
import unittest

from core import coverage, drift
from tests.core.test_drift_state import score_of
from tests.core.test_planning_drift import DriftHarness, SPEC, btask


class PlanCoversTests(DriftHarness):
    def planned_task(self, tid="T1", **fields):
        task = btask(tid)
        task.pop("kind")
        # R41 still applies: coverage tests must reach coverage validation, not
        # fail because their otherwise valid task instructions are too short.
        task["section"] = (
            f"Implement m_{tid.lower()}.VALUE as the integer 1. "
            "The module has no dependencies and must import without side effects. "
            "Acceptance criteria: importing the module exposes VALUE; VALUE is an "
            "integer and equals 1 on every import. The first alpha thing, second "
            "alpha thing and beta thing in this toy spec are proved by that value. "
            "Write the acceptance test in the listed test file using unittest. "
            "Assert the exact value and type, including after reloading the module. "
            "The test must fail when the module is missing, when VALUE is absent, "
            "or when VALUE is zero or a string. The builder may edit only the "
            "listed module; no external services, manual steps or other file "
            "changes are needed. Run the provided test command to verify it."
        )
        task.update(fields)
        return task

    def run_plan(self, *tasks, no_coverage=False, limits=None):
        plan = {"id": "P1", "kind": "plan", "title": "Plan toy layer",
                "section": "Plan the toy layer implementation.", "plan_file": "plan.md"}
        if no_coverage:
            plan["no_coverage"] = True

        def planner(prompt, cwd):
            (cwd / "plan.md").write_text("# Toy plan\n" + "\n".join(
                task["section"] for task in tasks), encoding="utf-8")
            return json.dumps({"tasks": list(tasks)}), 1

        c = self.make_conductor({"planner": planner}, limits)
        c.init_queue(self.layer, [plan])
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(self.team.planner.prompts), 1)
        return c

    def assert_accepted(self, *ids):
        self.assertEqual([t["id"] for t in self.queue()["tasks"]], ["P1", *ids])
        self.assertEqual(self.task_rec("P1")["status"], "done")
        self.assertEqual(self.task_rec("P1").get("plan_rejects", 0), 0)
        for tid in ids:
            self.assertEqual(self.task_rec(tid)["status"], "todo")
            self.assertEqual(self.task_rec(tid)["kind"], "build")

    def assert_covers_rejected(self, task, problem):
        # Put a valid task first: rejection must append none of the plan.
        self.run_plan(self.planned_task("GOOD", covers=["1.2"]), task)
        plan = self.task_rec("P1")
        notes = [n for n in plan["notes"] if n.startswith("plan rejected: covers")]
        with self.subTest(check="reason"):
            self.assertTrue(notes, plan["notes"])
            self.assertIn(task["id"], notes[-1])
            self.assertRegex(notes[-1].lower(), problem)
        with self.subTest(check="attempt counted"):
            self.assertEqual(plan.get("plan_rejects", 0), 1)
            self.assertEqual(plan["status"], "todo")
        with self.subTest(check="nothing appended"):
            self.assertEqual([t["id"] for t in self.queue()["tasks"]], ["P1"])
        with self.subTest(check="rejected before review"):
            self.assertEqual(self.team.reviewer.prompts, [])

    def test_planner_prompt_lists_requirement_ids_text_and_requests_covers(self):
        self.run_plan(self.planned_task(covers=["1.1"]))
        prompt = self.team.planner.prompts[0]
        for rid, text in coverage.parse_requirements(SPEC).items():
            with self.subTest(requirement=rid):
                self.assertIn(f"{rid}: {text}", prompt)
        with self.subTest(instruction="covers"):
            self.assertIn("covers", prompt)
            self.assertRegex(prompt.lower(), r"acceptance (?:tests|criteria)")

    def test_valid_covers_are_preserved_exactly_in_queued_task(self):
        covers = ["2.1", "1.2", "1.1"]  # Non-sorted order must survive.
        self.run_plan(self.planned_task(covers=covers))
        self.assert_accepted("T1")
        self.assertEqual(self.task_rec("T1").get("covers"), covers)

    def test_missing_covers_rejects_the_whole_plan(self):
        self.assert_covers_rejected(self.planned_task(), r"missing|required|non.?empty")

    def test_empty_covers_rejects_the_whole_plan(self):
        self.assert_covers_rejected(self.planned_task(covers=[]), r"empty|required")

    def test_unknown_requirement_rejects_the_whole_plan(self):
        self.assert_covers_rejected(self.planned_task(covers=["1.1", "9.9"]), r"unknown|9\.9")

    def test_repeated_requirement_rejects_the_whole_plan(self):
        self.assert_covers_rejected(self.planned_task(covers=["1.1", "1.1"]), r"repeat|duplicat|unique")

    def test_no_coverage_plan_accepts_missing_covers_and_ignores_claims(self):
        self.run_plan(self.planned_task(), self.planned_task("T2", covers=["1.1"]),
                      no_coverage=True)
        self.assert_accepted("T1", "T2")
        for tid in ("T1", "T2"):
            self.assertFalse(self.task_rec(tid).get("covers"))
        # Even completed tasks must earn no credit when the plan opted out.
        cov = coverage.compute(coverage.parse_requirements(SPEC), self.queue()["tasks"], {"T1", "T2"})
        self.assertEqual(cov.score, 0)

    def test_no_usable_spec_accepts_missing_covers_and_ignores_claims(self):
        self.run_plan(self.planned_task(), self.planned_task("T2", covers=["1.1"]),
                      limits={"spec_file": "docs/specs/missing.md",
                              "claude_daily_token_cap": 10 ** 9,
                              "codex_daily_token_cap": 10 ** 9})
        self.assert_accepted("T1", "T2")
        for tid in ("T1", "T2"):
            self.assertFalse(self.task_rec(tid).get("covers"))

    def test_reviewer_prompt_requires_acceptance_criteria_for_covers_claims(self):
        self.run_plan(self.planned_task(covers=["1.1"]))
        self.assertEqual(len(self.team.reviewer.prompts), 1)
        # Inspect instructions, excluding the plan/JSON which echo the fixture.
        instructions = self.team.reviewer.prompts[0].split("\nPLAN:\n", 1)[0].lower()
        self.assertIn("covers", instructions)
        self.assertIn("acceptance criteria", instructions)
        self.assertIn("section", instructions)
        self.assertRegex(instructions, r"backed|matching|match|support|prove")
        self.assertIn("blocking", instructions)


class UncoveredMergeStateTests(unittest.TestCase):
    def test_uncovered_merge_is_unknown_preserves_count_and_resets_active_mark(self):
        d = drift.adopt([], set(), drift_due=False, active_s=5.0)
        score = score_of({})
        drift.record_merges(d, ["C1"], {"C1"}, 10.0, score)
        self.assertEqual(d["no_gain"], 1)

        events = drift.record_merges(d, ["C1", "U1"], {"C1", "U1"}, 25.0,
                                     score, uncovered={"U1"})
        self.assertEqual([(e["tid"], e["gain"]) for e in events], [("U1", None)])
        self.assertIsNone(d["history"][-1]["gain"])
        self.assertEqual(d["no_gain"], 1)  # Neither increment nor reset.
        self.assertEqual(d["active_mark"], 25.0)
        self.assertEqual(d["counted"], ["C1", "U1"])

        events = drift.record_merges(d, ["C1", "U1", "C2"], {"C1", "U1", "C2"},
                                     40.0, score, uncovered={"U1"})
        self.assertEqual([(e["tid"], e["gain"]) for e in events], [("C2", False)])
        self.assertEqual(d["no_gain"], 2)
        self.assertEqual(d["active_mark"], 40.0)


class UncoveredConductorTests(DriftHarness):
    def test_three_merges_without_covers_never_trigger_no_coverage_gain_stall(self):
        c = self.conductor(*(btask(f"T{i}") for i in range(1, 4)), manager=False)
        snapshots = []
        for tid in ("T1", "T2", "T3"):
            self.finish(c, tid)
            snapshots.append(self.dstate())
        self.assertEqual(self.queue()["drift_marks"], ["T1", "T2", "T3"])
        self.assertEqual([self.task_rec(tid)["status"] for tid in ("T1", "T2", "T3")],
                         ["done", "done", "done"])
        with self.subTest(check="no stall sent to keeper"):
            self.assertFalse(any("no coverage gain" in p.lower() for p in self.keeper_prompts))
        with self.subTest(check="no pause or replan question"):
            self.assertFalse((self.state / "PAUSED").exists())
            self.assertFalse(any("Q-replan" in subject for subject, _ in self.mails))
        for index, d in enumerate(snapshots, 1):
            with self.subTest(merge=index):
                self.assertEqual(d["no_gain"], 0)
                self.assertIsNone(d["stall"])
                self.assertIsNone(d["replan"])
                self.assertEqual([e["gain"] for e in d["history"]], [None] * index)


if __name__ == "__main__":
    unittest.main()
