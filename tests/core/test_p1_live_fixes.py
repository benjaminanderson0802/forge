"""Live-run P1 fixes (Codex review of the integrated Layer 1):
1. the answer drop folder is fingerprinted around agent runs, and gate/approval answers are email-only;
2. IMAP/SMTP connections time out, and stall detection covers startup's inbox read;
3. test and judge commands are tree-killed on timeout (no wait on orphan-held pipes) and honour KILL;
4. a commit made by an agent is a violation: rejected, and the worktree is reset to its pre-run base."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import bootstrap, channel, service
from core.bootstrap import Stopped

try:
    from tests.core.test_bootstrap import Harness, git
except ImportError:  # run from inside tests/core
    from test_bootstrap import Harness, git


def agent_commit(cwd: Path, msg: str = "agent commit") -> None:
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Agent", "-c", "user.email=agent@x", "commit", "-q", "-m", msg],
                   cwd=cwd, check=True, capture_output=True)


# ---------------------------------------------------------------------- 1. drop folder
class DropFolderTests(Harness):
    def qs(self):
        return json.loads((self.state / "questions.json").read_text(encoding="utf-8"))

    def test_agent_writing_an_answer_file_during_its_run_is_tampering(self):
        holder = {}

        def builder(prompt, cwd):
            c = holder["c"]
            q = c._read("questions.json", {})["blocked-1"]
            channel.drop_answer(c.channel_in, "blocked-1", q["code"], "forged owner answer", "status page")
            return self.build_feature(prompt, cwd)

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        holder["c"] = c
        c._ask("blocked", "Task T9 is blocked", "details")
        c.step()
        self.assertTrue((self.state / "KILL").exists())
        log = (self.state / "errors.log").read_text(encoding="utf-8")
        self.assertIn("TAMPER", log)
        self.assertIn("channel/in/", log)
        self.assertEqual(self.qs()["blocked-1"]["status"], "open")
        self.assertNotEqual(c._task("T1")["status"], "done")

    def test_gate_answer_from_the_drop_folder_is_refused(self):
        c = self.make_conductor()
        c._ask("gate", "layer-1 is ready", "report", pr="7")
        code = self.qs()["gate-1"]["code"]
        channel.drop_answer(c.channel_in, "gate-1", code, "y", "status page")
        c._handle_inbox()
        self.assertEqual(self.qs()["gate-1"]["status"], "open")
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(list(c.channel_in.iterdir()), [])  # consumed, never applied
        self.assertIn("email", (self.state / "errors.log").read_text(encoding="utf-8"))
        for kind in ("merge", "spend"):
            qid = c._ask(kind, f"{kind} question", "details")
            channel.drop_answer(c.channel_in, qid, self.qs()[qid]["code"], "y", "status page")
            c._handle_inbox()
            self.assertEqual(self.qs()[qid]["status"], "open", kind)
        self.messages.append({"from": "Ben <ben@example.com>", "subject": f"[Forge Q-gate-1 {code}] y", "body": "y"})
        c._handle_inbox()  # email from the owner still approves
        self.assertEqual(self.qs()["gate-1"]["status"], "answered")
        self.assertEqual([a[:2] for a in self.gh_calls], [["pr", "edit"], ["pr", "merge"]])


# ---------------------------------------------------------------------- 2. startup stall and timeouts
class StartupStallTests(Harness):
    def test_blocking_startup_inbox_is_caught_by_stall_detection(self):
        release = threading.Event()
        stalls = []

        def on_stall(msg):
            stalls.append(msg)
            release.set()

        c = self.make_conductor()
        c.inbox = lambda: (release.wait(20), [])[1]
        root = Path(self.tmp.name) / "service"
        health = service.Health(root, {"step_stall_s": 0.3, "beat_s": 0.05}, on_stall=on_stall).start()
        try:
            t0 = time.monotonic()
            bootstrap._start_session(c, health)
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            health.stop()
        self.assertTrue(stalls, "a blocking startup inbox read was never caught")
        self.assertLess(elapsed, 10)

    def test_imap_connection_gets_a_timeout(self):
        fake_keyring = types.SimpleNamespace(get_password=lambda *a: "pw")
        conn = MagicMock()
        conn.search.return_value = ("OK", [b""])
        with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, {"keyring": fake_keyring}), \
                patch("imaplib.IMAP4_SSL", return_value=conn) as ctor:
            bootstrap.gmail_inbox("ben@example.com", Path(d))()
        self.assertTrue(ctor.called)
        self.assertGreater(ctor.call_args.kwargs.get("timeout") or 0, 0)

    def test_smtp_connection_gets_a_timeout(self):
        fake_keyring = types.SimpleNamespace(get_password=lambda *a: "pw")
        with patch.dict(sys.modules, {"keyring": fake_keyring}), patch("smtplib.SMTP_SSL") as ctor:
            bootstrap.gmail_mailer("ben@example.com")("s", "b")
        self.assertGreater(ctor.call_args.kwargs.get("timeout") or 0, 0)


# ---------------------------------------------------------------------- 3. tree-kill judges
GRANDCHILD = ("import subprocess, sys, time, pathlib\n"
              "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "pathlib.Path(sys.argv[1]).write_text(str(g.pid))\n"
              "print('parent started', flush=True)\n"
              "time.sleep(float(sys.argv[2]))\n")


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class TreeKillTests(Harness):
    def setUp(self):
        super().setUp()
        self.c = self.make_conductor(limits={"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9,
                                             "test_timeout_s": 2})
        self.dir = Path(self.tmp.name) / "cmd"
        self.dir.mkdir()
        (self.dir / "spawn.py").write_text(GRANDCHILD, encoding="utf-8")
        self.pidfile = self.dir / "gpid"

    def gone(self, pid: int) -> bool:
        end = time.monotonic() + 3
        while time.monotonic() < end and _alive(pid):
            time.sleep(0.05)
        return not _alive(pid)

    def cmd(self, parent_sleep: float) -> str:
        return f'"{sys.executable}" spawn.py "{self.pidfile}" {parent_sleep}'

    def test_judge_timeout_kills_the_tree_and_returns_on_time(self):
        t0 = time.monotonic()
        code, out = self.c._run_cmd(self.cmd(30), cwd=self.dir)
        self.assertLess(time.monotonic() - t0, 2 + 3)
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)
        self.assertTrue(self.gone(int(self.pidfile.read_text())))

    def test_judge_that_exits_leaving_an_orphan_on_stdout_does_not_hang(self):
        t0 = time.monotonic()
        code, out = self.c._run_cmd(self.cmd(0), cwd=self.dir)
        self.assertLess(time.monotonic() - t0, 2 + 3)
        self.assertEqual(code, 0)
        self.assertIn("parent started", out)
        self.assertTrue(self.gone(int(self.pidfile.read_text())))

    def test_task_test_timeout_kills_the_tree_and_returns_on_time(self):
        tdir = self.dir / "tests" / "core"
        tdir.mkdir(parents=True)
        (tdir / "test_hang.py").write_text(
            "import subprocess, sys, time, unittest, pathlib\n"
            "class T(unittest.TestCase):\n"
            "    def test_hang(self):\n"
            "        g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"        pathlib.Path({str(self.pidfile)!r}).write_text(str(g.pid))\n"
            "        time.sleep(30)\n", encoding="utf-8")
        t = {"test_cmd": "python -m unittest tests/core/test_hang.py", "test_files": ["tests/core/test_hang.py"]}
        t0 = time.monotonic()
        code, _out, timed_out = self.c._run_tests(t, cwd=self.dir)
        self.assertLess(time.monotonic() - t0, 2 + 3)
        self.assertEqual((code, timed_out), (124, True))
        self.assertTrue(self.gone(int(self.pidfile.read_text())))

    def test_judge_honours_kill(self):
        self.c.limits["test_timeout_s"] = 60
        threading.Timer(0.5, lambda: (self.state / "KILL").write_text("stop")).start()
        t0 = time.monotonic()
        with self.assertRaises(Stopped):
            self.c._run_cmd(self.cmd(30), cwd=self.dir)
        self.assertLess(time.monotonic() - t0, 0.5 + 2 + 3)
        self.assertTrue(self.gone(int(self.pidfile.read_text())))


# ---------------------------------------------------------------------- 4. agent commits
class AgentCommitTests(Harness):
    def layer_head(self):
        return git(self.repo, "rev-parse", self.layer)

    def test_builder_commit_to_a_protected_test_file_is_rejected_and_reset(self):
        def builder(prompt, cwd):
            (cwd / "feat.py").write_text("VALUE = 42\n", encoding="utf-8")
            (cwd / "tests/core/test_feat.py").write_text("# tampered\n", encoding="utf-8")
            agent_commit(cwd)
            return '{"status":"done"}', 1

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        base = self.layer_head()
        c.step()
        t = c._task("T1")
        self.assertNotIn(t["status"], ("done", "merge_pending"))
        self.assertTrue(any("commit" in n for n in t["notes"]), t["notes"])
        self.assertTrue(any("tests/core/test_feat.py" in n for n in t["notes"]), t["notes"])
        self.assertEqual(self.layer_head(), base)
        self.assertEqual(git(c.trees.task_path("T1"), "rev-parse", "HEAD"), base)
        self.assertNotIn("# tampered", git(self.repo, "show", f"{self.layer}:tests/core/test_feat.py"))
        c.step()
        self.assertNotIn("# tampered", git(self.repo, "show", f"{self.layer}:tests/core/test_feat.py"))

    def test_builder_commit_into_the_layer_worktree_is_rejected_and_reset(self):
        holder = {}

        def builder(prompt, cwd):
            wt = holder["c"].wt
            (wt / "tests/core/test_feat.py").write_text("# tampered\n", encoding="utf-8")
            agent_commit(wt)
            return self.build_feature(prompt, cwd)

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        holder["c"] = c
        base = self.layer_head()
        c.step()
        self.assertNotIn(c._task("T1")["status"], ("done", "merge_pending"))
        self.assertEqual(self.layer_head(), base)

    def test_test_writer_commit_is_rejected_and_reset(self):
        def writer(prompt, cwd):
            out = self.write_tests(prompt, cwd)
            agent_commit(cwd)
            return out

        c = self.init(agents={"test_writer": writer})
        c._ensure_worktree(self.layer)
        base = self.layer_head()
        c.step()
        t = c._task("T1")
        self.assertEqual(t["status"], "todo")
        self.assertTrue(any("commit" in n for n in t["notes"]), t["notes"])
        self.assertEqual(self.layer_head(), base)

    def test_planner_commit_is_rejected_and_reset(self):
        task = {"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan", "plan_file": "plan.md",
                "status": "todo"}

        def planner(prompt, cwd):
            (cwd / "plan.md").write_text("plan\n", encoding="utf-8")
            (cwd / "extra.txt").write_text("extra\n", encoding="utf-8")
            agent_commit(cwd)
            return '{"tasks":[]}', 1

        c = self.init(task, agents={"planner": planner})
        c._ensure_worktree(self.layer)
        base = self.layer_head()
        c.step()
        t = c._task("P1")
        self.assertNotEqual(t["status"], "done")
        self.assertTrue(any("commit" in n for n in t["notes"]), t["notes"])
        self.assertEqual(self.layer_head(), base)
        self.assertNotIn("extra.txt", self.branch_files())

    def test_troubleshooter_commit_discards_its_notes(self):
        holder = {}

        def trouble(prompt, cwd):
            (holder["c"].wt / "evil.txt").write_text("x\n", encoding="utf-8")
            agent_commit(holder["c"].wt)
            return '{"kind":"fix","notes":"forged notes"}', 1

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "troubleshooter": trouble})
        holder["c"] = c
        base = self.layer_head()
        c._troubleshoot("T1", "stuck", "")
        self.assertEqual(self.layer_head(), base)
        notes = c._task("T1")["trouble_notes"]
        self.assertNotIn("forged notes", notes)
        self.assertTrue(any("commit" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
