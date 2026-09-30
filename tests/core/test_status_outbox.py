"""The status page never writes an answer while an agent runs (the drop folder is tamper-fingerprinted)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from core import status_page


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.svc, self.drop = self.d / "service", self.d / "channel" / "in"
        self.svc.mkdir()

    def beat(self, phase, age=0.0):
        (self.svc / "heartbeat.json").write_text(json.dumps({"at": time.time() - age, "phase": phase}),
                                                 encoding="utf-8")

    def files(self):
        return sorted(p.name for p in self.drop.glob("*.json")) if self.drop.exists() else []

    def test_held_while_a_step_runs_and_delivered_when_it_sleeps(self):
        self.beat("step")
        box = status_page.AnswerOutbox(self.drop, self.svc)
        box.add("blocked-3", "abcDEF12", "use plan B")
        self.assertEqual(self.files(), [])
        self.assertEqual(box.flush(), 0)
        self.beat("sleep")
        self.assertEqual(box.flush(), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(box.pending, [])

    def test_delivered_at_once_when_the_conductor_is_not_running(self):
        box = status_page.AnswerOutbox(self.drop, self.svc)  # no heartbeat at all
        box.add("blocked-3", "abcDEF12", "x")
        self.assertEqual(len(self.files()), 1)
        self.beat("step", age=600)  # stale heartbeat: not running
        box.add("blocked-4", "abcDEF13", "y")
        self.assertEqual(len(self.files()), 2)

    def test_startup_counts_as_running(self):
        self.beat("startup")
        self.assertFalse(status_page.conductor_between_runs(self.svc))
        self.beat("exited")
        self.assertTrue(status_page.conductor_between_runs(self.svc))

    def test_order_is_kept(self):
        self.beat("step")
        box = status_page.AnswerOutbox(self.drop, self.svc)
        for i in range(3):
            box.add(f"blocked-{i}", "abcDEF12", f"a{i}")
        self.beat("sleep")
        box.flush()
        answers = [json.loads((self.drop / n).read_text(encoding="utf-8"))["answer"] for n in self.files()]
        self.assertEqual(answers, ["a0", "a1", "a2"])


if __name__ == "__main__":
    unittest.main()
