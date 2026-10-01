"""R59: a task's judges skip the tests of other layer tasks that aren't built yet."""
import tempfile
import unittest
from pathlib import Path

from core import bootstrap, suite

try:
    from tests.core.test_bootstrap import Harness
except ImportError:
    from test_bootstrap import Harness


class ExcludeTests(unittest.TestCase):
    def root(self):
        r = Path(tempfile.mkdtemp())
        (r / "tests" / "core").mkdir(parents=True)
        for p in (r / "tests", r / "tests" / "core"):
            (p / "__init__.py").write_text("", encoding="utf-8")
        (r / "tests/core/test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n", encoding="utf-8")
        (r / "tests/core/test_later.py").write_text(
            "import unittest\nimport not_built_yet\n", encoding="utf-8")
        return r

    def test_excluded_module_does_not_run(self):
        r = self.root()
        self.assertEqual(suite.main(["--root", str(r)]), 1)
        self.assertEqual(suite.main(["--root", str(r), "--exclude", "tests/core/test_later.py"]), 0)
        self.assertEqual(suite.main(["--root", str(r), "--exclude", "tests\\core\\test_later.py"]), 0)

    def test_a_failing_module_that_is_not_excluded_still_fails(self):
        r = self.root()
        self.assertEqual(suite.main(["--root", str(r), "--exclude", "tests/core/test_other.py"]), 1)

    def test_judge_command_excludes_others_but_never_the_tasks_own_tests(self):
        cmds = bootstrap.task_judge_cmds(["python drills/run_drills.py", "python -m core.suite"], "a", "b",
                                         ["tests/core/test_mine.py"],
                                         ["tests/core/test_later.py", "tests/core/test_mine.py"])
        self.assertEqual(cmds[0], "python drills/run_drills.py")
        self.assertIn("--exclude tests/core/test_later.py", cmds[1])
        self.assertNotIn("--exclude tests/core/test_mine.py", cmds[1])
        self.assertIn("--include tests/core/test_mine.py", cmds[1])


class UnbuiltTests(Harness):
    def test_only_other_unfinished_build_tasks_are_listed(self):
        c = self.init(self.task(id="T1", test_files=["tests/core/test_a.py"], test_cmd=bootstrap.py_test("tests/core/test_a.py") if hasattr(bootstrap, "py_test") else "python -m unittest tests/core/test_a.py"),
                      self.task(id="T2", test_files=["tests/core/test_b.py"], test_cmd="python -m unittest tests/core/test_b.py"),
                      self.task(id="T3", test_files=["tests/core/test_c.py"], test_cmd="python -m unittest tests/core/test_c.py"))
        q = c._queue()
        q["tasks"][2]["status"] = "done"
        c._save_queue(q)
        self.assertEqual(c._unbuilt_test_files("T1"), ["tests/core/test_b.py"])
        self.assertEqual(sorted(c._unbuilt_test_files("T3")), ["tests/core/test_a.py", "tests/core/test_b.py"])


if __name__ == "__main__":
    unittest.main()


class SpecFileTests(Harness):
    def test_lane_queue_names_its_own_design(self):
        c = self.init()
        self.assertEqual(c._spec_rel(), "docs/specs/layer-1-design.md")
        q = c._queue()
        q["spec_file"] = "docs/specs/phase-2-design.md"
        c._save_queue(q)
        self.assertEqual(c._spec_rel(), "docs/specs/phase-2-design.md")


if __name__ == "__main__":
    unittest.main()
class ReviewRound1Tests(unittest.TestCase):
    def test_deleted_module_forces_full_suite(self):
        r = Path(tempfile.mkdtemp())
        (r / "tests" / "core").mkdir(parents=True)
        (r / "tests/core/test_x.py").write_text("import unittest\n", encoding="utf-8")
        picked, why = suite.select(r, ["core/feature.py"])
        self.assertIsNone(picked)
        self.assertIn("deleted module", why)


class SyncReviewTests(Harness):
    def test_no_sync_while_a_gate_pr_is_open(self):
        c = self.init()
        c._ask("gate", "Layer ready", "approve?")
        self.assertIn("gate pull request", c._sync_busy(c._queue(), c.wt))

    def test_unregistered_sync_merge_at_head_is_repaired(self):
        import subprocess
        from core.finalize import ApprovedMerges
        c = self.init()
        wt, layer = c.wt, c._queue()["layer"]
        base = bootstrap._git(wt, "rev-parse", "HEAD")
        bootstrap._git(wt, "checkout", "-q", "-b", "side")
        (wt / "side.txt").write_text("x\n", encoding="utf-8")
        bootstrap._git(wt, "add", "side.txt")
        bootstrap._git(wt, "commit", "-q", "-m", "side")
        bootstrap._git(wt, "checkout", "-q", layer)
        subprocess.run(["git", "-c", "user.name=Forge", "-c", "user.email=forge@localhost", "merge", "--no-ff",
                        "-q", "-m", f"Sync {layer} with main", "side"], cwd=str(wt), check=True)
        head = bootstrap._git(wt, "rev-parse", "HEAD")
        self.assertFalse(ApprovedMerges(c.state).has(head))
        c._repair_sync_approval(wt, layer, head)
        self.assertTrue(ApprovedMerges(c.state).has(head))
        c._repair_sync_approval(wt, layer, base)  # not a sync merge: nothing registered
        self.assertFalse(ApprovedMerges(c.state).has(base))
