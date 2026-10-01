"""R57: per-task judges run only the tests a change can affect; the full suite runs at the layer gate."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from core import suite
from core.bootstrap import task_judge_cmds

try:
    from tests.core.test_bootstrap import Harness, git
except ImportError:
    from test_bootstrap import Harness, git

PASS = "import unittest\n{imports}\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"


def make_repo(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def sample(root: Path) -> None:
    make_repo(root, {
        "core/__init__.py": "",
        "core/a.py": "VALUE = 1\n",
        "core/b.py": "from core.a import VALUE\n",
        "core/c.py": "VALUE = 3\n",
        "core/d.py": "from . import b\n",  # relative import: d -> b -> a
        "core/bootstrap.py": "",
        "core/ledger.py": "", "core/agents.py": "", "core/protect.py": "",
        "drills/run_drills.py": "",
        "tests/__init__.py": "", "tests/core/__init__.py": "",
        "tests/core/test_a.py": PASS.format(imports="import core.a"),
        "tests/core/test_b.py": PASS.format(imports="from core import b"),
        "tests/core/test_c.py": PASS.format(imports="from core.c import VALUE"),
        "tests/core/test_d.py": PASS.format(imports="import core.d"),
        "tests/core/test_e.py": PASS.format(imports="from tests.core.test_c import T as _C"),
        "tests/core/test_s.py": PASS.format(imports="import subprocess\nMOD = 'core.a'"),
        "docs/notes.md": "notes\n",
    })


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sample(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_graph_selects_direct_and_transitive_importers_only(self):
        picked, _why = suite.select(self.root, ["core/a.py"])
        # test_a imports a; test_b imports b (which imports a); test_d imports d -> b -> a (relative import);
        # test_s names the module as a string (e.g. "-m core.a"). test_c and test_e never reach core.a.
        self.assertEqual(picked, ["test_a", "test_b", "test_d", "test_s"])
        picked, _why = suite.select(self.root, ["core/c.py"])
        self.assertEqual(picked, ["test_c", "test_e"])  # test_e imports test_c, which imports core.c

    def test_changed_test_modules_and_task_tests_are_included(self):
        self.assertEqual(suite.select(self.root, ["tests/core/test_c.py"])[0], ["test_c", "test_e"])
        self.assertEqual(suite.select(self.root, ["core/c.py"], include=["tests/core/test_a.py"])[0],
                         ["test_a", "test_c", "test_e"])

    def test_docs_select_nothing_but_unknown_data_runs_everything(self):
        self.assertEqual(suite.select(self.root, ["docs/notes.md"])[0], [])
        self.assertIsNone(suite.select(self.root, ["charter/new_limits.json"])[0])

    def test_high_blast_radius_changes_run_the_full_suite(self):
        for path in ("core/bootstrap.py", "core/ledger.py", "core/agents.py", "core/protect.py",
                     "drills/run_drills.py", "drills/new_drill.py"):
            picked, why = suite.select(self.root, ["core/c.py", path])
            self.assertIsNone(picked, path)
            self.assertIn("high-blast", why)

    def test_fast_list_is_honoured(self):
        (self.root / suite.FAST_FILE).write_text("# header\ntest_c  1.0\ntest_gone  0.5\n", encoding="utf-8")
        self.assertEqual(suite.read_fast(self.root), ["test_c", "test_gone"])
        self.assertEqual(suite.select(self.root, ["docs/notes.md"])[0], ["test_c"])  # unknown names dropped
        self.assertEqual(suite.select(self.root, ["core/a.py"])[0], ["test_a", "test_b", "test_c", "test_d", "test_s"])


class SuiteCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sample(self.root)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "T")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, rel, text):
        (self.root / rel).write_text(text, encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", f"change {rel}")
        return git(self.root, "rev-parse", "HEAD")

    def run_suite(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = suite.main(["--root", str(self.root), "--jobs", "2", *args])
        out = buf.getvalue()
        ran = sorted(ln.split()[1].rstrip(":") for ln in out.splitlines() if ln.startswith(("ok  ", "FAIL ")))
        return code, ran, out

    def test_changed_range_runs_selected_and_fast_modules(self):
        base = self.commit(suite.FAST_FILE, "test_e  0.2\n")
        sha = self.commit("core/c.py", "VALUE = 4\n")
        code, ran, out = self.run_suite("--changed", f"{base}..{sha}", "--include", "tests/core/test_a.py")
        self.assertEqual(code, 0, out)
        self.assertEqual(ran, ["test_a", "test_c", "test_e"])

    def test_high_blast_change_runs_every_module(self):
        sha = self.commit("core/ledger.py", "X = 1\n")
        code, ran, out = self.run_suite("--changed", f"{self.base}..{sha}")
        self.assertEqual(code, 0, out)
        self.assertEqual(ran, suite.modules(self.root))
        self.assertIn("full suite", out)

    def test_unreadable_range_fails_safe_to_the_full_suite(self):
        code, ran, out = self.run_suite("--changed", "nonexistent..alsonot")
        self.assertEqual(ran, suite.modules(self.root))

    def test_write_fast_lists_only_quick_passing_modules(self):
        make_repo(self.root, {
            "tests/core/test_slow.py": "import time, unittest\nclass T(unittest.TestCase):\n"
                                       "    def test_s(self): time.sleep(3)\n",
            "tests/core/test_bad.py": "import unittest\nclass T(unittest.TestCase):\n"
                                      "    def test_b(self): self.fail('no')\n"})
        with contextlib.redirect_stdout(io.StringIO()):
            suite.write_fast(self.root, jobs=2, timeout_s=60, limit_s=2.5)
        fast = suite.read_fast(self.root)
        self.assertIn("test_a", fast)
        self.assertNotIn("test_slow", fast)
        self.assertNotIn("test_bad", fast)
        self.assertIn("--write-fast", (self.root / suite.FAST_FILE).read_text(encoding="utf-8"))

    def test_the_repo_has_a_fast_list(self):
        fast = suite.read_fast(Path(__file__).resolve().parents[2])
        self.assertTrue(fast)
        self.assertTrue(all(n.startswith("test_") for n in fast))


class JudgeCommandTests(Harness):
    def test_only_suite_commands_get_the_range_and_task_tests(self):
        cmds = task_judge_cmds(["python drills/run_drills.py", "python -m core.suite", "python -m core.suite_x"],
                               "abc", "def", ["tests/core/test_feat.py", "tests/acceptance/test_x.py", "tests/core/x y.py"])
        self.assertEqual(cmds, ["python drills/run_drills.py",
                                "python -m core.suite --changed abc..def --include tests/core/test_feat.py",
                                "python -m core.suite_x"])

    def judge_script(self):
        log = Path(self.tmp.name) / "judge.log"
        script = Path(self.tmp.name) / "judge.py"
        script.write_text(
            "import subprocess, sys\n"
            "head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
            "with open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
            "    f.write(head + '|' + ' '.join(sys.argv[2:]) + '\\n')\n"
            "sys.exit(int(open(sys.argv[1] + '.exit').read()) if __import__('os').path.exists(sys.argv[1] + '.exit') else 0)\n",
            encoding="utf-8")
        return log, f'"{sys.executable}" "{script}" "{log}" -m core.suite'

    @staticmethod
    def until_gate(c, limit=5):
        status = ""
        for _ in range(limit):  # the drift check after a merge may come first
            status = c.step()
            if status == "gate":
                break
        return status

    def test_task_judge_gets_base_dot_dot_sha_and_gate_runs_the_full_suite(self):
        log, cmd = self.judge_script()
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.judge_cmds = [cmd]
        c.step()  # tests
        base = git(self.repo, "rev-parse", f"refs/heads/{self.layer}")
        c.step()  # build, judges, review, finalize
        self.assertEqual(c._task("T1")["status"], "done")
        sha = c._ledger().contracts()["T1"]["commit"]
        head, args = log.read_text(encoding="utf-8").splitlines()[0].split("|")
        self.assertEqual(head, sha)
        self.assertEqual(args, f"-m core.suite --changed {base}..{sha} --include tests/core/test_feat.py")
        n = len(log.read_text(encoding="utf-8").splitlines())
        self.assertEqual(self.until_gate(c), "gate")
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), n + 1)
        head, args = lines[-1].split("|")
        self.assertEqual(head, git(self.repo, "rev-parse", f"refs/heads/{self.layer}"))
        self.assertEqual(args, "-m core.suite")  # as written: the full suite
        self.assertTrue(any(a[:2] == ["pr", "create"] for a in self.gh_calls))

    def test_gate_opens_no_pr_while_the_full_suite_fails_and_runs_it_once_per_tip(self):
        log, cmd = self.judge_script()
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.judge_cmds = [cmd]
        c.step()
        c.step()
        self.assertEqual(c._task("T1")["status"], "done")
        Path(str(log) + ".exit").write_text("1", encoding="utf-8")
        n = len(log.read_text(encoding="utf-8").splitlines())
        self.assertEqual(self.until_gate(c), "gate")
        c.step()
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), n + 1)  # once for this tip
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in self.gh_calls))
        self.assertEqual(sum("full suite fails" in s for s, _b in self.mails), 1)
        self.assertFalse(c._queue()["gate_suite"]["passed"])


if __name__ == "__main__":
    unittest.main()
