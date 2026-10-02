"""L1C_3: drift checks are read-only and re-plans need useful reasons."""
import json
import subprocess
import unittest

from core.agents import ClaudeAgent
from core.bootstrap import real_team
from tests.core.test_planning_drift import DriftHarness


class DriftKeeperTests(DriftHarness):
    def ready(self, manager=True):
        task = {
            "id": "T1", "kind": "build", "title": "Build T1",
            "section": "Make m_t1.VALUE equal 1.",
            "files_in_scope": ["m_t1.py"],
            "test_files": ["tests/core/test_t1.py"],
            "test_cmd": "python -m unittest tests/core/test_t1.py",
            "covers": ["1.1"],
        }
        c = self.conductor(task, manager=manager)
        c.step()
        c.step()
        self.assertEqual(self.task_rec("T1")["status"], "done")
        self.assertTrue(self.queue()["drift_due"])
        self.assertEqual(self.keeper_prompts, [])
        return c

    def assert_replan_question(self, text):
        self.assertTrue((self.state / "PAUSED").exists())
        questions = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))
        matching = [q for q in questions.values()
                    if q["kind"] == "replan" and text in q["body"]]
        self.assertTrue(matching, questions)
        self.assertTrue(any(text in body for _, body in self.mails), self.mails)

    def test_keeper_reads_layer_head_in_disposable_checkout_with_full_prompt(self):
        c = self.ready()
        tip = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=c.wt, text=True).strip()
        seen = []

        def keeper(prompt, cwd):
            seen.append((cwd, prompt, subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip(),
                (cwd / "m_t1.py").read_text(encoding="utf-8")))
            return '{"status":"ok"}', 1

        c.team.drift_keeper.script = keeper
        c.step()
        self.assertEqual(len(seen), 1)
        cwd, prompt, keeper_tip, source = seen[0]
        self.assertEqual(keeper_tip, tip)
        self.assertIn("VALUE = 1", source)
        self.assertIn("COVERAGE", prompt)
        self.assertIn("DESIGN:", prompt)
        self.assertNotEqual(cwd.resolve(), c.wt.resolve())
        self.assertFalse(cwd.exists())
        self.assertFalse(self.queue()["drift_due"])

    def test_writing_keeper_is_rejected_three_times_then_pauses_and_emails(self):
        c = self.ready()
        seen = []

        def keeper(prompt, cwd):
            seen.append(cwd)
            (cwd / "drift-note.txt").write_text("unsolicited note", encoding="utf-8")
            return '{"status":"ok"}', 1

        c.team.drift_keeper.script = keeper
        for attempt in range(1, 4):
            c.step()
            with self.subTest(attempt=attempt):
                self.assertFalse((c.wt / "drift-note.txt").exists())
                self.assertTrue(self.queue()["drift_due"])
                self.assertEqual(self.queue()["drift_failures"], attempt)
                self.assertIsNone(self.dstate()["replan"])
                self.assertFalse(seen[-1].exists())
        self.assertEqual(len(seen), 3)
        self.assert_replan_question("read-only")
        log = (self.state / "errors.log").read_text(encoding="utf-8")
        self.assertIn("drift keeper is read-only but changed: drift-note.txt", log)

    def test_replan_without_reasons_is_retried_then_escalated(self):
        c = self.ready()
        answers = [
            {"status": "replan", "reasons": []},
            {"status": "replan", "reasons": ["  "]},
            {"status": "replan"},
        ]
        for attempt, answer in enumerate(answers, 1):
            self.keeper = answer
            c.step()
            with self.subTest(answer=answer):
                self.assertEqual(len(self.keeper_prompts), attempt)
                self.assertIsNone(self.dstate()["replan"])
                self.assertTrue(self.queue()["drift_due"])
                self.assertEqual(self.queue()["drift_failures"], attempt)
                self.assertEqual(self.manager_prompts, [])
        self.assert_replan_question("replan without reasons")

    def test_nonblank_reason_creates_replan_and_clears_due(self):
        c = self.ready()
        self.keeper = {"status": "replan", "reasons": ["  ", "off course"]}
        c.step()
        self.assertIn("off course", self.dstate()["replan"]["reasons"])
        self.assertFalse(self.queue()["drift_due"])
        self.assertEqual(self.queue()["drift_failures"], 0)

    def test_string_reason_remains_accepted(self):
        c = self.ready()
        self.keeper = {"status": "replan", "reasons": "design drift"}
        c.step()
        self.assertEqual(self.dstate()["replan"]["reasons"], ["design drift"])
        self.assertFalse(self.queue()["drift_due"])

    def test_clean_ok_clears_due_and_resets_previous_failure(self):
        c = self.ready()
        c.team.drift_keeper.script = lambda p, cwd: ("not json", 1)
        c.step()
        self.assertEqual(self.queue()["drift_failures"], 1)
        self.assertTrue(self.queue()["drift_due"])
        c.team.drift_keeper.script = self.drift_keeper
        c.step()
        self.assertEqual(len(self.keeper_prompts), 1)
        self.assertFalse(self.queue()["drift_due"])
        self.assertEqual(self.queue()["drift_failures"], 0)
        self.assertIsNone(self.dstate()["replan"])

    def test_replan_without_manager_pauses_and_asks_ben(self):
        c = self.ready(manager=False)
        self.keeper = {"status": "replan", "reasons": ["off course"]}
        c.step()
        self.assert_replan_question("off course")


class DriftKeeperConfigurationTests(unittest.TestCase):
    def test_real_keeper_is_claude_with_only_read_tools(self):
        keeper = real_team({"agent_timeout_s": 60}).drift_keeper
        self.assertIsInstance(keeper, ClaudeAgent)
        self.assertEqual(keeper.permission_mode, "plan")
        self.assertEqual(keeper.allowed_tools, ["Read", "Glob", "Grep"])


if __name__ == "__main__":
    unittest.main()
