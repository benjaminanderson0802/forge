"""T1B1d: Stage A tests must also fail on an empty implementation (R4 applied to both runs)."""
import json
import py_compile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import core.bootstrap as bootstrap
from tests.core.test_bootstrap import Harness, git


def write_test(cwd, body):
    path = cwd / "tests/core/test_feat.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return '{"files":["tests/core/test_feat.py"]}', 1


IMPORT_ONLY = ("import unittest\nimport feat\n"
               "class T(unittest.TestCase):\n"
               " def test_module(self): self.assertTrue(hasattr(feat, '__name__'))\n")
STUB_PASSES = ("import unittest\nimport feat\n"
               "class T(unittest.TestCase):\n"
               " def test_value(self):\n"
               "  self.assertTrue(feat.value() is None or feat.value() == 4244)\n")
SLEEP_ON_NONE = ("import time, unittest\nimport feat\n"
                 "class T(unittest.TestCase):\n"
                 " def test_value(self):\n"
                 "  if feat.value() is None:\n"
                 "   time.sleep(5)\n"
                 "  self.assertEqual(feat.value(), 4244)\n")
STRONG = ("import unittest\nimport feat\n"
          "class T(unittest.TestCase):\n"
          " def test_value(self): self.assertEqual(feat.value(), 4244)\n")
# Same length as the stub `return None`, so a checked .pyc could only be told apart by mtime.
MODULE = "def value(): return 4243\n"


class WeakEmptyTests(Harness):
    def queue_task(self):
        return json.loads((self.state / "queue.json").read_text(encoding="utf-8"))["tasks"][0]

    def commit_on_layer(self, c, files):
        for rel, text in files.items():
            p = c.wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
        git(c.wt, "add", "-A")
        git(c.wt, "commit", "-q", "-m", "setup")
        return git(c.wt, "rev-parse", "HEAD")

    def assert_clean(self, c):
        self.assertEqual(git(c.wt, "status", "--porcelain", "-uall"), "")

    # (a)
    def test_a_import_only_test_passes_on_empty_file_and_is_rejected(self):
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, IMPORT_ONLY)})
        head = git(c.wt, "rev-parse", "HEAD")
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertEqual(t["status"], "todo")
        self.assertTrue(t["notes"][-1].startswith("tests rejected: weak (they pass on an empty implementation)"),
                        t["notes"])
        self.assertEqual(t["test_rejects"], 1)
        self.assertFalse((c.wt / "feat.py").exists())
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)
        self.assertNotIn("tests/core/test_feat.py", self.branch_files())
        self.assert_clean(c)

    # (b)
    def test_b_tracked_module_stub_passes_rejected_and_module_restored_exactly(self):
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, STUB_PASSES)})
        head = self.commit_on_layer(c, {"feat.py": MODULE})
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertEqual(t["status"], "todo")
        self.assertTrue(t["notes"][-1].startswith("tests rejected: weak (they pass on an empty implementation)"),
                        t["notes"])
        self.assertEqual((c.wt / "feat.py").read_bytes(), MODULE.encode("utf-8"))
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)
        self.assert_clean(c)
        # No bytecode was written into the worktree, so a later restored-code run sees the real module.
        self.assertFalse(list(c.wt.rglob("*.pyc")))
        out = subprocess.run([sys.executable, "-c", "import feat; print(feat.value())"], cwd=str(c.wt),
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        self.assertEqual(out, "4243")

    def test_b_crlf_module_restored_byte_for_byte(self):
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, STUB_PASSES)})
        git(c.wt, "config", "core.autocrlf", "false")
        text = "def value():\r\n    return 4243\r\n"
        self.commit_on_layer(c, {"feat.py": text})
        c.step()
        self.assertEqual((c.wt / "feat.py").read_bytes(), text.encode("utf-8"))
        self.assert_clean(c)

    def test_b_restore_is_byte_exact_under_autocrlf(self):
        """Integration fix C: with core.autocrlf=true (Git for Windows' default) a git checkout of the stubbed
        module writes CRLF bytes. The restore must write back exactly the bytes it read, not git's checkout."""
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, STUB_PASSES)})
        head = self.commit_on_layer(c, {"feat.py": MODULE})
        git(c.wt, "config", "core.autocrlf", "true")  # the repo-wide setting a Windows install has
        self.assertEqual(c.step(), "worked")
        self.assertTrue(self.queue_task()["notes"][-1].startswith("tests rejected: weak"), self.queue_task()["notes"])
        self.assertEqual((c.wt / "feat.py").read_bytes(), MODULE.encode("utf-8"))
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)
        self.assert_clean(c)

    def test_stale_bytecode_of_real_code_is_not_used_in_empty_run(self):
        """A same-size stub must not be shadowed by a cached .pyc of the real module."""
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, STUB_PASSES)})
        self.commit_on_layer(c, {".gitignore": "__pycache__/\n*.pyc\n", "feat.py": MODULE})
        # A .pyc that Python never re-validates against the source: only an isolated cache avoids it.
        py_compile.compile(str(c.wt / "feat.py"), cfile=str(c.wt / "__pycache__" /
                                                           f"feat.{sys.implementation.cache_tag}.pyc"),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True)
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertTrue(t["notes"][-1].startswith("tests rejected: weak (they pass on an empty implementation)"),
                        t["notes"])
        self.assertEqual((c.wt / "feat.py").read_bytes(), MODULE.encode("utf-8"))

    def test_stub_run_writes_no_bytecode_for_later_restored_runs(self):
        """With __pycache__ ignored, a stub .pyc of the same size would silently stay behind; none may be written."""
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, STRONG)})
        self.commit_on_layer(c, {".gitignore": "__pycache__/\n*.pyc\n", "feat.py": MODULE})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.queue_task()["status"], "tests_ok")
        self.assertFalse(list(c.wt.rglob("*.pyc")))
        out = subprocess.run([sys.executable, "-c", "import feat; print(feat.value())"], cwd=str(c.wt),
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        self.assertEqual(out, "4243")

    # (c)
    def test_c_timeout_on_empty_implementation_is_never_accepted(self):
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, SLEEP_ON_NONE)},
                      limits={"claude_daily_token_cap": 10 ** 9, "codex_daily_token_cap": 10 ** 9,
                              "test_timeout_s": 1})
        self.commit_on_layer(c, {"feat.py": MODULE})
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertEqual(t["status"], "todo")
        self.assertEqual(t["notes"][-1], "tests rejected: no real failing run on the empty implementation (timed out)")
        self.assertEqual((c.wt / "feat.py").read_bytes(), MODULE.encode("utf-8"))
        self.assert_clean(c)

    def test_no_tests_ran_on_empty_implementation_is_rejected(self):
        body = ("import unittest\nimport feat\n"
                "if feat.value() is None:\n raise SystemExit(3)\n"
                "class T(unittest.TestCase):\n"
                " def test_value(self): self.assertEqual(feat.value(), 4244)\n")
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, body)})
        self.commit_on_layer(c, {"feat.py": MODULE})
        c.step()
        t = self.queue_task()
        self.assertEqual(t["status"], "todo")
        self.assertEqual(t["notes"][-1],
                         "tests rejected: no real failing run on the empty implementation (no tests ran)")

    def test_current_code_notes_unchanged(self):
        weak = "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n"
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, weak)})
        c.step()
        self.assertEqual(self.queue_task()["notes"][-1], "tests rejected: weak (they pass before the feature exists)")
        none = "import unittest\n"
        c2 = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, none)})
        c2.step()
        self.assertEqual(self.queue_task()["notes"][-1],
                         "tests rejected: no real failing run (timed out or no tests ran)")

    # (d)
    def test_d_default_writer_accepted_only_test_file_committed(self):
        c = self.init(agents={"test_writer": self.write_tests})
        self.assertEqual(c.step(), "worked")
        t = self.queue_task()
        self.assertEqual(t["status"], "tests_ok")
        files = git(c.wt, "show", "--name-only", "--format=", t["tests_commit"]).split()
        self.assertEqual(files, ["tests/core/test_feat.py"])
        self.assertFalse((c.wt / "feat.py").exists())
        self.assertNotIn("feat.py", self.branch_files())
        self.assert_clean(c)

    def test_nested_new_module_dirs_created_and_removed(self):
        body = ("import unittest\nfrom pkg.sub import mod\n"
                "class T(unittest.TestCase):\n def test_v(self): self.assertEqual(mod.VALUE, 1)\n")
        c = self.init(self.task(files_in_scope=["pkg/sub/mod.py", "pkg/sub/*.txt"]),
                      agents={"test_writer": lambda p, cwd: write_test(cwd, body)})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.queue_task()["status"], "tests_ok")
        self.assertFalse((c.wt / "pkg").exists())
        self.assert_clean(c)

    def test_existing_dir_is_kept_when_creating_new_module(self):
        body = ("import unittest\nfrom pkg import mod\n"
                "class T(unittest.TestCase):\n def test_v(self): self.assertEqual(mod.VALUE, 1)\n")
        c = self.init(self.task(files_in_scope=["pkg/mod.py"]),
                      agents={"test_writer": lambda p, cwd: write_test(cwd, body)})
        self.commit_on_layer(c, {"pkg/other.txt": "keep\n"})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.queue_task()["status"], "tests_ok")
        self.assertTrue((c.wt / "pkg" / "other.txt").exists())
        self.assertFalse((c.wt / "pkg" / "mod.py").exists())
        self.assert_clean(c)

    def test_unparsable_tracked_module_is_stubbed_as_empty_file(self):
        lazy = ("import unittest\nclass T(unittest.TestCase):\n"
                " def test_module(self):\n  import feat\n  self.assertTrue(hasattr(feat, '__name__'))\n")
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, lazy)})
        self.commit_on_layer(c, {"feat.py": "def broken(:\n"})
        # current code: syntax error -> fails; empty file -> passes -> weak
        c.step()
        self.assertTrue(self.queue_task()["notes"][-1].startswith(
            "tests rejected: weak (they pass on an empty implementation)"))
        self.assertEqual((c.wt / "feat.py").read_text(encoding="utf-8"), "def broken(:\n")

    # (e)
    def test_e_two_empty_implementation_rejections_block_and_email(self):
        c = self.init(agents={"test_writer": lambda p, cwd: write_test(cwd, IMPORT_ONLY)})
        c.step()
        self.assertEqual(self.queue_task()["status"], "todo")
        c.step()
        t = self.queue_task()
        self.assertEqual(t["status"], "blocked")
        self.assertEqual(t["test_rejects"], 2)
        qs = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))
        self.assertTrue(any(q["kind"] == "blocked" and q.get("task") == "T1" for q in qs.values()))
        self.assertTrue(any("blocked" in s for s, _ in self.mails))
        self.assertFalse((c.wt / "feat.py").exists())

    # restoration under failure
    def test_setup_failure_restores_every_touched_file(self):
        c = self.init(self.task(files_in_scope=["a.py", "b.py", "pkg/new/c.py"]),
                      agents={"test_writer": lambda p, cwd: write_test(cwd, STRONG)})
        a, b = "def value(): return 1\n", "def other(): return 2\n"
        head = self.commit_on_layer(c, {"a.py": a, "b.py": b})
        real = bootstrap.empty_implementation
        calls = []

        def flaky(src):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(src)

        with patch.object(bootstrap, "empty_implementation", side_effect=flaky):
            self.assertEqual(c.step(), "error")
        self.assertEqual(len(calls), 2)
        self.assertEqual((c.wt / "a.py").read_text(encoding="utf-8"), a)
        self.assertEqual((c.wt / "b.py").read_text(encoding="utf-8"), b)
        self.assertFalse((c.wt / "pkg").exists())
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.queue_task()["status"], "todo")

    def test_run_failure_restores_stubs_created_files_and_dirs(self):
        c = self.init(self.task(files_in_scope=["a.py", "pkg/new/c.py"]),
                      agents={"test_writer": lambda p, cwd: write_test(cwd, STRONG)})
        a = "def value(): return 1\n"
        head = self.commit_on_layer(c, {"a.py": a})
        seen = {}

        def boom(t):
            seen["a"] = (c.wt / "a.py").read_text(encoding="utf-8")
            seen["c"] = (c.wt / "pkg/new/c.py").exists()
            raise OSError("runner crashed")

        with patch.object(c, "_run_tests_on_stub", side_effect=boom):
            self.assertEqual(c.step(), "error")
        self.assertIn("return None", seen["a"])
        self.assertTrue(seen["c"])
        self.assertEqual((c.wt / "a.py").read_text(encoding="utf-8"), a)
        self.assertFalse((c.wt / "pkg").exists())
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.queue_task()["status"], "todo")

    def test_restore_failure_is_stage_error_and_nothing_committed(self):
        c = self.init(agents={"test_writer": self.write_tests})
        self.commit_on_layer(c, {"feat.py": MODULE})
        head = git(c.wt, "rev-parse", "HEAD")
        real_write = Path.write_bytes

        def no_restore(path, data):  # the restore writes the original bytes back (fix C); make that write fail
            if path.name == "feat.py" and data == MODULE.encode("utf-8"):
                return len(data)
            return real_write(path, data)

        with patch.object(Path, "write_bytes", no_restore):
            self.assertEqual(c.step(), "error")
        t = self.queue_task()
        self.assertEqual(t["status"], "todo")
        self.assertTrue(any("empty implementation restore failed" in n for n in t["notes"]), t["notes"])
        self.assertEqual(git(c.wt, "rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
