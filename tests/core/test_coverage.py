"""T1C1: spec requirements and the ledger-verified coverage map (D-026 coverage, design §3.7)."""
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from core import coverage
from core.ledger import Ledger, Rejected

ROLES = {"forge-manager": "manager", "forge-executor": "executor", "forge-auditor": "auditor", "ci": "ci",
         "forge-core": "core"}

SPEC = """# Title

Intro paragraph, not a requirement.

- a preamble bullet, not a requirement

## 1. The physics

- **Agents never talk.** One process each.
  - a nested bullet is part of its parent
- The conductor is plain Python.

## 2. Roles

| Role | Engine |
|---|---|
| Manager | Claude |
| Builder | Claude Code |

## 3. One contract's path

1. **Test writer** writes the tests.
   - nested detail
2. **Builder** runs.

```python
- not a requirement: inside a code block
## 9. nor is this
```

## 4. Plain section

Only prose here.

### 4.1 a sub-heading is not a section

## Appendix without a number

- ignored
"""


def new_ledger(root: Path) -> Ledger:
    root.mkdir(parents=True, exist_ok=True)
    (root / "roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
    return Ledger(root)


def complete(led: Ledger, cid: str, commit: str = "a" * 40) -> None:
    led.apply({"proposal_id": f"create-{cid}", "action": "create", "contract_id": cid, "payload": {
        "title": cid, "spec_ref": cid, "acceptance": "python -m unittest x", "files_in_scope": ["x.py"],
        "max_attempts": 3, "token_budget": 1000}}, "forge-manager")
    led.apply({"proposal_id": f"{cid}-claim", "action": "claim", "contract_id": cid}, "forge-executor")
    led.apply({"proposal_id": f"{cid}-report", "action": "run_report", "contract_id": cid, "payload": {
        "run_id": f"{cid}-r", "claim": "done", "commit": commit, "changed": ["x.py"], "violations": [],
        "out_of_scope": []}}, "forge-core")
    led.apply({"proposal_id": f"{cid}-submit", "action": "submit", "contract_id": cid,
               "payload": {"commit": commit}}, "forge-executor")
    led.apply({"proposal_id": f"{cid}-ci", "action": "test_run", "contract_id": cid,
               "payload": {"run_id": f"{cid}-ci", "commit": commit, "passed": True}}, "ci")
    led.apply({"proposal_id": f"{cid}-pass", "action": "pass", "contract_id": cid,
               "payload": {"run_id": f"{cid}-ci"}}, "forge-auditor")


def task(tid, covers, status="todo", kind="build"):
    return {"id": tid, "kind": kind, "status": status, "covers": covers}


class ParseRequirementsTests(unittest.TestCase):
    def test_numbered_items_rows_and_plain_sections(self):
        reqs = coverage.parse_requirements(SPEC)
        self.assertEqual(list(reqs), ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4"])
        self.assertIn("Agents never talk", reqs["1.1"])
        self.assertIn("Manager", reqs["2.1"])
        self.assertIn("Builder", reqs["3.2"])
        self.assertIn("Plain section", reqs["4"])

    def test_nested_bullets_code_blocks_and_unnumbered_sections_are_not_requirements(self):
        text = " ".join(coverage.parse_requirements(SPEC).values())
        for absent in ("nested", "code block", "nor is this", "ignored", "preamble", "sub-heading"):
            self.assertNotIn(absent, text)

    def test_crlf_and_real_design_numbering(self):
        reqs = coverage.parse_requirements(SPEC.replace("\n", "\r\n"))
        self.assertEqual(list(reqs), ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4"])
        design = Path(__file__).resolve().parents[2] / "docs" / "specs" / "layer-1-design.md"
        if design.exists():
            real = coverage.parse_requirements(design.read_text(encoding="utf-8"))
            self.assertIn("Drift keeper", real["3.7"])  # design §3.7
            self.assertIn("Manager", real["2.1"])

    def test_duplicate_section_numbers_are_an_error(self):
        with self.assertRaises(ValueError):
            coverage.parse_requirements("## 1. A\n\n- x\n\n## 1. B\n\n- y\n")

    def test_texts_are_capped(self):
        reqs = coverage.parse_requirements("## 1. A\n\n- " + "x" * 5000 + "\n")
        self.assertLessEqual(len(reqs["1.1"]), coverage.TEXT_CAP)


class ClaimsTests(unittest.TestCase):
    def test_only_a_list_of_strings_counts(self):
        self.assertEqual(coverage.claims({"covers": ["1.1", "2.1", "1.1"]}), ["1.1", "2.1"])
        for bad in (None, "1.1", [1], ["1.1", 2], {"1.1": 1}):
            self.assertEqual(coverage.claims({"covers": bad}), [])
        self.assertEqual(coverage.claims({}), [])


class VerifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "state"
        self.led = new_ledger(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_ledger_has_no_completions(self):
        self.assertEqual(coverage.ledger_completed(self.led), set())

    def test_completion_comes_from_pass_events(self):
        complete(self.led, "T1")
        self.assertEqual(coverage.ledger_completed(self.led), {"T1"})

    def test_queue_alone_is_not_trusted(self):
        """A task marked done in the queue without a ledger pass is not verified, and a ledger pass for a task
        the queue has not finished is not verified either."""
        complete(self.led, "T2")
        tasks = [task("T1", ["1.1"], "done"), task("T2", ["1.2"], "merge_pending"), task("T3", ["1.1"], "done")]
        complete(self.led, "T3")
        self.assertEqual(coverage.verified_done(tasks, self.led), {"T3"})

    def test_plan_tasks_never_count(self):
        complete(self.led, "P1")
        self.assertEqual(coverage.verified_done([task("P1", ["1.1"], "done", kind="plan")], self.led), set())

    def test_broken_chain_raises(self):
        complete(self.led, "T1")
        lines = self.led.events_path.read_text(encoding="utf-8").splitlines()
        e = json.loads(lines[0])
        e["payload"]["title"] = "forged"
        lines[0] = json.dumps(e)
        self.led.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(Rejected):
            coverage.ledger_completed(self.led)


class ComputeTests(unittest.TestCase):
    REQS = {"1.1": "a", "1.2": "b", "2.1": "c", "3": "d"}

    def test_statuses(self):
        tasks = [task("A", ["1.1"]), task("B", ["1.2"]), task("C", ["1.2"]), task("D", ["2.1"])]
        cov = coverage.compute(self.REQS, tasks, {"A", "B"})
        self.assertEqual(cov.status("1.1"), "covered")
        self.assertEqual(cov.status("1.2"), "partial")
        self.assertEqual(cov.status("2.1"), "open")
        self.assertEqual(cov.status("3"), "unclaimed")
        self.assertEqual(cov.covered, 1)
        self.assertEqual(cov.total, 4)
        self.assertEqual(cov.score, Fraction(3, 2))

    def test_partial_completion_never_marks_a_requirement_covered(self):
        """One of four tasks for a requirement done: partial, a quarter of credit, not covered."""
        tasks = [task(f"T{i}", ["1.1"]) for i in range(4)]
        seen = []
        for i in range(4):
            cov = coverage.compute(self.REQS, tasks, {f"T{j}" for j in range(i + 1)})
            seen.append((cov.status("1.1"), cov.score))
        self.assertEqual(seen, [("partial", Fraction(1, 4)), ("partial", Fraction(1, 2)),
                                ("partial", Fraction(3, 4)), ("covered", Fraction(1))])

    def test_new_claimant_reopens_a_covered_requirement(self):
        tasks = [task("A", ["1.1"])]
        self.assertEqual(coverage.compute(self.REQS, tasks, {"A"}).status("1.1"), "covered")
        tasks.append(task("B", ["1.1"]))
        self.assertEqual(coverage.compute(self.REQS, tasks, {"A"}).status("1.1"), "partial")

    def test_unknown_claims_and_tasks_without_claims_add_nothing(self):
        tasks = [task("A", ["9.9"]), {"id": "B", "kind": "build", "status": "done"}]
        cov = coverage.compute(self.REQS, tasks, {"A", "B"})
        self.assertEqual(cov.score, 0)
        self.assertEqual(cov.covered, 0)

    def test_one_task_may_serve_several_requirements(self):
        cov = coverage.compute(self.REQS, [task("A", ["1.1", "3"])], {"A"})
        self.assertEqual((cov.covered, cov.score), (2, Fraction(2)))

    def test_report_lists_every_requirement_with_status_and_claimants(self):
        tasks = [task("A", ["1.1"]), task("B", ["1.1"])]
        text = coverage.compute(self.REQS, tasks, {"A"}).report()
        self.assertIn("1.1 [partial 1/2] a (tasks: A done, B open)", text)
        self.assertIn("3 [unclaimed] d", text)
        self.assertIn("covered 0 of 4", text)


if __name__ == "__main__":
    unittest.main()
