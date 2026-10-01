"""R63 evidence for design 2.1: Claude, read-only, schema-checked plans."""
import json
import tempfile
import unittest
from pathlib import Path

from core import drift, manager
from core.agents import ClaudeAgent, FakeAgent
from core.bootstrap import real_manager
from core.coverage import Coverage
from core.ledger import Ledger
from tests.core.test_planning_drift import DriftHarness, btask, proposal


class ManagerSchemaEvidence(unittest.TestCase):
    requirements = {"1.2": "second alpha thing", "2.1": "the beta thing"}

    def assert_rejected(self, data, existing_ids=None):
        clean, problems = manager.validate_proposal(
            data, self.requirements, existing_ids or set())
        self.assertEqual(clean, [])
        self.assertTrue(problems)

    def test_real_manager_is_claude_with_only_reading_tools(self):
        agent = real_manager({"agent_timeout_s": 60})
        self.assertIsInstance(agent, ClaudeAgent)
        self.assertEqual(agent.provider, "claude")
        self.assertEqual(agent.permission_mode, "plan")
        self.assertEqual(agent.allowed_tools, ["Read", "Glob", "Grep"])

    def test_schema_and_normalized_valid_task(self):
        schema = manager.S_MANAGER
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["tasks"])
        tasks = schema["properties"]["tasks"]
        self.assertEqual(tasks["type"], "array")
        self.assertEqual(tasks["items"]["type"], "object")
        expected = ["id", "title", "section", "covers", "files_in_scope",
                    "test_files", "test_cmd"]
        self.assertEqual(list(manager.REQUIRED), expected)
        self.assertEqual(tasks["items"]["required"], list(manager.REQUIRED))
        data = proposal("M1", covers=("2.1", "1.2"))
        task = data["tasks"][0]
        task["files_in_scope"] = ["./core\\feature.py"]
        task["test_files"] = ["./tests\\core\\test_feature.py"]
        task["test_cmd"] = "python -m unittest ./tests\\core\\test_feature.py"
        task["needs"] = ["git"]
        clean, problems = manager.validate_proposal(data, self.requirements, set())
        self.assertEqual(problems, [])
        self.assertEqual(clean, [dict(task, files_in_scope=["core/feature.py"],
                                     test_files=["tests/core/test_feature.py"])])
        self.assertEqual(clean[0]["covers"], ["2.1", "1.2"])

    def test_invalid_response_shapes_and_task_count(self):
        for data in (None, [], "plan", {}, {"tasks": []}, {"tasks": {}},
                     {"tasks": [None]},
                     proposal(*(f"M{i}" for i in range(manager.MAX_TASKS + 1)))):
            with self.subTest(data=data):
                self.assert_rejected(data)

    def test_each_required_field_is_checked(self):
        for field in manager.REQUIRED:
            with self.subTest(field=field):
                data = proposal("M1")
                del data["tasks"][0][field]
                self.assert_rejected(data)

    def test_invalid_task_fields_are_rejected(self):
        cases = [
            ("unknown_field", True), ("id", "bad id!"), ("id", 12),
            ("covers", []), ("covers", ["9.9"]),
            ("covers", ["1.2", "1.2"]), ("covers", "1.2"),
            ("title", ""), ("section", "x" * (manager.SECTION_CAP + 1)),
            ("section", 7), ("files_in_scope", ["tests/core/test_m1.py"]),
            ("files_in_scope", ["/absolute.py"]),
            ("files_in_scope", ["C:\\absolute.py"]),
            ("files_in_scope", []), ("files_in_scope", "feature.py"),
            ("test_files", []), ("test_files", "tests/core/test_m1.py"),
            ("test_cmd", "pytest tests/core/test_m1.py"),
            ("test_cmd", "python -m unittest tests/core/test_other.py"),
            ("test_cmd", "python -m unittest tests/core/test_m1.py tests/core/test_extra.py"),
            ("test_cmd", "python -m unittest "),
            ("needs", "git"), ("needs", [3]), ("needs", ["bad name!"]),
            ("needs", ["git"] * 11),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                data = proposal("M1")
                data["tasks"][0][field] = value
                self.assert_rejected(data)

    def test_test_paths_are_checked_even_when_command_matches(self):
        for path in ("tests/test_m1.py", "tests/core/helper.py",
                     "tests/core/../test_m1.py", "tests/core/test_..py"):
            with self.subTest(path=path):
                data = proposal("M1")
                data["tasks"][0].update(test_files=[path],
                                        test_cmd="python -m unittest " + path)
                self.assert_rejected(data)

    def test_existing_ids_and_shared_test_files_reject_whole_proposal(self):
        self.assert_rejected(proposal("M1"), {"M1"})
        data = proposal("M1", "M2")
        data["tasks"][1].update(test_files=data["tasks"][0]["test_files"],
                                test_cmd=data["tasks"][0]["test_cmd"])
        self.assert_rejected(data)

    def test_prompt_contains_all_four_inputs(self):
        spec = "## 2. Manager\n- Claude, read-only; writes nothing."
        cov = Coverage(self.requirements, {"1.2": ["LEDGER_A"]}, {})
        rows = [{"id": cid, "title": "A contract", "spec_ref": "2.1",
                 "status": "open", "attempts": 0, "completed": False}
                for cid in ("LEDGER_A", "LEDGER_B")]
        reasons = ["Unclaimed beta requirement", "No coverage gain"]
        prompt = manager.build_prompt(spec, cov, rows, reasons)
        for text in (spec, cov.report(), *(r["id"] for r in rows), *reasons):
            self.assertIn(text, prompt)

    def test_ledger_completion_requires_a_pass_event_not_cached_done_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = {"mgr": "manager", "exe": "executor", "ci": "ci",
                     "aud": "auditor", "forge-core": "core"}
            (root / "roles.json").write_text(json.dumps(roles), encoding="utf-8")
            ledger = Ledger(root)

            def apply(cid, action, identity, payload=None):
                ledger.apply({"proposal_id": f"{cid}-{action}", "action": action,
                              "contract_id": cid, "payload": payload or {}}, identity)

            for cid in ("PASSED", "CACHE_ONLY"):
                apply(cid, "create", "mgr", {
                    "title": cid, "spec_ref": "2.1", "acceptance": "python -m unittest",
                    "files_in_scope": ["feature.py"], "max_attempts": 3, "token_budget": 1000})
            apply("PASSED", "claim", "exe")
            apply("PASSED", "submit", "exe", {"commit": "a" * 40})
            apply("PASSED", "test_run", "ci", {
                "run_id": "ci-pass", "commit": "a" * 40, "passed": True})
            apply("PASSED", "run_report", "forge-core", {
                "run_id": "attempt", "claim": "done", "commit": "a" * 40,
                "changed": ["feature.py"], "violations": [], "out_of_scope": []})
            self.assertFalse(any(row["completed"] for row in manager.ledger_rows(ledger)))
            apply("PASSED", "pass", "aud", {"run_id": "ci-pass"})
            cache = ledger.contracts()
            cache["CACHE_ONLY"]["status"] = "done"
            ledger.contracts_path.write_text(json.dumps(cache), encoding="utf-8")
            rows = {row["id"]: row for row in manager.ledger_rows(ledger)}
            self.assertEqual(set(rows), {"PASSED", "CACHE_ONLY"})
            self.assertTrue(rows["PASSED"]["completed"])
            self.assertEqual(rows["CACHE_ONLY"]["status"], "done")
            self.assertFalse(rows["CACHE_ONLY"]["completed"])


class ManagerConductorEvidence(DriftHarness):
    def pending_replan(self):
        conductor = self.conductor(btask("T1", ["1.1"]))
        self.assertIsInstance(conductor.manager, FakeAgent)
        state = drift.adopt([], set(), False, 0)
        rid = drift.new_replan(state, ["Cover remaining requirements"], "evidence test")
        drift.save(self.state, state)
        return conductor, rid

    def test_writing_manager_is_rejected_and_throwaway_is_removed(self):
        conductor, rid = self.pending_replan()
        before = self.queue()["tasks"]

        def writes(prompt, cwd):
            (cwd / "manager_write.py").write_text("VALUE = 1\n", encoding="utf-8")
            return json.dumps(proposal("M1")), 1

        self.manager_answers = [writes]
        conductor.step()
        pending = self.dstate()["replan"]
        self.assertEqual(pending["id"], rid)
        self.assertTrue(any("read-only" in note for note in pending["notes"]))
        self.assertEqual(self.queue()["tasks"], before)
        self.assertNotIn(rid, self.queue().get("replans", []))
        self.assertFalse((conductor.wt / "manager_write.py").exists())
        self.assertEqual(len(self.manager_cwds), 1)
        self.assertNotEqual(self.manager_cwds[0].resolve(), conductor.wt.resolve())
        self.assertFalse(self.manager_cwds[0].exists())

    def test_valid_manager_proposal_is_appended_with_coverage_and_replan_id(self):
        conductor, rid = self.pending_replan()
        self.manager_answers = [proposal("M1")]
        self.assertEqual(conductor.step(), "worked")
        queue = self.queue()
        self.assertEqual([task["id"] for task in queue["tasks"]], ["T1", "M1"])
        self.assertEqual(queue["tasks"][-1]["covers"], ["1.2"])
        self.assertEqual(queue["tasks"][-1]["status"], "todo")
        self.assertIn(rid, queue["replans"])
        self.assertIsNone(self.dstate()["replan"])


if __name__ == "__main__":
    unittest.main()
