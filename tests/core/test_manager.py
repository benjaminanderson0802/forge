"""T1C3: the Manager's inputs (ledger and spec only) and complete validation of its proposals."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from core import coverage, manager
from core.agents import schema_ok, strict_schema
from core.ledger import Ledger
try:
    from tests.core.test_coverage import ROLES, complete
except ImportError:  # pragma: no cover
    from test_coverage import ROLES, complete

REQS = {"1.1": "agents never talk", "3.7": "drift keeper after every merge", "4": "plain section"}


def good(**kw):
    t = {"id": "T9", "title": "Coverage map", "section": "Build the coverage map.", "covers": ["3.7"],
         "files_in_scope": ["core/coverage.py"], "test_files": ["tests/core/test_cov9.py"],
         "test_cmd": "python -m unittest tests/core/test_cov9.py"}
    t.update(kw)
    return t


class SchemaTests(unittest.TestCase):
    def test_schema_accepts_a_good_proposal_and_is_strict_convertible(self):
        self.assertTrue(schema_ok({"tasks": [good()], "reasons": ["x"]}, manager.S_MANAGER))
        self.assertTrue(schema_ok({"tasks": [good(needs=["github"])]}, manager.S_MANAGER))
        strict = strict_schema(manager.S_MANAGER)
        self.assertFalse(strict["properties"]["tasks"]["items"]["additionalProperties"])

    def test_schema_rejects_wrong_types(self):
        self.assertFalse(schema_ok({"tasks": [good(covers="3.7")]}, manager.S_MANAGER))
        self.assertFalse(schema_ok({"tasks": [good(title=5)]}, manager.S_MANAGER))
        self.assertFalse(schema_ok({}, manager.S_MANAGER))


class ValidateTests(unittest.TestCase):
    def check(self, *tasks, existing=()):
        return manager.validate_proposal({"tasks": list(tasks)}, REQS, set(existing))

    def problems(self, *tasks, existing=()):
        return self.check(*tasks, existing=existing)[1]

    def test_good_task_passes_and_is_normalised(self):
        tasks, problems = self.check(good(files_in_scope=["core\\coverage.py"], needs=["github"]))
        self.assertEqual(problems, [])
        self.assertEqual(tasks[0]["files_in_scope"], ["core/coverage.py"])
        self.assertEqual(tasks[0]["needs"], ["github"])
        self.assertEqual(set(tasks[0]), set(manager.REQUIRED) | {"needs"})

    def test_not_a_proposal(self):
        for data in (None, [], {"tasks": []}, {"tasks": "T1"}, {"tasks": [1]}, {"tasks": [good()] * 13}):
            self.assertTrue(manager.validate_proposal(data, REQS, set())[1], data)

    def test_tests_must_live_under_tests_core(self):
        for tf in ("tests/test_x.py", "tests/acceptance/test_x.py", "core/test_x.py", "tests/core/sub/../test_x.py",
                   "tests/core/x.py", "tests/core/test_x.txt", "/tests/core/test_x.py", "tests/core/test_*.py"):
            t = good(test_files=[tf], test_cmd=f"python -m unittest {tf}")
            self.assertTrue(self.problems(t), tf)

    def test_every_field_is_type_checked(self):
        bad_values = {
            "id": [None, 7, "", "has space", "x" * 41, ["T1"]],
            "title": [None, 3, "", "   ", "x" * 201],
            "section": [None, 3, "", ["text"]],
            "covers": [None, "3.7", [], [3.7], ["9.9"], ["3.7", "3.7"]],
            "files_in_scope": [None, "core/x.py", [], [1], ["../x.py"], ["/abs.py"], ["C:/x.py"], ["tests/core/x.py"],
                               [""], ["\\\\server\\x.py"]],
            "test_files": [None, "tests/core/test_cov9.py", [], [1]],
            "test_cmd": [None, 1, "", "python -m unittest tests/core/other.py", "pytest tests/core/test_cov9.py",
                         "python -m unittest tests/core/test_cov9.py && rm -rf /",
                         "python3 -m unittest tests/core/test_cov9.py"],
            "needs": ["github", [1], ["Bad Name"], ["x"] * 11],
        }
        for key, values in bad_values.items():
            for v in values:
                self.assertTrue(self.problems(good(**{key: v})), f"{key}={v!r}")

    def test_missing_and_unknown_keys(self):
        for key in manager.REQUIRED:
            t = good()
            del t[key]
            self.assertTrue(self.problems(t), key)
        self.assertTrue(self.problems(good(status="done")))
        self.assertTrue(self.problems(good(kind="plan")))

    def test_test_cmd_must_run_all_of_its_test_files(self):
        t = good(test_files=["tests/core/test_a.py", "tests/core/test_b.py"],
                 test_cmd="python -m unittest tests/core/test_a.py")
        self.assertTrue(self.problems(t))
        t["test_cmd"] = "python -m unittest tests/core/test_a.py tests/core/test_b.py"
        self.assertEqual(self.problems(t), [])

    def test_ids_must_be_new_and_unique(self):
        self.assertTrue(self.problems(good(), existing={"T9"}))
        self.assertTrue(self.problems(good(), good(title="other")))
        self.assertEqual(self.problems(good(), good(id="T10", test_files=["tests/core/test_c.py"],
                                                    test_cmd="python -m unittest tests/core/test_c.py")), [])

    def test_test_file_shared_between_two_new_tasks_is_rejected(self):
        self.assertTrue(self.problems(good(), good(id="T10")))

    def test_input_is_not_mutated(self):
        t = good(files_in_scope=["core\\coverage.py"])
        before = copy.deepcopy(t)
        self.check(t)
        self.assertEqual(t, before)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "state"
        root.mkdir()
        (root / "roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
        self.led = Ledger(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ledger_rows_come_from_the_ledger(self):
        complete(self.led, "T1")
        self.led.apply({"proposal_id": "create-T2", "action": "create", "contract_id": "T2", "payload": {
            "title": "second", "spec_ref": "T2", "acceptance": "python -m unittest x", "files_in_scope": ["y.py"],
            "max_attempts": 3, "token_budget": 1000}}, "forge-manager")
        rows = manager.ledger_rows(self.led)
        self.assertEqual([(r["id"], r["status"], r["completed"]) for r in rows],
                         [("T1", "done", True), ("T2", "open", False)])
        self.assertEqual(manager.ledger_rows(Ledger(Path(self.tmp.name) / "none")), [])

    def test_prompt_holds_exactly_rules_spec_coverage_ledger_and_reasons(self):
        complete(self.led, "T1")
        cov = coverage.compute(REQS, [{"id": "T1", "kind": "build", "status": "done", "covers": ["3.7"]}], {"T1"})
        p = manager.build_prompt("SPEC TEXT HERE", cov, manager.ledger_rows(self.led), ["no gain in 3 merges"])
        self.assertTrue(p.startswith(manager.MANAGER_TEXT))
        for part in ("SPEC TEXT HERE", "3.7 [covered]", "4 [unclaimed]", "T1", "no gain in 3 merges",
                     "LEDGER", "SPEC", "COVERAGE", "tests/core/", "python -m unittest"):
            self.assertIn(part, p)

    def test_prompt_is_bounded(self):
        cov = coverage.compute(REQS, [], set())
        rows = [{"id": f"T{i}", "title": "x" * 300, "spec_ref": "s", "status": "open", "attempts": 0,
                 "completed": False} for i in range(2000)]
        p = manager.build_prompt("s" * 500000, cov, rows, ["r" * 5000] * 50)
        self.assertLess(len(p), 200000)


if __name__ == "__main__":
    unittest.main()
