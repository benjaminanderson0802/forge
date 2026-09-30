"""T1B3d: merge journal, approved-merge registry, safe push and the crash-safe finalizer.

Every scenario uses real git: a bare `origin`, a main repo, a layer worktree, and a second clone that
plays "someone else pushing to origin". Hooks are fakes that record every call.
"""
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core import finalize
from core.finalize import ApprovedMerges, Finalizer, Hooks, Journal, safe_push, unapproved_merges

LAYER = "layer-1"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_TERMINAL_PROMPT": "0"}


def git(cwd, *args, check=True, env=None, input=None):
    # Binary pipes: in text mode Windows rewrites "\n" as "\r\n" on the way to git, so
    # `hash-object --stdin` would store CRLF blobs that differ from the LF ones `git add` makes
    # under core.autocrlf=true (finding 6).
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, env=env or ENV,
                       input=None if input is None else input.encode("utf-8"))
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace')}")
    return out.strip()


class Boom(BaseException):
    """A simulated process crash (not an Exception, so nothing may swallow it)."""


class FakeTrees:
    """Stands in for core.worktrees.Worktrees (T1B3b): real throwaway worktrees, recorded task removal."""

    def __init__(self, repo: Path, work: Path, world):
        self.repo, self.work, self.world = repo, work, world
        self.throwaway_shas = []

    @contextlib.contextmanager
    def throwaway(self, sha):
        path = self.work / "tmp" / uuid.uuid4().hex[:12]
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(path), sha)
        self.throwaway_shas.append(git(path, "rev-parse", "HEAD"))
        try:
            yield path
        finally:
            git(self.repo, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            git(self.repo, "worktree", "prune", check=False)

    def remove_task(self, tid):
        self.world.effect("remove_task", tid)


class World:
    """Fake conductor side: queue, ledger, drift marks, questions, and a crash switch."""

    def __init__(self, env):
        self.env = env
        self.calls = []            # every hook call, in order: (name, arg)
        self.effects = []          # only the calls that change something
        self.completed = {}        # cid -> number of effective ledger passes
        self.drift = set()
        self.tasks = {}
        self.questions = {}        # qid -> {"subject", "tid", "open"}
        self.snapshots = {}        # hook name -> journal record on disk at call time
        self.judge_fn = lambda sha: {"passed": True, "run_id": f"ci-{sha[:12]}", "output": "ok"}
        self.review_fn = lambda rec, cand: {"verdict": "pass", "reasons": ["merge looks right"]}
        self.judge_seen = []       # (sha, layer HEAD, approved?) at judge time
        self.review_seen = []
        self.crash_at = None
        self.crashed = False
        self.points = []
        self.on_point = None

    def effect(self, name, arg):
        self.calls.append((name, arg))
        self.effects.append((name, arg))

    def disk(self, tid):
        return json.loads((self.env.state / "merges" / f"{tid}.json").read_text(encoding="utf-8"))

    # hooks -----------------------------------------------------------------
    def judge(self, sha):
        self.calls.append(("judge", sha))
        self.effects.append(("judge", sha))
        self.judge_seen.append((sha, self.env.layer_head(), ApprovedMerges(self.env.state).has(sha)))
        return self.judge_fn(sha)

    def review(self, rec, cand):
        self.calls.append(("review", cand.get("sha")))
        self.effects.append(("review", cand.get("sha")))
        self.review_seen.append((cand.get("sha"), self.env.layer_head(), ApprovedMerges(self.env.state).has(cand["sha"])))
        return self.review_fn(rec, cand)

    def ask(self, kind, subject, body, tid):
        qid = f"q{len(self.questions) + 1}"
        self.effect("ask", (kind, subject, body, tid))
        self.questions[qid] = {"subject": subject, "tid": tid, "open": True, "body": body, "kind": kind}
        return qid

    def question_open(self, qid):
        self.calls.append(("question_open", qid))
        return self.questions.get(qid, {}).get("open", False)

    def find_question(self, kind, subject, tid):
        self.calls.append(("find_question", tid))
        for qid, q in self.questions.items():
            if q["open"] and q["tid"] == tid and q["subject"] == subject and q["kind"] == kind:
                return qid
        return None

    def mark_drift(self, tid):
        self.effect("mark_drift", tid)
        self.drift.add(tid)

    def drift_marked(self, tid):
        self.calls.append(("drift_marked", tid))
        return tid in self.drift

    def ledger_pass(self, rec):
        self.effect("ledger_pass", rec["tid"])
        self.snapshots["ledger_pass"] = self.disk(rec["tid"])
        if self.completed.get(rec["cid"]):
            raise RuntimeError("ledger refused: already completed")
        self.completed[rec["cid"]] = self.completed.get(rec["cid"], 0) + 1

    def ledger_completed(self, cid):
        self.calls.append(("ledger_completed", cid))
        return bool(self.completed.get(cid))

    def set_task(self, tid, changes):
        self.effect("set_task", (tid, dict(changes)))
        if changes.get("status") == "done":
            self.snapshots.setdefault("set_task_done", []).append(self.disk(tid))
        self.tasks.setdefault(tid, {"id": tid}).update(changes)

    def get_task(self, tid):
        self.calls.append(("get_task", tid))
        t = self.tasks.get(tid)
        return dict(t) if t else None

    def crash(self, point):
        self.points.append(point)
        if self.on_point:
            self.on_point(point)
        if point == self.crash_at and not self.crashed:
            self.crashed = True
            raise Boom(point)

    def hooks(self):
        return Hooks(judge=self.judge, review=self.review, ask=self.ask, question_open=self.question_open,
                     mark_drift=self.mark_drift, drift_marked=self.drift_marked, ledger_pass=self.ledger_pass,
                     ledger_completed=self.ledger_completed, set_task=self.set_task, crash=self.crash,
                     get_task=self.get_task, find_question=self.find_question)

    def count(self, name, pred=lambda a: True):
        return sum(1 for n, a in self.effects if n == name and pred(a))

    def done_sets(self):
        return self.count("set_task", lambda a: a[1].get("status") == "done")


class Env:
    """origin (bare), repo (main checkout), work/<layer> (layer worktree), other (someone else's clone)."""

    def __init__(self, tc: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.origin, self.repo, self.work, self.state = root / "origin.git", root / "repo", root / "work", root / "state"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "README.md").write_bytes(b"initial\n")  # LF bytes on every platform
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.T0 = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", LAYER)
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "origin", "main", LAYER)
        git(self.repo, "fetch", "-q", "origin")
        self.layer_wt = self.work / LAYER
        git(self.repo, "worktree", "add", "-q", str(self.layer_wt), LAYER)
        self.other = root / "other"
        git(root, "clone", "-q", str(self.origin), str(self.other))
        self.world = World(self)
        self.trees = FakeTrees(self.repo, self.work, self.world)

    # git helpers -------------------------------------------------------------
    def commit(self, parents, files, msg, where=None):
        """Create a commit object with plumbing (no checkout touched) and return its sha."""
        where = where or self.repo
        parents = [parents] if isinstance(parents, str) else list(parents)
        env = {**ENV, "GIT_INDEX_FILE": str(self.root / f"idx-{uuid.uuid4().hex}")}
        git(where, "read-tree", parents[0], env=env)
        for path, content in files.items():
            blob = git(where, "hash-object", "-w", "--stdin", input=content)
            git(where, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        tree = git(where, "write-tree", env=env)
        args = ["commit-tree", tree, "-m", msg]
        for p in parents:
            args += ["-p", p]
        return git(where, *args, env=env)

    def push_other(self, files, msg):
        """Someone else pushes a commit onto origin's layer branch; returns its sha."""
        git(self.other, "fetch", "-q", "origin")
        parent = git(self.other, "rev-parse", f"refs/remotes/origin/{LAYER}")
        sha = self.commit(parent, files, msg, where=self.other)
        git(self.other, "push", "-q", "origin", f"{sha}:refs/heads/{LAYER}")
        return sha

    def origin_head(self):
        return git(self.origin, "rev-parse", f"refs/heads/{LAYER}")

    def layer_head(self):
        return git(self.layer_wt, "rev-parse", "HEAD")

    def parents(self, sha):
        return git(self.repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]

    def is_ancestor(self, a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(self.repo),
                              capture_output=True).returncode == 0

    def remote_merges_all_approved(self):
        merges = git(self.origin, "rev-list", "--merges", f"refs/heads/{LAYER}").split()
        reg = ApprovedMerges(self.state)
        return [m for m in merges if not reg.has(m)]

    def candidate_refs(self):
        return git(self.repo, "for-each-ref", "--format=%(refname)", "refs/forge/candidates/").split()

    # finalizer helpers -------------------------------------------------------
    def journal(self):
        return Journal(self.state)

    def begin(self, tid, sha):
        self.world.tasks[tid] = {"id": tid, "status": "merge_pending"}
        return self.journal().begin(tid, f"C-{tid}", sha, self.T0, f"ci-{tid}",
                                    {"verdict": "pass", "reasons": ["good"]}, {"mutation": "caught 5/5"})

    def finalizer(self, push=True, max_rounds=3):
        return Finalizer(self.layer_wt, LAYER, self.trees, Journal(self.state), ApprovedMerges(self.state),
                         self.world.hooks(), push=push, max_rounds=max_rounds)


# --------------------------------------------------------------------------- journal and registry
class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.j = Journal(self.state)

    def test_begin_creates_full_record_on_disk(self):
        rec = self.j.begin("T1", "C1", "a" * 40, "b" * 40, "ci-1", {"verdict": "pass", "reasons": ["r"]}, {"e": 1})
        expected = {"tid": "T1", "cid": "C1", "task_sha": "a" * 40, "base": "b" * 40, "ci_run_id": "ci-1",
                    "review": {"verdict": "pass", "reasons": ["r"]}, "evidence": {"e": 1},
                    "pass_pid": "C1-pass-" + "a" * 12, "phase": "merge", "status": "active", "blocked": None,
                    "intent": None, "candidates": [], "final_sha": None, "pushed": None, "drift_marked": False,
                    "cleaned": False, "notes": [], "answers": 0, "rounds": 0}
        for k, v in expected.items():
            self.assertEqual(rec[k], v, k)
        on_disk = json.loads((self.state / "merges" / "T1.json").read_text(encoding="utf-8"))
        for k, v in expected.items():
            self.assertEqual(on_disk[k], v, k)
        self.assertEqual(self.j.load("T1"), rec)

    def test_begin_is_idempotent_for_same_sha_and_refuses_a_different_one(self):
        first = self.j.begin("T1", "C1", "a" * 40, "b" * 40, "ci-1", {"verdict": "pass", "reasons": []}, {})
        first["phase"] = "push"
        self.j.save(first)
        again = self.j.begin("T1", "C1", "a" * 40, "c" * 40, "ci-2", {"verdict": "pass", "reasons": ["x"]}, {"z": 1})
        self.assertEqual(again, first)
        self.assertEqual(again["phase"], "push")
        with self.assertRaises(ValueError):
            self.j.begin("T1", "C1", "d" * 40, "b" * 40, "ci-1", {}, {})
        self.assertEqual(self.j.load("T1")["task_sha"], "a" * 40)

    def test_load_missing_is_none_and_all_sorted(self):
        self.assertIsNone(self.j.load("nope"))
        for tid in ("T3", "T1", "T2"):
            self.j.begin(tid, "C" + tid, "a" * 40, "b" * 40, "ci", {}, {})
        self.assertEqual([r["tid"] for r in self.j.all()], ["T1", "T2", "T3"])

    def test_active_excludes_blocked_and_finished_and_blocked_on(self):
        for tid in ("T1", "T2", "T3", "T4"):
            self.j.begin(tid, "C" + tid, "a" * 40, "b" * 40, "ci", {}, {})
        r2 = self.j.load("T2"); r2["status"] = "blocked"; r2["blocked"] = {"reason": "merge_conflict", "qid": "q9"}
        self.j.save(r2)
        r3 = self.j.load("T3"); r3["status"] = "finished"; self.j.save(r3)
        r4 = self.j.load("T4"); r4["status"] = "blocked"; r4["blocked"] = {"reason": "x", "qid": "q8"}
        self.j.save(r4)
        self.assertEqual([r["tid"] for r in self.j.active()], ["T1"])
        self.assertEqual([r["tid"] for r in self.j.blocked_on("q9")], ["T2"])
        self.assertEqual(self.j.blocked_on("q7"), [])

    def test_unblock_reactivates_caps_notes_and_retires_rejected_candidates(self):
        for tid in ("T1", "T2", "T3"):
            self.j.begin(tid, "C" + tid, "a" * 40, "b" * 40, "ci", {}, {})
        for tid in ("T1", "T2"):
            r = self.j.load(tid)
            r.update(status="blocked", blocked={"reason": "merge_conflict", "qid": "q1", "other": "c" * 40},
                     rounds=2, phase="candidate", intent={"op": "ff", "from": "x", "to": "y"},
                     notes=[f"n{i}" for i in range(35)],
                     candidates=[{"n": 1, "kind": "divergence", "base": "a", "other": "c", "sha": None,
                                  "state": "rejected"},
                                 {"n": 2, "kind": "divergence", "base": "a", "other": "c", "sha": "m" * 40,
                                  "state": "approved"}])
            self.j.save(r)
        r3 = self.j.load("T3"); r3.update(status="blocked", blocked={"reason": "x", "qid": "q2"}); self.j.save(r3)
        self.assertLessEqual(len(self.j.load("T1")["notes"]), 30)
        tids = self.j.unblock("q1", "Z" * 5000)
        self.assertEqual(sorted(tids), ["T1", "T2"])
        for tid in ("T1", "T2"):
            r = self.j.load(tid)
            self.assertEqual(r["status"], "active")
            self.assertIsNone(r["blocked"])
            self.assertEqual(r["rounds"], 0)
            self.assertEqual(r["answers"], 1)
            self.assertEqual(len(r["notes"]), 30)
            self.assertTrue(r["notes"][-1].startswith("Ben: ZZZ"))
            self.assertEqual(len(r["notes"][-1]), 2000)
            self.assertEqual(r["phase"], "merge")
            self.assertIsNone(r["intent"])
            self.assertEqual(r["candidates"][0]["state"], "retired")
            self.assertEqual(r["candidates"][1]["state"], "approved")
        self.assertEqual(self.j.load("T3")["status"], "blocked")
        self.assertEqual(self.j.unblock("q1", "again"), [])

    def test_unblock_from_ledger_phase_stays_in_ledger(self):
        self.j.begin("T1", "C1", "a" * 40, "b" * 40, "ci", {}, {})
        r = self.j.load("T1")
        r.update(status="blocked", phase="ledger", blocked={"reason": "ledger refused completion", "qid": "q5"})
        self.j.save(r)
        self.assertEqual(self.j.unblock("q5", "fixed the ledger"), ["T1"])
        self.assertEqual(self.j.load("T1")["phase"], "ledger")

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        self.j.begin("T1", "C1", "a" * 40, "b" * 40, "ci", {}, {})
        with patch("os.fsync", wraps=os.fsync) as fs, patch("os.replace", wraps=os.replace) as rp:
            r = self.j.load("T1"); r["phase"] = "sync"; self.j.save(r)
            self.assertTrue(fs.called)
            self.assertTrue(rp.called)
        self.assertEqual(sorted(p.name for p in (self.state / "merges").iterdir()), ["T1.json"])

    def test_approved_merges_registry_persists(self):
        reg = ApprovedMerges(self.state)
        self.assertFalse(reg.has("m" * 40))
        self.assertIsNone(reg.get("m" * 40))
        reg.add("m" * 40, {"tid": "T1", "verdict": "pass"})
        again = ApprovedMerges(self.state)
        self.assertTrue(again.has("m" * 40))
        self.assertEqual(again.get("m" * 40)["tid"], "T1")
        self.assertFalse(again.has("n" * 40))
        self.assertTrue((self.state / "approved_merges.json").is_file())


# --------------------------------------------------------------------------- safe push
class SafePushTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def _layer_with_unapproved_merge(self):
        e = self.env
        x = e.commit(e.T0, {"x.txt": "x\n"}, "side")
        u = e.commit([e.T0, x], {"x.txt": "x\n"}, "unreviewed merge")
        s = e.commit(u, {"s.txt": "s\n"}, "task")
        git(e.layer_wt, "reset", "-q", "--hard", s)
        return u, s

    def test_refuses_unapproved_merge_then_pushes_once_approved(self):
        e = self.env
        u, s = self._layer_with_unapproved_merge()
        reg = ApprovedMerges(e.state)
        self.assertEqual(unapproved_merges(e.layer_wt, LAYER, reg), [u])
        status, detail = safe_push(e.layer_wt, LAYER, reg)
        self.assertEqual(status, "refused")
        self.assertIn(u, detail)
        self.assertEqual(e.origin_head(), e.T0)
        reg.add(u, {"tid": "T1"})
        self.assertEqual(unapproved_merges(e.layer_wt, LAYER, reg), [])
        status, _ = safe_push(e.layer_wt, LAYER, reg)
        self.assertEqual(status, "ok")
        self.assertEqual(e.origin_head(), s)

    def test_merges_already_on_origin_are_not_counted(self):
        e = self.env
        u, s = self._layer_with_unapproved_merge()
        git(e.repo, "push", "-q", "origin", f"{u}:refs/heads/{LAYER}")  # someone else's problem now
        git(e.repo, "fetch", "-q", "origin")
        self.assertEqual(unapproved_merges(e.layer_wt, LAYER, ApprovedMerges(e.state)), [])

    def test_falls_back_to_all_origin_refs_when_layer_ref_missing(self):
        e = self.env
        y = e.commit(e.T0, {"y.txt": "y\n"}, "y")
        v = e.commit([e.T0, y], {"y.txt": "y\n"}, "merge on main")
        git(e.repo, "push", "-q", "origin", f"{v}:refs/heads/main")
        git(e.repo, "fetch", "-q", "origin")
        x = e.commit(v, {"x.txt": "x\n"}, "x")
        u = e.commit([v, x], {"x.txt": "x\n"}, "unreviewed")
        s = e.commit(u, {"s.txt": "s\n"}, "task")
        git(e.layer_wt, "reset", "-q", "--hard", s)
        git(e.repo, "update-ref", "-d", f"refs/remotes/origin/{LAYER}")
        self.assertEqual(unapproved_merges(e.layer_wt, LAYER, ApprovedMerges(e.state)), [u])

    def test_non_fast_forward_is_rejected(self):
        e = self.env
        e.push_other({"r.txt": "r\n"}, "other")
        s = e.commit(e.T0, {"s.txt": "s\n"}, "task")
        git(e.layer_wt, "reset", "-q", "--hard", s)
        status, out = safe_push(e.layer_wt, LAYER, ApprovedMerges(e.state))
        self.assertEqual(status, "rejected", out)
        self.assertNotEqual(e.origin_head(), s)

    def test_other_push_failure_is_error(self):
        e = self.env
        s = e.commit(e.T0, {"s.txt": "s\n"}, "task")
        git(e.layer_wt, "reset", "-q", "--hard", s)
        git(e.repo, "remote", "set-url", "origin", str(e.root / "missing.git"))
        status, _ = safe_push(e.layer_wt, LAYER, ApprovedMerges(e.state))
        self.assertEqual(status, "error")


# --------------------------------------------------------------------------- finalizer
FF_POINTS = ["before:ff", "after:ff", "before:push", "after:push", "after:drift", "after:cleanup",
             "before:ledger", "after:ledger", "after:queue"]
DIV_POINTS = ["before:ff", "after:ff", "before:candidate-merge", "after:candidate-merge", "before:judge",
              "after:judge", "before:review", "after:review", "before:approve", "after:approve",
              "before:candidate-ff", "after:candidate-ff", "before:push", "after:push", "after:drift",
              "after:cleanup", "before:ledger", "after:ledger", "after:queue"]


class FinalizerTests(unittest.TestCase):
    def ff_env(self):
        e = Env(self)
        s = e.commit(e.T0, {"a.txt": "task\n"}, "task T1")
        e.begin("T1", s)
        return e, s

    def div_env(self):
        e = Env(self)
        s = e.commit(e.T0, {"a.txt": "task\n"}, "task T1")
        r = e.push_other({"b.txt": "other\n"}, "someone else")
        e.begin("T1", s)
        return e, s, r

    def conflict_env(self):
        e = Env(self)
        s = e.commit(e.T0, {"README.md": "task version\n"}, "task T1")
        r = e.push_other({"README.md": "other version\n"}, "someone else")
        e.begin("T1", s)
        return e, s, r

    def assert_finished_cleanly(self, e, tid, s):
        w = e.world
        rec = e.journal().load(tid)
        self.assertEqual(rec["status"], "finished")
        self.assertTrue(rec["pushed"])
        self.assertTrue(rec["drift_marked"])
        self.assertTrue(rec["cleaned"])
        self.assertEqual(rec["task_sha"], s)
        self.assertEqual(rec["final_sha"], e.layer_head())
        self.assertEqual(e.origin_head(), e.layer_head())
        self.assertTrue(e.is_ancestor(s, rec["final_sha"]))
        self.assertEqual(w.count("ledger_pass"), 1)
        self.assertEqual(w.completed.get(f"C-{tid}"), 1)
        self.assertEqual(w.done_sets(), 1)
        self.assertEqual(w.count("mark_drift"), 1)
        self.assertIn(("remove_task", tid), w.effects)
        self.assertEqual(e.candidate_refs(), [])
        for snap in [w.snapshots["ledger_pass"]] + w.snapshots["set_task_done"]:
            self.assertTrue(snap["pushed"])
            self.assertTrue(snap["drift_marked"])
            self.assertTrue(snap["cleaned"])
        self.assertEqual(w.tasks[tid]["status"], "done")
        self.assertEqual(w.tasks[tid]["done_commit"], s)
        self.assertEqual(w.tasks[tid]["final_sha"], rec["final_sha"])
        self.assertEqual(e.remote_merges_all_approved(), [])
        return rec

    # ---- fast-forward ----------------------------------------------------
    def test_fast_forward_with_push(self):
        e, s = self.ff_env()
        self.assertEqual(e.finalizer().run("T1"), "finished")
        rec = self.assert_finished_cleanly(e, "T1", s)
        self.assertEqual(e.origin_head(), s)
        self.assertEqual(e.layer_head(), s)
        self.assertEqual(rec["final_sha"], s)
        self.assertEqual(rec["candidates"], [])
        order = [n for n, _ in e.world.effects]
        self.assertEqual(order, ["mark_drift", "remove_task", "ledger_pass", "set_task"])
        self.assertEqual(e.world.effects[-1][1], ("T1", {"status": "done", "done_commit": s, "final_sha": s,
                                                          "merge_candidates": []}))
        self.assertEqual(e.world.points, FF_POINTS)
        # running a finished record again does nothing
        before = list(e.world.calls)
        self.assertEqual(e.finalizer().run("T1"), "finished")
        self.assertEqual(e.world.calls, before)

    def test_every_crash_point_on_fast_forward_path_recovers(self):
        for point in FF_POINTS:
            with self.subTest(point=point):
                e, s = self.ff_env()
                e.world.crash_at = point
                with self.assertRaises(Boom):
                    e.finalizer().run("T1")
                self.assertEqual(e.remote_merges_all_approved(), [])
                if e.world.done_sets():  # done may only ever be set after the durable prerequisites
                    self.assertTrue(e.world.snapshots["set_task_done"][0]["cleaned"])
                self.assertEqual(e.finalizer().run("T1"), "finished")
                self.assert_finished_cleanly(e, "T1", s)
                self.assertEqual(e.origin_head(), s)

    def test_push_off_never_touches_the_remote(self):
        e, s, r = self.div_env()
        seen = []
        real = finalize._run

        def spy(cwd, *args):
            seen.append(args)
            return real(cwd, *args)

        with patch.object(finalize, "_run", spy):
            self.assertEqual(e.finalizer(push=False).run("T1"), "finished")
        verbs = {a[0] for a in seen}
        self.assertFalse(verbs & {"ls-remote", "fetch", "push"}, verbs)
        rec = e.journal().load("T1")
        self.assertIs(rec["pushed"], False)
        self.assertEqual(rec["final_sha"], s)
        self.assertEqual(e.layer_head(), s)
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(e.world.done_sets(), 1)

    def test_local_candidate_when_layer_moved_sideways(self):
        e = Env(self)
        l = e.commit(e.T0, {"l.txt": "layer\n"}, "layer moved")
        git(e.layer_wt, "reset", "-q", "--hard", l)
        git(e.layer_wt, "push", "-q", "origin", LAYER)
        s = e.commit(e.T0, {"a.txt": "task\n"}, "task")
        e.begin("T1", s)
        self.assertEqual(e.finalizer().run("T1"), "finished")
        rec = self.assert_finished_cleanly(e, "T1", s)
        cand = rec["candidates"][0]
        self.assertEqual(cand["kind"], "local")
        self.assertEqual(e.parents(cand["sha"]), [l, s])
        self.assertEqual(rec["final_sha"], cand["sha"])

    # ---- divergence --------------------------------------------------------
    def test_divergence_candidate_judged_and_reviewed_before_layer_moves(self):
        e, s, r = self.div_env()
        self.assertEqual(e.finalizer().run("T1"), "finished")
        rec = self.assert_finished_cleanly(e, "T1", s)
        m = rec["final_sha"]
        self.assertEqual(e.parents(m), [s, r])
        self.assertEqual(e.origin_head(), m)
        self.assertEqual(e.layer_head(), m)
        self.assertEqual(e.world.judge_seen, [(m, s, False)])
        self.assertEqual(e.world.review_seen, [(m, s, False)])
        self.assertEqual(e.trees.throwaway_shas, [s])  # the merge was built in one throwaway worktree at S
        cand = rec["candidates"][0]
        self.assertEqual(cand["sha"], m)
        self.assertEqual(cand["kind"], "divergence")
        self.assertEqual(cand["state"], "approved")
        self.assertEqual(cand["run_id"], f"ci-{m[:12]}")
        self.assertEqual(cand["verdict"], "pass")
        self.assertEqual(cand["reasons"], ["merge looks right"])
        self.assertEqual(cand["parents"], [s, r])
        reg = ApprovedMerges(e.state).get(m)
        for k, v in {"tid": "T1", "kind": "divergence", "base": s, "other": r, "run_id": f"ci-{m[:12]}",
                     "verdict": "pass", "reasons": ["merge looks right"]}.items():
            self.assertEqual(reg[k], v, k)
        self.assertEqual(e.world.tasks["T1"]["merge_candidates"], [m])
        self.assertEqual(e.world.points, DIV_POINTS)
        self.assertEqual(e.world.count("ask"), 0)

    def test_every_crash_point_on_divergence_path_recovers(self):
        for point in DIV_POINTS:
            with self.subTest(point=point):
                e, s, r = self.div_env()
                e.world.crash_at = point
                with self.assertRaises(Boom):
                    e.finalizer().run("T1")
                self.assertEqual(e.remote_merges_all_approved(), [])
                layer = e.layer_head()
                if layer not in (e.T0, s):  # the layer only ever points at an approved merge
                    self.assertTrue(ApprovedMerges(e.state).has(layer))
                self.assertEqual(e.finalizer().run("T1"), "finished")
                rec = self.assert_finished_cleanly(e, "T1", s)
                self.assertEqual(e.parents(rec["final_sha"]), [s, r])
                self.assertEqual([c["state"] for c in rec["candidates"]], ["approved"])
                self.assertEqual(e.world.count("ask"), 0)

    def test_judge_failure_blocks_without_pushing(self):
        e, s, r = self.div_env()
        e.world.judge_fn = lambda sha: {"passed": False, "run_id": "ci-x", "output": "3 tests failed"}
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(rec["candidates"][0]["state"], "rejected")
        self.assertEqual(e.world.count("review"), 0)
        self.assertEqual(e.world.count("ask"), 1)
        self.assertIn("3 tests failed", e.world.effects[-1][1][2])
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(e.layer_head(), s)
        self.assertFalse(ApprovedMerges(e.state).has(rec["candidates"][0]["sha"]))
        self.assertEqual(e.world.count("ledger_pass") + e.world.done_sets(), 0)

    def test_review_failure_blocks_without_pushing(self):
        e, s, r = self.div_env()
        e.world.review_fn = lambda rec, cand: {"verdict": "fail", "reasons": ["drops the other change"]}
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["candidates"][0]["state"], "rejected")
        self.assertIn("drops the other change", e.world.effects[-1][1][2])
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(e.layer_head(), s)
        self.assertFalse(ApprovedMerges(e.state).has(rec["candidates"][0]["sha"]))

    def _crash_once_rejection_is_durable(self, e, strip_blocking):
        """Review finding 4: crash right after the first journal save that records a rejected candidate."""
        real_save = Journal.save
        state = {"done": False}

        def save(journal, rec):
            real_save(journal, rec)
            if not state["done"] and any(c.get("state") == "rejected" for c in rec.get("candidates") or []):
                state["done"] = True
                raise Boom("after rejected save")

        with patch.object(Journal, "save", save):
            with self.assertRaises(Boom):
                e.finalizer().run("T1")
        self.assertEqual(e.world.count("ask"), 0)
        if strip_blocking:  # a record left by the old code: rejected, active, no blocking intent
            path = e.state / "merges" / "T1.json"
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec.pop("blocking", None)
            path.write_bytes(json.dumps(rec, indent=2, sort_keys=True).encode("utf-8"))
        stuck = e.journal().load("T1")
        self.assertEqual((stuck["status"], stuck["phase"]), ("active", "candidate"))
        self.assertEqual(stuck["candidates"][-1]["state"], "rejected")

    def _assert_rejection_recovered(self, e, r, s, reason, text):
        calls = (e.world.count("judge"), e.world.count("review"))
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(rec["blocked"]["reason"], reason)
        self.assertEqual(e.world.count("ask"), 1)
        self.assertIn(text, e.world.effects[-1][1][2])
        self.assertEqual((e.world.count("judge"), e.world.count("review")), calls)  # not judged again
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(e.layer_head(), s)
        self.assertFalse(ApprovedMerges(e.state).has(rec["candidates"][0]["sha"]))
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        self.assertEqual(e.world.count("ask"), 1)
        # and it is answerable: unblock, fix the checks, finish
        e.world.judge_fn = lambda sha: {"passed": True, "run_id": f"ci-{sha[:12]}", "output": "ok"}
        e.world.review_fn = lambda rec, cand: {"verdict": "pass", "reasons": ["ok now"]}
        e.world.questions[rec["blocked"]["qid"]]["open"] = False
        self.assertEqual(e.journal().unblock(rec["blocked"]["qid"], "fixed"), ["T1"])
        self.assertEqual(e.finalizer().run("T1"), "finished")
        self.assert_finished_cleanly(e, "T1", s)

    def test_crash_after_judge_rejection_saved_recovers_into_blocking(self):
        for strip in (False, True):
            with self.subTest(legacy_record=strip):
                e, s, r = self.div_env()
                e.world.judge_fn = lambda sha: {"passed": False, "run_id": "ci-x", "output": "3 tests failed"}
                self._crash_once_rejection_is_durable(e, strip)
                self._assert_rejection_recovered(e, r, s, "judge failed", "3 tests failed")

    def test_crash_after_review_rejection_saved_recovers_into_blocking(self):
        for strip in (False, True):
            with self.subTest(legacy_record=strip):
                e, s, r = self.div_env()
                e.world.review_fn = lambda rec, cand: {"verdict": "fail", "reasons": ["drops the other change"]}
                self._crash_once_rejection_is_durable(e, strip)
                self._assert_rejection_recovered(e, r, s, "review failed", "drops the other change")

    def test_crash_after_conflict_rejection_saved_recovers_into_blocking(self):
        for strip in (False, True):
            with self.subTest(legacy_record=strip):
                e, s, r = self.conflict_env()
                self._crash_once_rejection_is_durable(e, strip)
                self.assertEqual(e.finalizer().run("T1"), "blocked")
                rec = e.journal().load("T1")
                self.assertEqual(rec["blocked"]["reason"], "merge_conflict")
                self.assertEqual(e.world.count("ask"), 1)
                self.assertIn("README.md", e.world.effects[-1][1][2])
                self.assertEqual(e.origin_head(), r)

    def test_review_exception_propagates_and_resume_calls_only_review(self):
        e, s, r = self.div_env()

        def down(rec, cand):
            raise RuntimeError("reviewer unavailable")

        e.world.review_fn = down
        with self.assertRaises(RuntimeError):
            e.finalizer().run("T1")
        rec = e.journal().load("T1")
        self.assertEqual(rec["status"], "active")
        self.assertEqual(rec["candidates"][0]["state"], "judged")
        self.assertNotIn("verdict", rec["candidates"][0])
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(e.world.count("judge"), 1)
        e.world.review_fn = lambda rec, cand: {"verdict": "pass", "reasons": ["fine"]}
        self.assertEqual(e.finalizer().run("T1"), "finished")
        self.assertEqual(e.world.count("judge"), 1)
        self.assertEqual(e.world.count("review"), 2)
        self.assert_finished_cleanly(e, "T1", s)

    def test_safe_push_refusal_blocks(self):
        e = Env(self)
        x = e.commit(e.T0, {"x.txt": "x\n"}, "side")
        u = e.commit([e.T0, x], {"x.txt": "x\n"}, "unreviewed merge")
        s = e.commit(u, {"a.txt": "task\n"}, "task")
        e.begin("T1", s)
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["status"], "blocked")
        self.assertIn(u, e.world.effects[-1][1][2])
        self.assertEqual(e.origin_head(), e.T0)
        self.assertEqual(e.remote_merges_all_approved(), [])
        self.assertEqual(e.world.count("ledger_pass"), 0)

    def test_max_rounds_exceeded_blocks(self):
        e, s, r = self.div_env()
        self.assertEqual(e.finalizer(max_rounds=0).run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["blocked"]["reason"], "too many merge rounds")
        self.assertEqual(rec["candidates"], [])
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(e.world.count("judge"), 0)

    def test_push_rejections_count_toward_max_rounds(self):
        e, s = self.ff_env()
        pushed = []

        def race(point):  # someone else pushes just before every push of ours
            if point == "before:push":
                pushed.append(e.push_other({f"r{len(pushed)}.txt": "x\n"}, "race"))

        e.world.on_point = race
        self.assertEqual(e.finalizer(max_rounds=2).run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["blocked"]["reason"], "too many merge rounds")
        self.assertEqual(len(rec["candidates"]), 1)  # rejected push (1), candidate (2), rejected push (3) > 2
        self.assertEqual(e.origin_head(), pushed[-1])
        self.assertEqual(e.remote_merges_all_approved(), [])

    def test_layer_moved_unexpectedly_during_fast_forward_blocks(self):
        e, s = self.ff_env()
        e.world.crash_at = "before:ff"
        with self.assertRaises(Boom):
            e.finalizer().run("T1")
        z = e.commit(e.T0, {"z.txt": "z\n"}, "unexpected")
        git(e.layer_wt, "reset", "-q", "--hard", z)
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["blocked"]["reason"], "layer moved unexpectedly")
        self.assertEqual(e.layer_head(), z)
        self.assertEqual(e.origin_head(), e.T0)

    # ---- conflicts, blocking and unblocking ---------------------------------
    def test_conflict_blocks_once_reuses_question_and_finishes_after_unblock(self):
        e, s_a, r = self.conflict_env()
        w = e.world
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(rec["blocked"]["reason"], "merge_conflict")
        qid = rec["blocked"]["qid"]
        self.assertEqual(w.count("ask"), 1)
        kind, subject, body, tid = w.effects[-1][1]
        self.assertEqual((kind, tid), ("merge", "T1"))
        self.assertIn("T1", subject)
        self.assertIn("README.md", body)
        self.assertEqual(rec["candidates"][0]["state"], "rejected")
        self.assertEqual(rec["candidates"][0]["conflicts"], ["README.md"])
        self.assertEqual(e.layer_head(), s_a)
        self.assertEqual(e.origin_head(), r)
        self.assertEqual(w.count("judge") + w.count("review"), 0)

        # blocked: nothing happens, however often and on whatever objects we call run
        path = e.state / "merges" / "T1.json"
        raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
        listing = sorted(os.listdir(e.state / "merges"))
        calls = list(w.calls)
        seen = []
        with patch.object(finalize, "_run", lambda cwd, *a: seen.append(a)):
            fin = e.finalizer()
            for _ in range(3):
                self.assertEqual(fin.run("T1"), "blocked")
            for _ in range(2):
                self.assertEqual(e.finalizer().run("T1"), "blocked")
        self.assertEqual(seen, [])
        self.assertEqual(w.calls, calls)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(sorted(os.listdir(e.state / "merges")), listing)

        # a second task on top hits the same conflict and reuses the question
        s_b = e.commit(s_a, {"c.txt": "second\n"}, "task T2")
        e.begin("T2", s_b)
        self.assertEqual(e.finalizer().run("T2"), "blocked")
        rec2 = e.journal().load("T2")
        self.assertEqual(rec2["blocked"]["qid"], qid)
        self.assertEqual(w.count("ask"), 1)
        self.assertIn(("question_open", qid), w.calls)

        # Ben resolves it on the remote and answers
        r2 = e.push_other({"README.md": "initial\n"}, "undo the conflicting change")
        w.questions[qid]["open"] = False
        self.assertEqual(sorted(e.journal().unblock(qid, "reverted README on origin")), ["T1", "T2"])
        for tid in ("T1", "T2"):
            rr = e.journal().load(tid)
            self.assertEqual(rr["notes"], ["Ben: reverted README on origin"])
            self.assertEqual([c["state"] for c in rr["candidates"]], ["retired"])
        self.assertEqual(e.finalizer().run("T1"), "finished")
        self.assertEqual(e.finalizer().run("T2"), "finished")
        final = e.origin_head()
        self.assertEqual(final, e.layer_head())
        for sha in (s_a, s_b, r2):
            self.assertTrue(e.is_ancestor(sha, final), sha)
        self.assertEqual(e.journal().load("T1")["final_sha"], final)
        self.assertEqual(e.journal().load("T2")["final_sha"], final)
        self.assertEqual(w.count("ask"), 1)
        self.assertEqual(w.count("ledger_pass"), 2)
        self.assertEqual(w.done_sets(), 2)
        self.assertEqual(e.remote_merges_all_approved(), [])
        self.assertEqual(e.candidate_refs(), [])
        self.assertEqual(e.journal().load("T1")["candidates"][-1]["state"], "approved")

    def test_conflict_scenario_under_windows_git_settings(self):
        # Review finding 6: on Windows the scenario above ended 'blocked' after unblock. Emulate that PC:
        # a global core.autocrlf=true, and text-mode subprocess pipes that turn "\n" into "\r\n" on the
        # way to the child (what Windows does with text=True input). The harness must still create
        # byte-exact blobs, so the "undo" commit really restores README.md and the retry merges cleanly.
        cfg = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cfg, True)
        (cfg / "gitconfig").write_bytes(b"[core]\n\tautocrlf = true\n")
        real_run = subprocess.run

        def windows_pipes(*args, **kw):
            if kw.get("text") and isinstance(kw.get("input"), str):
                kw["input"] = kw["input"].replace("\n", "\r\n")
            return real_run(*args, **kw)

        glob = {"GIT_CONFIG_GLOBAL": str(cfg / "gitconfig")}
        with patch.dict(os.environ, glob), patch.dict(ENV, glob), patch.object(subprocess, "run", windows_pipes):
            self.assertEqual(git(Path(cfg), "config", "--global", "core.autocrlf"), "true")
            self.test_conflict_blocks_once_reuses_question_and_finishes_after_unblock()

    def test_closed_question_is_not_reused(self):
        e, s_a, r = self.conflict_env()
        e.finalizer().run("T1")
        qid = e.journal().load("T1")["blocked"]["qid"]
        e.world.questions[qid]["open"] = False
        s_b = e.commit(s_a, {"c.txt": "second\n"}, "task T2")
        e.begin("T2", s_b)
        self.assertEqual(e.finalizer().run("T2"), "blocked")
        self.assertNotEqual(e.journal().load("T2")["blocked"]["qid"], qid)
        self.assertEqual(e.world.count("ask"), 2)

    def test_crash_after_ask_does_not_ask_twice(self):
        e, s_a, r = self.conflict_env()
        e.world.crash_at = "after:ask"
        with self.assertRaises(Boom):
            e.finalizer().run("T1")
        self.assertEqual(e.world.count("ask"), 1)
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(e.world.count("ask"), 1)
        self.assertEqual(rec["blocked"]["qid"], "q1")
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(e.origin_head(), r)

    def test_unblock_recomputes_tips_after_judge_failure(self):
        e, s, r = self.div_env()
        e.world.judge_fn = lambda sha: {"passed": False, "run_id": "ci-bad", "output": "broken"}
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        qid = e.journal().load("T1")["blocked"]["qid"]
        r2 = e.push_other({"fix.txt": "fixed\n"}, "fix on origin")
        e.world.judge_fn = lambda sha: {"passed": True, "run_id": f"ci-{sha[:12]}", "output": "ok"}
        e.journal().unblock(qid, "pushed a fix")
        self.assertEqual(e.finalizer().run("T1"), "finished")
        rec = self.assert_finished_cleanly(e, "T1", s)
        self.assertEqual([c["state"] for c in rec["candidates"]], ["retired", "approved"])
        self.assertEqual(e.parents(rec["final_sha"]), [s, r2])
        self.assertEqual(e.world.tasks["T1"]["merge_candidates"], [rec["final_sha"]])

    # ---- queue and ledger idempotence ----------------------------------------
    def test_ledger_refusal_blocks(self):
        e, s = self.ff_env()

        def refuse(rec):
            e.world.effect("ledger_pass", rec["tid"])
            raise RuntimeError("evidence incomplete")

        e.world.ledger_pass = refuse
        self.assertEqual(e.finalizer().run("T1"), "blocked")
        rec = e.journal().load("T1")
        self.assertEqual(rec["blocked"]["reason"], "ledger refused completion")
        self.assertEqual(e.world.done_sets(), 0)
        e.world.questions[rec["blocked"]["qid"]]["open"] = False
        e.journal().unblock(rec["blocked"]["qid"], "fixed")
        self.assertEqual(e.journal().load("T1")["phase"], "ledger")

    def test_queue_done_not_repeated_after_crash_after_queue(self):
        e, s = self.ff_env()
        e.world.crash_at = "after:queue"
        with self.assertRaises(Boom):
            e.finalizer().run("T1")
        self.assertEqual(e.world.done_sets(), 1)
        self.assertEqual(e.journal().load("T1")["status"], "active")
        self.assertEqual(e.finalizer().run("T1"), "finished")
        self.assertEqual(e.world.done_sets(), 1)


    def test_get_task_is_a_required_hook(self):
        # Review finding 5: exactly-once queue completion must not depend on an optional hook.
        e = Env(self)
        w = e.world
        kw = dict(judge=w.judge, review=w.review, ask=w.ask, question_open=w.question_open,
                  mark_drift=w.mark_drift, drift_marked=w.drift_marked, ledger_pass=w.ledger_pass,
                  ledger_completed=w.ledger_completed, set_task=w.set_task)
        with self.assertRaises(TypeError):
            Hooks(**kw)
        with self.assertRaises(TypeError):
            Hooks(**kw, get_task=None)
        self.assertIsNotNone(Hooks(**kw, get_task=w.get_task).get_task)

    def test_queue_done_not_repeated_after_crash_after_queue_on_fresh_objects(self):
        e, s = self.ff_env()
        e.world.crash_at = "after:queue"
        with self.assertRaises(Boom):
            e.finalizer().run("T1")
        # a brand-new conductor view of the same durable queue
        fresh = Finalizer(e.layer_wt, LAYER, e.trees, Journal(e.state), ApprovedMerges(e.state),
                          e.world.hooks(), push=True)
        self.assertEqual(fresh.run("T1"), "finished")
        self.assertEqual(e.world.done_sets(), 1)
        self.assertIn(("get_task", "T1"), e.world.calls)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.s = self.env.commit(self.env.T0, {"a.txt": "a\n"}, "task")

    def fin(self):
        return self.env.finalizer()

    def rec(self, tid, **changes):
        self.env.begin(tid, self.s)
        r = self.env.journal().load(tid)
        r.update(changes)
        self.env.journal().save(r)

    def sets(self):
        return [a for n, a in self.env.world.effects if n == "set_task"]

    def test_active_or_blocked_record_with_tests_ok_task_becomes_merge_pending(self):
        self.rec("T1")
        self.rec("T2", status="blocked", blocked={"reason": "merge_conflict", "qid": "q1"})
        self.fin().reconcile([{"id": "T1", "status": "tests_ok"}, {"id": "T2", "status": "tests_ok"}])
        self.assertEqual(self.sets(), [("T1", {"status": "merge_pending"}), ("T2", {"status": "merge_pending"})])

    def test_merge_pending_without_record_goes_back_to_tests_ok(self):
        self.fin().reconcile([{"id": "T9", "status": "merge_pending"}, {"id": "T8", "status": "todo"}])
        self.assertEqual(self.sets(), [("T9", {"status": "tests_ok"})])

    def test_finished_record_and_ledger_completed_sets_done(self):
        self.rec("T1", status="finished", phase="finished", final_sha=self.s, pushed=True, drift_marked=True,
                 cleaned=True)
        self.env.world.completed["C-T1"] = 1
        self.fin().reconcile([{"id": "T1", "status": "merge_pending"}])
        self.assertEqual(self.sets(), [("T1", {"status": "done", "done_commit": self.s, "final_sha": self.s,
                                               "merge_candidates": []})])

    def test_finished_record_without_ledger_is_reopened_at_ledger(self):
        self.rec("T1", status="finished", phase="finished", final_sha=self.s, pushed=True, drift_marked=True,
                 cleaned=True)
        self.fin().reconcile([{"id": "T1", "status": "merge_pending"}])
        r = self.env.journal().load("T1")
        self.assertEqual((r["status"], r["phase"]), ("active", "ledger"))
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.fin().run("T1"), "finished")
        self.assertEqual(self.env.world.count("ledger_pass"), 1)

    def test_done_task_with_unfinished_record_finishes_or_goes_to_ledger(self):
        self.rec("T1", phase="queue", final_sha=self.s, pushed=True, drift_marked=True, cleaned=True)
        self.rec("T2", phase="drift", final_sha=self.s, pushed=True)
        self.env.world.completed["C-T1"] = 1
        self.fin().reconcile([{"id": "T1", "status": "done"}, {"id": "T2", "status": "done"}])
        r1, r2 = self.env.journal().load("T1"), self.env.journal().load("T2")
        self.assertEqual(r1["status"], "finished")
        self.assertEqual((r2["status"], r2["phase"]), ("active", "ledger"))
        self.assertEqual(self.sets(), [])

    def test_consistent_state_changes_nothing(self):
        self.rec("T1", status="finished", phase="finished", final_sha=self.s)
        self.env.world.completed["C-T1"] = 1
        self.rec("T2")
        raw = {t: (self.env.state / "merges" / f"{t}.json").read_bytes() for t in ("T1", "T2")}
        self.fin().reconcile([{"id": "T1", "status": "done"}, {"id": "T2", "status": "merge_pending"},
                              {"id": "T3", "status": "todo"}])
        self.assertEqual(self.sets(), [])
        for t, b in raw.items():
            self.assertEqual((self.env.state / "merges" / f"{t}.json").read_bytes(), b)


if __name__ == "__main__":
    unittest.main()
