"""T1B3e: the conductor finalizes reviewed work through core.finalize (journal, safe push, crash recovery).

Real git throughout: a bare `origin`, the main repo, the layer worktree, and a second clone that plays
"someone else pushing to origin". Agents are fakes. Crashes are BaseException subclasses, so nothing in
the conductor may swallow them; a fresh Conductor then plays the restarted process.
"""
import contextlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from core import finalize
from core.bootstrap import Conductor, Team
from core.finalize import ApprovedMerges, Journal
from core.ledger import Ledger
from core.roles import DEFAULTS
from tests.core.test_bootstrap import Harness, git, py_test

MUTATION_NA = "not applicable: merge commit adds no builder lines"
CRASH_POINTS = ["after:journal-begin", "before:ff", "after:ff", "before:candidate-merge", "after:candidate-merge",
                "before:judge", "after:judge", "before:review", "after:review", "before:approve", "after:approve",
                "before:candidate-ff", "after:candidate-ff", "before:push", "after:push", "after:drift",
                "after:cleanup", "before:ledger", "after:ledger", "after:queue"]
HAPPY_POINTS = ["after:journal-begin", "before:ff", "after:ff", "before:push", "after:push", "after:drift",
                "after:cleanup", "before:ledger", "after:ledger", "after:queue"]


class Crash(BaseException):
    """A simulated process death."""


class CrashingConductor(Conductor):
    crash_at = None
    fired = None

    def _crash(self, point):
        if point == self.crash_at and not self.fired:
            self.fired = point
            raise Crash(point)


class Pipeline(Harness):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "origin", "main")
        self.other = root / "other"
        self.log = root / "judge.log"
        self.review_log = []
        self.agents = {"test_writer": self.writer, "builder": self.builder, "reviewer": self.reviewer}
        self.teams = []

    # ---------------------------------------------------------------- fakes
    @staticmethod
    def files_of(prompt, label):
        line = next(ln for ln in prompt.splitlines() if ln.startswith(label))
        return [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]

    def writer(self, prompt, cwd):
        tf = self.files_of(prompt, "Test files")[0]
        mod = Path(self.files_of(prompt, "Files you may change")[0]).stem
        value = 1 if mod == "other" else 42
        p = cwd / tf
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "import os, subprocess, unittest\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as f:\n"
            f"    f.write({mod!r} + '|' + os.getcwd() + '|' + subprocess.run(['git', 'rev-parse', 'HEAD'], "
            "capture_output=True, text=True).stdout.strip() + '\\n')\n"
            f"import {mod}\n"
            "class T(unittest.TestCase):\n"
            f"    def test_value(self): self.assertEqual({mod}.VALUE, {value})\n", encoding="utf-8")
        return json.dumps({"files": [tf]}), 1

    def builder(self, prompt, cwd):
        f = self.files_of(prompt, "Files you may change")[0]
        (cwd / f).write_text(f"VALUE = {1 if f == 'other.py' else 42}\n", encoding="utf-8")
        return '{"status":"done"}', 1

    def reviewer(self, prompt, cwd):
        self.review_log.append({"merge": "MERGE COMMIT" in prompt, "prompt": prompt, "cwd": Path(cwd),
                                "head": git(cwd, "rev-parse", "HEAD"),
                                "layer": git(self.repo, "rev-parse", f"refs/heads/{self.layer}")})
        return '{"verdict":"pass","reasons":["looks right"]}', 1

    # ---------------------------------------------------------------- setup helpers
    def conductor(self, cls=Conductor, crash_at=None, judge_cmds=None):
        self.make_conductor(self.agents)
        self.teams.append(self.team)
        c = cls(self.repo, self.work, self.state, self.team, self.c.limits, owner_email="ben@example.com",
                mailer=lambda s, b: self.mails.append((s, b)), inbox=lambda: self.messages, gh=self.gh,
                judge_cmds=list(judge_cmds or []), push=True)
        if crash_at:
            c.crash_at = crash_at
        self.c = c
        return c

    def tasks(self, *ids):
        out = []
        for tid in ids:
            if tid in ("T0", "T2"):
                out.append(self.task(id=tid, title=f"Other {tid}", files_in_scope=["other.py"],
                                     test_files=["tests/core/test_other.py"],
                                     test_cmd=py_test("tests/core/test_other.py")))
            else:
                out.append(self.task(id=tid))
        return out

    def start(self, *ids, cls=Conductor, crash_at=None, judge_cmds=None):
        c = self.conductor(cls, crash_at, judge_cmds)
        c.init_queue(self.layer, self.tasks(*(ids or ("T1",))))
        git(self.repo, "push", "-q", "origin", self.layer)
        git(self.repo, "fetch", "-q", "origin")
        return c

    def push_from_other(self, name, text, message="someone else"):
        if not self.other.exists():
            git(self.origin.parent, "clone", "-q", str(self.origin), str(self.other))
            git(self.other, "config", "user.name", "Other")
            git(self.other, "config", "user.email", "other@example.com")
        git(self.other, "fetch", "-q", "origin")
        git(self.other, "checkout", "-q", "-B", self.layer, f"origin/{self.layer}")
        p = self.other / name
        if text is None:
            git(self.other, "rm", "-q", name)
        else:
            p.write_text(text, encoding="utf-8")
            git(self.other, "add", name)
        git(self.other, "commit", "-q", "-m", message)
        git(self.other, "push", "-q", "origin", self.layer)
        return git(self.other, "rev-parse", "HEAD")

    # ---------------------------------------------------------------- observations
    def origin_tip(self):
        return subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{self.layer}"], cwd=self.origin,
                              capture_output=True, text=True).stdout.strip()

    def origin_contains(self, sha):
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, f"refs/heads/{self.layer}"],
                              cwd=self.origin, capture_output=True).returncode == 0

    def queue_task(self, tid):
        q = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        return next(t for t in q["tasks"] if t["id"] == tid)

    def passes(self, cid):
        return [e for e in Ledger(self.state).events() if e["action"] == "pass" and e["contract_id"] == cid]

    def task_commit(self, cid):
        return Ledger(self.state).contracts()[cid]["commit"]

    def merge_mails(self):
        return [s for s, b in self.mails if "[Forge Q-merge-" in s]

    def check_invariants(self, *tids):
        for tid in tids:
            t = self.queue_task(tid)
            passes = self.passes(tid)
            self.assertLessEqual(len(passes), 1, f"{tid}: more than one pass event")
            rec = Journal(self.state).load(tid)
            if t["status"] == "done":
                self.assertTrue(passes, f"{tid}: queue done without a pass event")
                self.assertTrue(self.origin_contains(t["done_commit"]), f"{tid}: done before origin has S")
            if passes:
                self.assertIsNotNone(rec, f"{tid}: pass event without a journal")
                self.assertTrue(rec["pushed"] and rec["drift_marked"] and rec["cleaned"],
                                f"{tid}: pass before pushed/drift/cleanup: {rec['phase']}")

    def run_until_settled(self, c, tids, limit=20):
        results = []
        for _ in range(limit):
            r = c.step()
            results.append(r)
            self.check_invariants(*tids)
            if r in ("gate", "idle"):
                break
        return results

    def drift_prompts(self):
        return sum(len(t.drift_keeper.prompts) for t in self.teams)


class HappyPathTests(Pipeline):
    def test_happy_path_done_only_after_origin_has_s(self):
        c = self.start("T1")
        self.assertEqual(c.step(), "worked")  # tests
        self.assertEqual(c.step(), "worked")  # build + finalize
        s = self.task_commit("T1")
        t = self.queue_task("T1")
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["done_commit"], s)
        self.assertEqual(self.origin_tip(), s)
        comp = Ledger(self.state).completion("T1")
        self.assertIsNotNone(comp)
        self.assertEqual(comp["payload"]["task_commit"], s)
        self.assertEqual(comp["payload"]["final_sha"], s)
        self.assertEqual(comp["payload"]["merges"], [])
        self.assertIs(comp["payload"]["pushed"], True)
        self.assertEqual(comp["payload"]["verdict"], "pass")
        self.assertEqual(comp["payload"]["reasons"], ["looks right"])
        self.assertEqual(Ledger(self.state).contracts()["T1"]["commit"], s)
        self.assertEqual(Ledger(self.state).contracts()["T1"]["status"], "done")
        self.assertFalse((self.work / "tasks" / "T1").exists())
        self.assertNotIn("forge-task/T1", git(self.repo, "branch", "--format=%(refname:short)").split())
        rec = Journal(self.state).load("T1")
        self.assertEqual(rec["status"], "finished")
        q = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        self.assertTrue(q["drift_due"])
        self.assertEqual(q["drift_marks"], ["T1"])
        self.assertEqual(c.step(), "worked")  # drift keeper
        self.assertEqual(len(self.team.drift_keeper.prompts), 1)
        self.assertEqual(c.step(), "gate")
        self.assertTrue(any(a[:2] == ["pr", "create"] for a in self.gh_calls))
        self.check_invariants("T1")

    def test_pass_is_applied_only_by_the_finalizer_after_push(self):
        seen = []
        real = Ledger.apply

        def spy(led, proposal, identity):
            if proposal["action"] == "pass":
                seen.append(self.origin_tip())
            return real(led, proposal, identity)

        c = self.start("T1")
        with patch.object(Ledger, "apply", spy):
            c.step()
            c.step()
        self.assertEqual(seen, [self.task_commit("T1")])

    def test_task_with_journal_never_starts_a_build(self):
        c = self.start("T1")
        c.step()
        Journal(self.state).begin("T1", "T1", "a" * 40, "b" * 40, "run", {"verdict": "pass", "reasons": []}, {})
        rec = Journal(self.state).load("T1")
        rec["status"] = "blocked"
        rec["blocked"] = {"reason": "x", "qid": "merge-99", "other": None}
        Journal(self.state).save(rec)
        c._build_stage("T1")
        self.assertEqual(self.team.builder.prompts, [])
        self.assertEqual(c.step(), "idle")
        self.assertEqual(self.team.builder.prompts, [])
        self.assertEqual(self.queue_task("T1")["status"], "merge_pending")

    def test_mark_drift_is_one_durable_queue_write(self):
        c = self.start("T1")
        writes = []
        real = Conductor._write

        def spy(conductor, name, data, *a, **kw):
            if name == "queue.json":
                writes.append((json.loads(json.dumps(data)), a, kw))
            return real(conductor, name, data, *a, **kw)

        fsyncs = []
        real_fsync = __import__("os").fsync
        with patch.object(Conductor, "_write", spy), \
                patch("core.bootstrap.os.fsync", side_effect=lambda fd: fsyncs.append(fd) or real_fsync(fd)):
            hooks = c._hooks()
            hooks.mark_drift("T1")
            hooks.mark_drift("T1")
        self.assertEqual(len(writes), 2)
        data = writes[0][0]
        self.assertTrue(data["drift_due"])
        self.assertEqual(data["drift_marks"], ["T1"])
        self.assertEqual(writes[1][0]["drift_marks"], ["T1"])  # no duplicates
        self.assertTrue(fsyncs, "the drift-mark queue write must be flushed to disk")
        self.assertTrue(hooks.drift_marked("T1"))
        self.assertFalse(hooks.drift_marked("T2"))


class DivergenceTests(Pipeline):
    def test_clean_divergence_is_judged_and_reviewed_before_layer_moves(self):
        c = self.start("T0", "T1")
        for _ in range(3):  # T0: tests, build+finalize, drift
            c.step()
        self.assertEqual(self.queue_task("T0")["status"], "done")
        x = self.push_from_other("notes.txt", "from elsewhere\n")
        self.assertEqual(c.step(), "worked")  # T1 tests
        self.log.write_text("", encoding="utf-8")
        self.assertEqual(c.step(), "worked")  # T1 build + finalize with a merge candidate
        s = self.task_commit("T1")
        t = self.queue_task("T1")
        self.assertEqual(t["status"], "done")
        m = self.origin_tip()
        self.assertEqual(git(self.repo, "rev-list", "--parents", "-n", "1", m).split()[1:], [s, x])
        self.assertEqual(git(self.repo, "rev-parse", f"refs/heads/{self.layer}"), m)
        self.assertEqual(t["final_sha"], m)
        self.assertEqual(t["merge_candidates"], [m])
        self.assertTrue(ApprovedMerges(self.state).has(m))
        # judged at exactly M in a throwaway: T1's own tests and every done task's tests (T0)
        lines = [ln.split("|") for ln in self.log.read_text(encoding="utf-8").splitlines()]
        at_m = [(mod, Path(cwd)) for mod, cwd, head in lines if head == m]
        self.assertEqual(sorted(mod for mod, _ in at_m), ["feat", "other"])
        for _, cwd in at_m:
            self.assertEqual(cwd.resolve().parent, (self.work / "tmp").resolve())
            self.assertFalse(cwd.exists())
        # reviewed at M, before the layer moved to M
        merge_reviews = [r for r in self.review_log if r["merge"]]
        self.assertEqual(len(merge_reviews), 1)
        rv = merge_reviews[0]
        self.assertEqual(rv["head"], m)
        self.assertNotEqual(rv["layer"], m)
        self.assertEqual(rv["cwd"].resolve().parent, (self.work / "tmp").resolve())
        self.assertTrue(rv["prompt"].startswith(DEFAULTS["reviewer"] + "\n\n"))
        self.assertIn("You are reviewing a MERGE COMMIT created because the layer branch moved", rv["prompt"])
        self.assertIn("TASK T1", rv["prompt"])
        self.assertIn("notes.txt", rv["prompt"])  # git diff <base>..<M>
        self.assertIn("feat.py", rv["prompt"])  # git diff <other>..<M>
        # ledger evidence
        run_id = f"ci-merge-T1-{m[:12]}"
        runs = Ledger(self.state).test_runs()
        self.assertEqual(runs[run_id], {"contract_id": "T1", "commit": m, "passed": True})
        comp = Ledger(self.state).completion("T1")["payload"]
        self.assertEqual(comp["task_commit"], s)
        self.assertEqual(comp["final_sha"], m)
        self.assertEqual(len(comp["merges"]), 1)
        mg = comp["merges"][0]
        self.assertEqual((mg["sha"], mg["kind"], mg["run_id"], mg["verdict"], mg["reasons"]),
                         (m, "divergence", run_id, "pass", ["looks right"]))
        self.assertEqual(mg["parents"], [s, x])
        self.assertEqual(mg["mutation"], MUTATION_NA)
        self.check_invariants("T0", "T1")

    def test_failing_merge_judge_blocks_without_pushing(self):
        c = self.start("T1")
        c.step()
        c.judge_cmds = [f'"{sys.executable}" -c "import os, sys; sys.exit(1 if os.path.exists(\'notes.txt\') else 0)"']
        x = self.push_from_other("notes.txt", "from elsewhere\n")
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.origin_tip(), x)
        self.assertEqual(self.queue_task("T1")["status"], "merge_pending")
        self.assertEqual(Journal(self.state).load("T1")["status"], "blocked")
        self.assertEqual(len(self.merge_mails()), 1)
        self.assertEqual(self.passes("T1"), [])
        run_ids = [k for k, v in Ledger(self.state).test_runs().items() if k.startswith("ci-merge-T1-")]
        self.assertEqual(len(run_ids), 1)
        self.assertFalse(Ledger(self.state).test_runs()[run_ids[0]]["passed"])

    def test_merge_reviewer_unusable_output_is_a_fail_verdict(self):
        def reviewer(prompt, cwd):
            if "MERGE COMMIT" in prompt:
                return "not json", 1
            return '{"verdict":"pass","reasons":["ok"]}', 1
        self.agents["reviewer"] = reviewer
        c = self.start("T1")
        c.step()
        self.push_from_other("notes.txt", "from elsewhere\n")
        c.step()
        rec = Journal(self.state).load("T1")
        self.assertEqual(rec["status"], "blocked")
        cand = rec["candidates"][-1]
        self.assertEqual(cand["verdict"], "fail")
        self.assertTrue(cand["reasons"][0].startswith("reviewer output unusable:"))


class ConflictTests(Pipeline):
    def test_conflict_waits_for_ben_while_other_work_continues(self):
        c = self.start("T1", "T2")
        c.step()  # T1 tests
        x = self.push_from_other("feat.py", "VALUE = 7\n")
        self.assertEqual(c.step(), "worked")  # T1 build; finalize blocks on the conflict
        rec1 = Journal(self.state).load("T1")
        self.assertEqual(rec1["status"], "blocked")
        self.assertEqual(rec1["blocked"]["reason"], "merge_conflict")
        self.assertEqual(rec1["blocked"]["other"], x)
        self.assertEqual(len(self.merge_mails()), 1)
        self.assertEqual(self.origin_tip(), x)
        self.assertEqual(self.queue_task("T1")["status"], "merge_pending")
        j1 = (self.state / "merges" / "T1.json").read_bytes()
        # the second task runs its test and build stages meanwhile, and hits the same conflict
        self.assertEqual(c.step(), "worked")
        self.assertEqual(self.queue_task("T2")["status"], "tests_ok")
        self.assertEqual(c.step(), "worked")
        rec2 = Journal(self.state).load("T2")
        self.assertEqual(rec2["status"], "blocked")
        self.assertEqual(rec2["blocked"]["qid"], rec1["blocked"]["qid"])  # same question reused
        self.assertEqual(len(self.merge_mails()), 1)
        self.assertEqual((self.state / "merges" / "T1.json").read_bytes(), j1)
        j2 = (self.state / "merges" / "T2.json").read_bytes()
        self.assertEqual(len([p for p in self.team.builder.prompts if "TASK T1" in p]), 1)
        # repeated steps and a restart never retry blocked records
        calls = []
        real_run = finalize._run

        def spy(cwd, *args):
            calls.append(args)
            return real_run(cwd, *args)

        with patch("core.finalize._run", side_effect=spy):
            for _ in range(3):
                self.assertEqual(c.step(), "idle")
            c2 = self.conductor()
            for _ in range(3):
                self.assertEqual(c2.step(), "idle")
        self.assertFalse([a for a in calls if "merge" in a], calls)
        self.assertEqual((self.state / "merges" / "T1.json").read_bytes(), j1)
        self.assertEqual((self.state / "merges" / "T2.json").read_bytes(), j2)
        self.assertEqual(len(self.merge_mails()), 1)
        self.assertEqual(self.origin_tip(), x)
        self.assertEqual(self.team.builder.prompts, [])
        # a reply with the wrong code does nothing
        subject = self.merge_mails()[0]
        qid = rec1["blocked"]["qid"]
        self.messages[:] = [{"from": "ben@example.com", "subject": f"Re: [Forge Q-{qid} ZZZZZZZZ]", "body": "go"}]
        self.assertEqual(c2.step(), "idle")
        self.assertEqual(Journal(self.state).load("T1")["status"], "blocked")
        # Ben resolves the remote, then answers with the code
        self.push_from_other("feat.py", None, "revert the conflicting change")
        self.messages[:] = [{"from": "ben@example.com", "subject": "Re: " + subject, "body": "Fixed on origin, retry."}]
        results = self.run_until_settled(c2, ["T1", "T2"])
        self.messages[:] = []
        self.assertEqual(results[-1], "gate", results)
        qs = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))
        self.assertEqual(qs[qid]["status"], "answered")
        for tid in ("T1", "T2"):
            self.assertEqual(self.queue_task(tid)["status"], "done")
            self.assertEqual(len(self.passes(tid)), 1)
            self.assertTrue(self.origin_contains(self.task_commit(tid)))
            self.assertEqual(Journal(self.state).load(tid)["status"], "finished")
            self.assertIn("Ben: Fixed on origin, retry.", Journal(self.state).load(tid)["notes"])
        self.assertEqual(git(self.repo, "rev-parse", f"refs/heads/{self.layer}"), self.origin_tip())
        self.assertEqual(len(self.merge_mails()), 1)
        self.assertTrue(any(a[:2] == ["pr", "create"] for a in self.gh_calls))


class UnapprovedMergePushTests(Pipeline):
    def sneak_merge(self):
        wt = self.work / self.layer
        git(wt, "checkout", "-q", "-b", "side")
        (wt / "side.txt").write_text("side\n", encoding="utf-8")
        git(wt, "add", "side.txt")
        git(wt, "commit", "-q", "-m", "side")
        git(wt, "checkout", "-q", self.layer)
        git(wt, "merge", "-q", "--no-ff", "-m", "sneaky merge", "side")
        return git(wt, "rev-parse", "HEAD")

    def test_gate_push_refuses_unapproved_merge(self):
        c = self.start("T1")
        for _ in range(3):
            c.step()
        before = self.origin_tip()
        self.sneak_merge()
        self.assertEqual(c.step(), "error")
        self.assertEqual(self.origin_tip(), before)
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in self.gh_calls))
        self.assertIn("refused", (self.state / "errors.log").read_text(encoding="utf-8"))

    def test_plan_stage_push_refuses_unapproved_merge(self):
        plan = {"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan it", "plan_file": "plan.md"}

        def planner(prompt, cwd):
            (cwd / "plan.md").write_text("plan\n", encoding="utf-8")
            return json.dumps({"tasks": [self.task(id="T5")]}), 1

        self.agents["planner"] = planner
        c = self.conductor()
        c.init_queue(self.layer, [plan])
        git(self.repo, "push", "-q", "origin", self.layer)
        before = self.origin_tip()
        self.sneak_merge()
        self.assertEqual(c.step(), "error")
        self.assertEqual(self.origin_tip(), before)
        self.assertIn("refused", (self.state / "errors.log").read_text(encoding="utf-8"))

    def test_push_off_does_nothing(self):
        c = self.start("T1")
        c.push = False
        before = self.origin_tip()
        with patch("core.bootstrap.safe_push") as sp:
            c._push()
        sp.assert_not_called()
        self.assertEqual(self.origin_tip(), before)

    def test_rejected_push_is_an_error_not_success(self):
        c = self.start("T1")
        with patch("core.bootstrap.safe_push", return_value=("rejected", "! [rejected] non-fast-forward")):
            with self.assertRaises(RuntimeError):
                c._push()
        with patch("core.bootstrap.safe_push", return_value=("error", "network down")):
            with self.assertRaises(RuntimeError):
                c._push()
        with patch("core.bootstrap.safe_push", return_value=("ok", "")):
            c._push()


class CrashRecoveryTests(Pipeline):
    """A crash at every point: a restarted conductor finishes exactly once."""

    def crash_scenario(self, point, diverge, ledger_crash=False):
        c = self.start("T1", cls=CrashingConductor, crash_at=None if ledger_crash else point)
        self.assertEqual(c.step(), "worked")
        x = self.push_from_other("notes.txt", "from elsewhere\n") if diverge else None
        crashed = []
        real_append = Ledger._append_event

        def append(led, event):
            if event["action"] == "pass" and not crashed:
                crashed.append("ledger")
                raise Crash("inside the ledger pass")
            return real_append(led, event)

        ctx = patch.object(Ledger, "_append_event", append) if ledger_crash else contextlib.nullcontext()
        with ctx:
            for _ in range(6):
                try:
                    c.step()
                except Crash as e:
                    crashed.append(str(e))
                    break
                self.check_invariants("T1")
        self.assertTrue(crashed, f"crash point {point} was never reached")
        self.check_invariants("T1")
        c2 = self.conductor()
        results = self.run_until_settled(c2, ["T1"])
        self.assertEqual(results[-1], "gate", results)
        s = self.task_commit("T1")
        self.assertEqual(self.queue_task("T1")["status"], "done")
        self.assertEqual(len(self.passes("T1")), 1)
        self.assertTrue(self.origin_contains(s))
        self.assertGreaterEqual(self.drift_prompts(), 1)
        self.assertFalse((self.work / "tasks" / "T1").exists())
        self.assertNotIn("forge-task/T1", git(self.repo, "branch", "--format=%(refname:short)").split())
        self.assertEqual(Journal(self.state).load("T1")["status"], "finished")
        self.assertEqual(self.merge_mails(), [])
        comp = Ledger(self.state).completion("T1")["payload"]
        self.assertEqual(comp["task_commit"], s)
        self.assertEqual(comp["final_sha"], self.origin_tip())
        if diverge:
            m = self.origin_tip()
            self.assertEqual(git(self.repo, "rev-list", "--parents", "-n", "1", m).split()[1:], [s, x])
            self.assertEqual([g["sha"] for g in comp["merges"]], [m])
        return c

    def test_crash_inside_ledger_pass(self):
        self.crash_scenario(None, diverge=False, ledger_crash=True)

    def test_crash_inside_ledger_pass_after_divergence(self):
        self.crash_scenario(None, diverge=True, ledger_crash=True)

    def test_crash_while_asking_never_asks_twice(self):
        for point in ("before:ask", "after:ask"):
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                c = self.start("T1", cls=CrashingConductor, crash_at=point)
                c.step()
                self.push_from_other("feat.py", "VALUE = 7\n")
                with self.assertRaises(Crash):
                    c.step()
                c2 = self.conductor()
                self.assertEqual(c2.step(), "worked")  # finishes recording the block, asking nothing new
                for _ in range(2):
                    self.assertEqual(c2.step(), "idle")
                rec = Journal(self.state).load("T1")
                self.assertEqual(rec["status"], "blocked")
                self.assertEqual(len(self.merge_mails()), 1)
                qs = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))
                self.assertEqual([k for k, v in qs.items() if v["kind"] == "merge"], [rec["blocked"]["qid"]])


def _add_crash_tests():
    for point in CRASH_POINTS:
        name = point.replace(":", "_").replace("-", "_")

        def diverged(self, point=point):
            self.crash_scenario(point, diverge=True)

        setattr(CrashRecoveryTests, f"test_crash_{name}_with_divergence", diverged)
        if point in HAPPY_POINTS:
            def plain(self, point=point):
                self.crash_scenario(point, diverge=False)

            setattr(CrashRecoveryTests, f"test_crash_{name}", plain)


_add_crash_tests()


if __name__ == "__main__":
    unittest.main()
