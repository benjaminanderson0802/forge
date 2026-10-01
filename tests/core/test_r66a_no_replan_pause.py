"""R66a: exhausted re-plans notify Ben while runnable work continues.

Real conductor, drift state, git and test stages; agents and mail are local fakes.
"""
import json
import unittest

from core import drift

try:
    from tests.core.test_planning_drift import DriftHarness, btask, proposal
except ImportError:  # pragma: no cover - unittest discovery from tests/core
    from test_planning_drift import DriftHarness, btask, proposal


class NoReplanPauseTests(DriftHarness):
    def assert_notice_without_pause(self, trigger, *details):
        # Separate failures make the red run show every missing R66a guarantee.
        with self.subTest(guarantee="no automatic pause"):
            self.assertFalse((self.state / "PAUSED").exists())
        with self.subTest(guarantee="no open replan question"):
            path = self.state / "questions.json"
            questions = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            self.assertFalse([q for q in questions.values()
                              if q.get("kind") == "replan" and q.get("status") == "open"])
        with self.subTest(guarantee="one informational mail"):
            notices = [(s, b) for s, b in self.mails if s.startswith("[Forge] FYI:")]
            self.assertEqual(len(notices), 1, self.mails)
            body = notices[0][1]
            for detail in details:
                self.assertIn(detail, body)
            self.assertIn("continu", body.lower())
            self.assertIn("supervisor", body.lower())
            self.assertNotIn("reply with", body.lower())
            self.assertNotIn("please reply", body.lower())
        with self.subTest(guarantee="trigger logged"):
            path = self.state / "errors.log"
            self.assertTrue(path.exists())
            self.assertIn(trigger, path.read_text(encoding="utf-8"))

    def assert_window_restarted(self, c):
        with self.subTest(guarantee="pending replan and stall cleared"):
            self.assertIsNone(self.dstate()["replan"])
            self.assertIsNone(self.dstate()["stall"])
        with self.subTest(guarantee="fresh stall window"):
            d = self.dstate()
            self.assertEqual(d["auto_replans"], 0)
            self.assertEqual(d["no_gain"], 0)
            self.assertEqual(d["active_mark"], c._activity().total())

    def assert_next_task_runs(self, c):
        with self.subTest(guarantee="next step runs runnable work"):
            before = len(c.team.test_writer.prompts)
            self.assertEqual(self.task_rec("T2")["status"], "todo")
            self.assertEqual(c.step(), "worked")
            self.assertEqual(len(c.team.test_writer.prompts), before + 1)
            self.assertEqual(self.task_rec("T2")["status"], "tests_ok")

    def test_manager_proposals_rejected_twice_notify_and_keep_building(self):
        self.keeper = {"status": "replan", "reasons": ["coverage is off course"]}
        c = self.conductor(btask("T1", ["1.1"]), btask("T2", ["2.1"]))
        self.finish(c, "T1")
        self.assertIsNotNone(self.dstate()["replan"])
        # Nonzero counters prove escalation actually restarts the window.
        d = self.dstate()
        d["auto_replans"], d["no_gain"] = 1, 2
        drift.save(self.state, d)
        c._activity().add(123)
        bad1, bad2 = proposal("M1"), proposal("M2")
        bad1["tasks"][0]["test_files"] = ["tests/test_m1.py"]
        bad1["tasks"][0]["test_cmd"] = "python -m unittest tests/test_m1.py"
        bad2["tasks"][0]["covers"] = "1.2"
        self.manager_answers = [bad1, bad2]
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.dstate()["replan"]["attempts"], 1)
        first_rejection = self.dstate()["replan"]["notes"][0]
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(self.manager_prompts), 2)
        self.assertEqual([t["id"] for t in self.queue()["tasks"]], ["T1", "T2"])
        self.assert_notice_without_pause("drift keeper", "coverage is off course",
                                         first_rejection, "covers")
        self.assert_window_restarted(c)
        self.assert_next_task_runs(c)
        self.assertEqual(len(self.manager_prompts), 2)

    def test_three_unusable_drift_results_notify_once_and_release_work(self):
        c = self.conductor(btask("T1", ["1.1"]), btask("T2", ["2.1"]))
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")
        self.assertTrue(self.queue()["drift_due"])
        # Count the merge before seeding: its coverage gain resets this counter.
        c._drift_bookkeeping(self.queue())
        d = self.dstate()
        d["auto_replans"] = 2
        drift.save(self.state, d)
        c.team.drift_keeper.script = lambda p, cwd: ("not json", 1)
        for failures in (1, 2):
            self.assertEqual(c.step(), "worked")
            self.assertEqual(self.queue()["drift_failures"], failures)
            self.assertTrue(self.queue()["drift_due"])
            self.assertEqual(self.mails, [])
            if failures == 1:
                # Bookkeeping has counted T1's merge; now a real active-time
                # stall must also be consumed by the unusable-output fallback.
                c._activity().add(7201)
        self.assertEqual(self.dstate()["stall"]["trigger"], "no merge in 2 active hours")
        self.assertEqual(self.dstate()["auto_replans"], 2)
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.drift_keeper.prompts), 3)
        self.assert_notice_without_pause("no merge in 2 active hours", "3")
        self.assert_window_restarted(c)
        with self.subTest(guarantee="failed drift gate released"):
            self.assertFalse(self.queue()["drift_due"])
        self.assert_next_task_runs(c)
        # No new merge: the unchanged bad keeper must not run or mail again.
        self.assertEqual(len(c.team.drift_keeper.prompts), 3)
        with self.subTest(guarantee="no repeat notice"):
            self.assertEqual(sum(s.startswith("[Forge] FYI:") for s, _ in self.mails), 1)

    def test_replan_without_manager_notifies_and_keeps_building(self):
        self.keeper = {"status": "replan", "reasons": ["missing coverage for beta"]}
        c = self.conductor(btask("T1", ["1.1"]), btask("T2", ["2.1"]), manager=False)
        self.assertIsNone(c.manager)
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")
        self.assertTrue(self.queue()["drift_due"])
        c._drift_bookkeeping(self.queue())
        d = self.dstate()
        d["auto_replans"] = 2
        drift.save(self.state, d)
        self.assertEqual(self.dstate()["auto_replans"], 2)
        self.finish(c, "T1")
        self.assert_notice_without_pause("drift keeper", "missing coverage for beta")
        self.assert_window_restarted(c)
        self.assert_next_task_runs(c)

    def test_bens_paused_file_still_stops_runnable_work(self):
        c = self.conductor(btask("T2", ["2.1"]))
        paused = self.state / "PAUSED"
        paused.write_text("Ben paused this lane\n", encoding="utf-8")
        before = self.queue()
        for _ in range(2):
            self.assertEqual(c.step(), "paused")
        self.assertEqual(paused.read_text(encoding="utf-8"), "Ben paused this lane\n")
        self.assertEqual(self.queue(), before)
        self.assertFalse(any(a.prompts for a in vars(self.team).values()))
        self.assertEqual(self.manager_prompts, [])
        self.assertEqual(self.mails, [])
        paused.unlink()  # Ben resumes; the same task is still runnable.
        self.assert_next_task_runs(c)


if __name__ == "__main__":
    unittest.main()
