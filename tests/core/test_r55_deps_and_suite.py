"""R55: tasks wait for their dependencies; judges run the parallel suite with a longer limit."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from core import bootstrap, suite

try:
    from tests.core.test_bootstrap import Harness
except ImportError:
    from test_bootstrap import Harness


class DependsOnTests(Harness):
    def test_dependent_task_waits_until_its_dependency_is_done(self):
        c = self.init(self.task(id="T1"), self.task(id="T2", depends_on=["T1"]))
        c._stage_unready = lambda t, cap_map: {}  # readiness is not under test here
        q = c._queue()
        picked = [t["id"] for t in c._pick_tasks(q, {})]
        self.assertNotIn("T2", picked)
        for t in q["tasks"]:
            if t["id"] == "T1":
                t["status"] = "done"
        self.assertIn("T2", [t["id"] for t in c._pick_tasks(q, {})])

    def test_waiting_on_a_dependency_is_not_a_capability_wait(self):
        c = self.init(self.task(id="T1", status="blocked"), self.task(id="T2", depends_on=["T1"]))
        c._stage_unready = lambda t, cap_map: {}
        c._waiting = False
        self.assertEqual(list(c._pick_tasks(c._queue(), {})), [])
        self.assertFalse(c._waiting)
        self.assertNotIn("waiting_on", c._task("T2"))

    def test_planner_children_keep_depends_on(self):
        self.assertIn("depends_on", bootstrap.S_PLAN["properties"]["tasks"]["items"]["properties"])


class JudgeTests(unittest.TestCase):
    def test_judges_use_the_parallel_suite_and_a_longer_limit(self):
        src = Path(bootstrap.__file__).read_text(encoding="utf-8")
        self.assertIn('"python -m core.suite"', src)
        self.assertNotIn('"python -m unittest discover -s tests/core"])', src)
        self.assertGreaterEqual(bootstrap.JUDGE_TIMEOUT_S, 1800)

    def test_suite_runs_modules_in_parallel_and_reports_failures(self):
        root = Path(tempfile.mkdtemp())
        (root / "tests" / "core").mkdir(parents=True)
        for p in (root / "tests", root / "tests" / "core"):
            (p / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests/core/test_a.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n", encoding="utf-8")
        self.assertEqual(suite.main(["--root", str(root), "--jobs", "2"]), 0)
        (root / "tests/core/test_b.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail('x')\n", encoding="utf-8")
        self.assertEqual(suite.main(["--root", str(root), "--jobs", "2"]), 1)

    def test_suite_counts_a_hung_module_as_failed(self):
        root = Path(tempfile.mkdtemp())
        (root / "tests" / "core").mkdir(parents=True)
        for p in (root / "tests", root / "tests" / "core"):
            (p / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests/core/test_slow.py").write_text(
            "import time, unittest\nclass T(unittest.TestCase):\n def test_s(self): time.sleep(30)\n", encoding="utf-8")
        self.assertEqual(suite.main(["--root", str(root), "--module-timeout", "2"]), 1)


if __name__ == "__main__":
    unittest.main()
