"""T1B3b: per-task worktrees, judges at the exact commit in a throwaway worktree (real git in temp folders)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.worktrees import Worktrees
from tests.core.test_bootstrap import Harness, git


def ok(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).returncode == 0


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.work = root / "repo", root / "work"
        self.repo.mkdir()
        self.work.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "config", "user.email", "t@example.com")
        self.c1 = self.commit("a.txt", "one\n")
        self.c2 = self.commit("a.txt", "two\n")
        self.trees = Worktrees(self.repo, self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", f"{name} {text.strip()}")
        return git(self.repo, "rev-parse", "HEAD")

    def branches(self):
        return set(git(self.repo, "branch", "--format=%(refname:short)").split())

    def registered(self):
        out = git(self.repo, "worktree", "list", "--porcelain")
        return {Path(ln[len("worktree "):]).resolve() for ln in out.splitlines() if ln.startswith("worktree ")}


class PathsTests(Repo):
    def test_paths_and_branch_names(self):
        self.assertEqual(self.trees.task_path("T1"), self.work / "tasks" / "T1")
        self.assertEqual(self.trees.task_branch("T1"), "forge-task/T1")

    def test_bad_tid_raises_value_error_everywhere(self):
        for bad in ("", "../x", "a/b", "x" * 41, "a b", "T1\n", "..", None):
            for call in (self.trees.task_path, self.trees.task_branch, self.trees.remove_task,
                         lambda t: self.trees.prepare_task(t, self.c1)):
                with self.subTest(tid=bad, call=getattr(call, "__name__", "prepare_task")):
                    with self.assertRaises(ValueError):
                        call(bad)
        self.assertEqual(self.trees.task_path("A_b-9"), self.work / "tasks" / "A_b-9")


class PrepareTests(Repo):
    def test_prepare_gives_clean_worktree_at_base_on_task_branch(self):
        p = self.trees.prepare_task("T1", self.c1)
        self.assertEqual(p, self.work / "tasks" / "T1")
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c1)
        self.assertEqual(git(p, "symbolic-ref", "--short", "HEAD"), "forge-task/T1")
        self.assertEqual(git(p, "status", "--porcelain", "-uall"), "")
        self.assertEqual((p / "a.txt").read_text(encoding="utf-8"), "one\n")
        self.assertIn(p.resolve(), self.registered())

    def test_reprepare_moves_to_new_base_and_discards_dirty_and_untracked(self):
        p = self.trees.prepare_task("T1", self.c1)
        (p / "a.txt").write_text("dirty\n", encoding="utf-8")
        (p / "new.txt").write_text("untracked\n", encoding="utf-8")
        (p / "sub").mkdir()
        (p / "sub" / "deep.txt").write_text("x", encoding="utf-8")
        git(p, "add", "a.txt")
        self.assertEqual(self.trees.prepare_task("T1", self.c2), p)
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c2)
        self.assertEqual(git(p, "status", "--porcelain", "-uall"), "")
        self.assertFalse((p / "new.txt").exists())
        self.assertFalse((p / "sub").exists())
        self.assertEqual((p / "a.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(self.trees.prepare_task("T1", self.c1), p)  # back again, and twice is safe
        self.assertEqual(self.trees.prepare_task("T1", self.c1), p)
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c1)
        self.assertEqual(git(self.repo, "rev-parse", "forge-task/T1"), self.c1)

    def test_prepare_moves_a_branch_that_has_commits(self):
        p = self.trees.prepare_task("T1", self.c1)
        (p / "b.txt").write_text("b\n", encoding="utf-8")
        git(p, "add", "b.txt")
        git(p, "-c", "user.name=X", "-c", "user.email=x@x", "commit", "-q", "-m", "b")
        self.trees.prepare_task("T1", self.c2)
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c2)
        self.assertFalse((p / "b.txt").exists())

    def test_leftover_unregistered_folder_is_replaced(self):
        p = self.work / "tasks" / "T1"
        p.mkdir(parents=True)
        (p / "junk.txt").write_text("left from a crash", encoding="utf-8")
        self.assertEqual(self.trees.prepare_task("T1", self.c2), p)
        self.assertFalse((p / "junk.txt").exists())
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c2)

    def test_prepare_after_folder_deleted_behind_gits_back(self):
        import shutil
        p = self.trees.prepare_task("T1", self.c1)
        shutil.rmtree(p)
        self.assertEqual(self.trees.prepare_task("T1", self.c2), p)
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c2)

    def test_unknown_base_raises_runtime_error(self):
        for base in ("0" * 40, "no-such-ref", "deadbeef"):
            with self.subTest(base=base), self.assertRaises(RuntimeError):
                self.trees.prepare_task("T1", base)

    def test_short_sha_base_is_accepted(self):
        p = self.trees.prepare_task("T1", self.c1[:10])
        self.assertEqual(git(p, "rev-parse", "HEAD"), self.c1)


class RemoveTests(Repo):
    def test_remove_task_removes_folder_and_branch_and_is_idempotent(self):
        p = self.trees.prepare_task("T1", self.c1)
        other = self.trees.prepare_task("T2", self.c2)
        git(self.repo, "branch", "keepme", self.c1)
        self.trees.remove_task("T1")
        self.assertFalse(p.exists())
        self.assertNotIn("forge-task/T1", self.branches())
        self.assertNotIn(p.resolve(), self.registered())
        self.trees.remove_task("T1")  # nothing to remove is not an error
        self.trees.remove_task("T9")
        self.assertTrue(other.is_dir())
        self.assertEqual(git(other, "rev-parse", "HEAD"), self.c2)
        self.assertTrue({"forge-task/T2", "keepme", "main"} <= self.branches())

    def test_remove_task_with_only_a_stale_branch(self):
        git(self.repo, "branch", "forge-task/T3", self.c1)
        self.trees.remove_task("T3")
        self.assertNotIn("forge-task/T3", self.branches())

    def test_remove_task_with_folder_deleted_behind_gits_back(self):
        import shutil
        p = self.trees.prepare_task("T1", self.c1)
        shutil.rmtree(p)
        self.trees.remove_task("T1")
        self.assertNotIn("forge-task/T1", self.branches())
        self.assertNotIn(p.resolve(), self.registered())


class ThrowawayTests(Repo):
    def test_throwaway_is_at_exactly_the_sha_and_removed_on_exit(self):
        with self.trees.throwaway(self.c1[:8]) as tw:
            tw = Path(tw)
            self.assertEqual(tw.parent, self.work / "tmp")
            self.assertRegex(tw.name, r"^[0-9a-f]{12}$")
            self.assertEqual(git(tw, "rev-parse", "HEAD"), self.c1)
            self.assertIn(tw.resolve(), self.registered())
            (tw / "scratch.txt").write_text("edits vanish", encoding="utf-8")
        self.assertFalse(tw.exists())
        self.assertNotIn(tw.resolve(), self.registered())
        self.assertEqual(self.branches(), {"main"})

    def test_throwaway_removed_on_exception_and_exception_propagates(self):
        class Oops(Exception):
            pass
        seen = []
        with self.assertRaises(Oops):
            with self.trees.throwaway(self.c2) as tw:
                seen.append(Path(tw))
                raise Oops("body failed")
        self.assertFalse(seen[0].exists())
        self.assertNotIn(seen[0].resolve(), self.registered())

    def test_removal_failure_is_swallowed_and_never_masks_body_error(self):
        class Oops(Exception):
            pass
        with patch.object(Worktrees, "_remove_tree", side_effect=OSError("locked")):
            with self.assertRaises(Oops):
                with self.trees.throwaway(self.c2):
                    raise Oops("the real error")
            with self.trees.throwaway(self.c2) as tw:  # normal exit: removal failure is swallowed
                left = Path(tw)
        self.assertTrue(left.exists())  # left for sweep
        self.assertIn(f"tmp/{left.name}", self.trees.sweep(set()))
        self.assertFalse(left.exists())

    def test_unknown_sha_raises_before_yield(self):
        body = []
        with self.assertRaises(RuntimeError):
            with self.trees.throwaway("0" * 40):
                body.append(1)
        self.assertEqual(body, [])

    def test_two_throwaways_at_once_are_distinct(self):
        with self.trees.throwaway(self.c1) as a, self.trees.throwaway(self.c2) as b:
            self.assertNotEqual(Path(a), Path(b))
            self.assertEqual(git(a, "rev-parse", "HEAD"), self.c1)
            self.assertEqual(git(b, "rev-parse", "HEAD"), self.c2)


class SweepTests(Repo):
    def test_sweep_removes_stale_tmp_and_unkept_tasks_only(self):
        layer = self.work / "layer-1"
        git(self.repo, "worktree", "add", "-q", "-b", "layer-1", str(layer), "main")
        keep = self.trees.prepare_task("T1", self.c1)
        drop = self.trees.prepare_task("T2", self.c2)
        stale = self.work / "tmp" / "abcdef012345"
        git(self.repo, "worktree", "add", "-q", "--detach", str(stale), self.c1)
        junk = self.work / "tmp" / "junkfolder"
        junk.mkdir()
        (junk / "x.txt").write_text("x", encoding="utf-8")
        orphan = self.work / "tasks" / "T3"
        orphan.mkdir()
        removed = self.trees.sweep({"T1"})
        self.assertEqual(sorted(removed), ["tasks/T2", "tasks/T3", "tmp/abcdef012345", "tmp/junkfolder"])
        self.assertTrue(keep.is_dir())
        self.assertEqual(git(keep, "rev-parse", "HEAD"), self.c1)
        for p in (drop, stale, junk, orphan):
            self.assertFalse(p.exists(), p)
        self.assertNotIn("forge-task/T2", self.branches())
        self.assertIn("forge-task/T1", self.branches())
        self.assertIn("layer-1", self.branches())
        self.assertTrue((layer / "a.txt").is_file())
        self.assertEqual(git(layer, "rev-parse", "HEAD"), self.c2)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.c2)
        self.assertEqual(self.registered(), {self.repo.resolve(), layer.resolve(), keep.resolve()})
        self.assertEqual(self.trees.sweep({"T1"}), [])

    def test_sweep_with_nothing_there(self):
        self.assertEqual(self.trees.sweep(set()), [])


class ConductorWorktreeTests(Harness):
    def setUp(self):
        super().setUp()
        self.cwds = {}

    def rec(self, role, fn):
        def run(prompt, cwd):
            self.cwds.setdefault(role, []).append((Path(cwd), git(cwd, "rev-parse", "HEAD")))
            return fn(prompt, cwd)
        return run

    def layer_tip(self):
        return git(self.repo, "rev-parse", self.layer)

    def contract(self, c):
        return c._ledger().contracts()["T1"]

    def test_builder_runs_in_task_worktree_and_pass_lands_on_layer(self):
        c = self.advance_to_build(agents={"test_writer": self.write_tests,
                                          "builder": self.rec("builder", self.build_feature),
                                          "reviewer": self.rec("reviewer", lambda p, cwd: (
                                              json.dumps({"verdict": "pass" if (cwd / "feat.py").is_file()
                                                          else "fail", "reasons": []}), 1))})
        base = self.layer_tip()
        self.assertEqual(c.step(), "worked")
        cwd, head = self.cwds["builder"][0]
        self.assertEqual(cwd, self.work / "tasks" / "T1")
        self.assertNotEqual(cwd, self.work / self.layer)
        self.assertEqual(head, base)
        s = self.contract(c)["commit"]
        self.assertNotEqual(s, base)
        self.assertEqual(self.layer_tip(), s)
        self.assertEqual(git(self.repo, "rev-parse", f"{s}^"), base)
        self.assertEqual(c._task("T1")["status"], "done")
        self.assertEqual(c._task("T1")["done_commit"], s)
        self.assertFalse((self.work / "tasks" / "T1").exists())
        self.assertNotIn("forge-task/T1", git(self.repo, "branch", "--format=%(refname:short)").split())
        rcwd, rhead = self.cwds["reviewer"][0]
        self.assertNotEqual(rcwd, self.work / self.layer)
        self.assertEqual(rhead, s)
        self.assertEqual(self.contract(c)["status"], "done")

    def test_failed_attempt_never_moves_layer_and_resets_task_worktree(self):
        def bad(p, cwd):
            (cwd / "feat.py").write_text("VALUE = 41\n", encoding="utf-8")  # judge fails
            (cwd / "scratch.txt").write_text("x", encoding="utf-8")
            return '{"status":"done"}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": bad})
        base = self.layer_tip()
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.layer_tip(), base)
        self.assertEqual(git(self.work / self.layer, "status", "--porcelain", "-uall"), "")
        self.assertFalse((self.work / self.layer / "feat.py").exists())
        twt = self.work / "tasks" / "T1"
        self.assertEqual(git(twt, "rev-parse", "HEAD"), base)
        self.assertEqual(git(twt, "status", "--porcelain", "-uall"), "")
        self.assertNotEqual(c._task("T1")["status"], "done")
        self.assertTrue(c._task("T1")["fail_signatures"])

    def test_out_of_scope_attempt_never_moves_layer(self):
        def bad(p, cwd):
            (cwd / "oops.py").write_text("x = 1\n", encoding="utf-8")
            return '{"status":"done"}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": bad})
        base = self.layer_tip()
        c.step()
        self.assertEqual(self.layer_tip(), base)
        self.assertIn("out of scope", " ".join(c._task("T1")["notes"]))
        self.assertEqual(git(self.work / "tasks" / "T1", "status", "--porcelain", "-uall"), "")

    def test_judges_run_at_exact_commit_in_throwaway(self):
        log = Path(self.tmp.name) / "judge.log"
        script = Path(self.tmp.name) / "judge.py"
        script.write_text(
            "import os, subprocess, sys\n"
            "head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
            "with open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
            "    f.write(os.getcwd() + '|' + head + '|' + str(os.path.isfile('feat.py')) + '\\n')\n",
            encoding="utf-8")
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.judge_cmds = [f'"{sys.executable}" "{script}" "{log}"']
        c.step()
        c.step()
        s = self.contract(c)["commit"]
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        where, head, has_feat = lines[0].split("|")
        self.assertEqual(Path(where).resolve().parent, (self.work / "tmp").resolve())
        self.assertEqual(head, s)
        self.assertEqual(has_feat, "True")
        self.assertFalse(Path(where).exists())
        self.assertEqual(c._task("T1")["status"], "done")
        runs = c._ledger().test_runs()
        self.assertTrue(any(r["commit"] == s and r["passed"] for r in runs.values()))

    def test_failing_judge_cmd_in_throwaway_fails_attempt_with_signature(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.judge_cmds = [f'"{sys.executable}" -c "import sys; print(\'judge said no\'); sys.exit(3)"']
        c.step()
        base = self.layer_tip()
        c.step()
        t = c._task("T1")
        self.assertNotEqual(t["status"], "done")
        self.assertIn("judge failed", " ".join(t["notes"]))
        self.assertEqual(self.layer_tip(), base)
        self.assertEqual(list((self.work / "tmp").iterdir()) if (self.work / "tmp").exists() else [], [])
        runs = c._ledger().test_runs()
        self.assertTrue(any(not r["passed"] for r in runs.values()))

    def test_troubleshooter_edits_are_discarded(self):
        def ts(p, cwd):
            (cwd / "trouble.txt").write_text("scratch", encoding="utf-8")
            (cwd / "feat.py").write_text("VALUE = 'ts'\n", encoding="utf-8")
            git(cwd, "add", "-A")
            git(cwd, "-c", "user.name=TS", "-c", "user.email=ts@x", "commit", "-q", "-m", "troubleshooter commit")
            return '{"kind":"fix","notes":"set VALUE to 42"}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests,
                                          "builder": lambda p, cwd: ('{"status":"done"}', 1),
                                          "troubleshooter": self.rec("troubleshooter", ts)})
        base = self.layer_tip()
        c.step()
        c.step()
        self.assertEqual(len(self.cwds.get("troubleshooter", [])), 1)
        cwd, head = self.cwds["troubleshooter"][0]
        self.assertEqual(cwd.resolve().parent, (self.work / "tmp").resolve())
        self.assertEqual(head, base)
        self.assertFalse(cwd.exists())
        self.assertEqual(self.layer_tip(), base)
        self.assertFalse((self.work / self.layer / "trouble.txt").exists())
        self.assertEqual(git(self.repo, "log", "--all", "--format=%H", "--", "trouble.txt"), "")
        for ref in git(self.repo, "for-each-ref", "--format=%(refname)").split():
            self.assertNotIn("troubleshooter commit", git(self.repo, "log", "--format=%s", ref))
        self.assertIn("set VALUE to 42", c._task("T1")["trouble_notes"])

    def recover(self, leave):
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c._apply("create-T1", "create", "T1", "forge-manager", {
            "title": "Implement feature", "spec_ref": "T1", "acceptance": "x", "files_in_scope": ["feat.py"],
            "max_attempts": 6, "token_budget": 10 ** 9})
        c._apply("T1-crash-claim", "claim", "T1", "forge-executor")
        if leave == "submitted":
            c._apply("T1-crash-report", "run_report", "T1", "forge-core", {
                "run_id": "T1-crash", "claim": "done", "commit": "x", "changed": [], "violations": [],
                "out_of_scope": []})
            c._apply("T1-crash-submit", "submit", "T1", "forge-executor", {"commit": self.layer_tip()})
        self.assertEqual(self.contract(c)["status"], leave)
        before = c._task("T1")
        self.assertEqual(c.step(), "worked")
        pids = [e["proposal_id"] for e in c._ledger().events()]
        return c, before, pids

    def test_claimed_contract_is_released_then_attempt_proceeds(self):
        c, before, pids = self.recover("claimed")
        self.assertIn("T1-recover-0-release", pids)
        self.assertNotIn("T1-recover-0-fail", pids)
        t = c._task("T1")
        self.assertIn("interrupted attempt recovered", t["notes"])
        self.assertEqual(len(self.team.builder.prompts), 1)
        self.assertEqual(t["fail_signatures"], before["fail_signatures"])
        self.assertEqual(t["status"], "done")
        self.assertEqual(self.contract(c)["status"], "done")

    def test_submitted_contract_is_failed_and_reopened_then_attempt_proceeds(self):
        c, before, pids = self.recover("submitted")
        self.assertIn("T1-recover-0-fail", pids)
        self.assertIn("T1-recover-0-reopen", pids)
        self.assertLess(pids.index("T1-recover-0-fail"), pids.index("T1-recover-0-reopen"))
        self.assertNotIn("T1-recover-0-release", pids)
        t = c._task("T1")
        self.assertIn("interrupted attempt recovered", t["notes"])
        self.assertEqual(t["fail_signatures"], before["fail_signatures"])
        self.assertEqual(len(self.team.builder.prompts), 1)
        self.assertEqual(t["status"], "done")


if __name__ == "__main__":
    unittest.main()
