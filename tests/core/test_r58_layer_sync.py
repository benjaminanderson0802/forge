"""R58: when nothing is mid-stage, the layer branch takes new origin/main commits (no-ff merge by Forge)."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.bootstrap import Conductor
from core.finalize import Journal

try:
    from tests.core.test_bootstrap import Harness, git
except ImportError:
    from test_bootstrap import Harness, git


class LayerSync(Harness):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "origin", "main")
        self.other = root / "other"
        git(root, "clone", "-q", str(self.origin), str(self.other))
        git(self.other, "config", "user.name", "Other")
        git(self.other, "config", "user.email", "other@example.com")
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def conductor(self, agents=None):
        self.make_conductor(agents)
        c = Conductor(self.repo, self.work, self.state, self.team, self.c.limits, owner_email="ben@example.com",
                      mailer=lambda s, b: self.mails.append((s, b)), inbox=lambda: self.messages, gh=self.gh,
                      clock=lambda: self.now, judge_cmds=[], push=True, checks=self.checks, probes=self.probes)
        c.init_queue(self.layer, [self.task()])
        git(self.repo, "push", "-q", "origin", self.layer)
        self.c = c
        return c

    def main_commit(self, name, text, message="main moved on"):
        git(self.other, "fetch", "-q", "origin")
        git(self.other, "checkout", "-q", "-B", "main", "origin/main")
        (self.other / name).write_text(text, encoding="utf-8")
        git(self.other, "add", name)
        git(self.other, "commit", "-q", "-m", message)
        git(self.other, "push", "-q", "origin", "main")
        return git(self.other, "rev-parse", "HEAD")

    @property
    def lwt(self):
        return self.work / self.layer

    def tip(self):
        return git(self.repo, "rev-parse", f"refs/heads/{self.layer}")

    def later(self, minutes=11):
        self.now += timedelta(minutes=minutes)

    def sync_questions(self):
        qs = json.loads((self.state / "questions.json").read_text(encoding="utf-8")) \
            if (self.state / "questions.json").exists() else {}
        return {k: v for k, v in qs.items() if v.get("kind") == "merge" and v.get("sync")}

    # ------------------------------------------------------------------ tests
    def test_fast_sync_merges_main_pushes_and_new_task_worktrees_use_the_new_tip(self):
        seen = {}

        def builder(prompt, cwd):
            seen["main_file"] = (Path(cwd) / "from_main.txt").exists()
            seen["head_parent"] = git(cwd, "rev-parse", "HEAD")
            return self.build_feature(prompt, cwd)

        c = self.conductor({"test_writer": self.write_tests, "builder": builder})
        old = self.tip()
        m = self.main_commit("from_main.txt", "new core module\n")
        self.assertEqual(c.step(), "worked")  # synced first, then the tests stage
        log = git(self.repo, "log", "--format=%H|%P|%an|%s", f"{self.layer}", "-n", "5").splitlines()
        sync = next(ln for ln in log if ln.endswith(f"Sync {self.layer} with main"))
        sha, parents, author, _s = sync.split("|")
        self.assertEqual(parents.split(), [old, m])
        self.assertEqual(author, "Forge")
        self.assertTrue((self.lwt / "from_main.txt").exists())
        self.assertEqual(git(self.origin, "rev-parse", f"refs/heads/{self.layer}"), sha)  # the sync was pushed
        c.step()  # build: the task worktree starts from the synced layer tip
        self.assertTrue(seen.get("main_file"))
        self.assertEqual(c._task("T1")["status"], "done")
        self.assertIn("main sync: merged", (self.state / "errors.log").read_text(encoding="utf-8"))

    def test_no_sync_while_a_task_is_mid_stage(self):
        c = self.conductor()
        old = self.tip()
        self.main_commit("from_main.txt", "x\n")
        # 1. a task worktree with uncommitted work
        twt = c.trees.prepare_task("T1", old)
        (twt / "feat.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertTrue(c._sync_with_main().startswith("busy"))
        c.trees.remove_task("T1")
        # 2. a tests_ok task with an active claim
        c._update("T1", status="tests_ok")
        c._apply("create-T1", "create", "T1", "forge-manager", {
            "title": "t", "spec_ref": "T1", "acceptance": "x", "files_in_scope": ["feat.py"], "max_attempts": 6,
            "token_budget": 10 ** 9})
        c._apply("claim-T1", "claim", "T1", "forge-executor")
        self.assertTrue(c._sync_with_main().startswith("busy"))
        c._apply("release-T1", "release", "T1", "forge-core")
        # 3. an active finalization
        Journal(self.state).save({"tid": "T9", "status": "active", "phase": "merge", "notes": []})
        self.assertTrue(c._sync_with_main().startswith("busy"))
        self.assertEqual(self.tip(), old)
        self.assertFalse((self.state / "main_sync.json").exists())  # waiting never uses up the fetch window
        (self.state / "merges" / "T9.json").unlink()
        self.assertEqual(c._sync_with_main(), "synced")
        self.assertNotEqual(self.tip(), old)

    def test_conflict_aborts_asks_once_and_does_not_loop(self):
        c = self.conductor()
        (self.lwt / "README.md").write_text("layer version\n", encoding="utf-8")
        git(self.lwt, "commit", "-q", "-am", "layer edit")
        old = self.tip()
        self.main_commit("README.md", "main version\n")
        self.assertEqual(c._sync_with_main(), "conflict")
        self.assertEqual(self.tip(), old)  # keeps its old base
        self.assertEqual(git(self.lwt, "status", "--porcelain"), "")
        self.assertFalse((self.lwt / git(self.lwt, "rev-parse", "--git-path", "MERGE_HEAD")).exists())  # aborted
        qs = self.sync_questions()
        self.assertEqual(len(qs), 1)
        self.assertEqual(list(qs.values())[0]["status"], "open")
        mails = len(self.mails)
        for _ in range(3):  # nothing moved and Ben hasn't answered: no retry, no new question
            self.later()
            self.assertEqual(c._sync_with_main(), "conflict pending")
        self.later()
        self.main_commit("README.md", "main version 2\n")  # main moves on, still conflicting: same question
        self.assertEqual(c._sync_with_main(), "conflict")
        self.assertEqual(len(self.sync_questions()), 1)
        self.assertEqual(len(self.mails), mails)
        self.assertEqual(self.tip(), old)
        for _ in range(3):  # and a full step never loops on it either
            self.later()
            c.step()
        self.assertEqual(len(self.sync_questions()), 1)

    def test_fetch_is_rate_limited_to_once_every_ten_minutes(self):
        c = self.conductor()
        self.assertEqual(c._sync_with_main(), "current")
        m = self.main_commit("from_main.txt", "x\n")
        self.later(5)
        self.assertEqual(c._sync_with_main(), "rate limited")
        self.assertNotIn(m, git(self.repo, "rev-list", self.layer))
        self.later(6)
        self.assertEqual(c._sync_with_main(), "synced")
        self.assertIn(m, git(self.repo, "rev-list", self.layer))
        self.later(1)
        self.assertEqual(c._sync_with_main(), "rate limited")


if __name__ == "__main__":
    unittest.main()
