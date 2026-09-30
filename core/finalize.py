"""Crash-safe finalization of a reviewed task commit (T1B3d).

Plain code, no AI and no imports from core.bootstrap. A reviewed task commit S is taken into the layer
branch and finalized in phases (merge, sync, candidate, push, drift, cleanup, ledger, queue). Every phase
first reconciles against Git; every Git or durable operation is preceded by a saved intent where needed
and followed by a journal save, so a crash at any boundary is recoverable by running a fresh Finalizer.

Assumptions about collaborators (injected, so this module does not depend on their code):
- `trees` is a core.worktrees.Worktrees (T1B3b): `throwaway(sha)` context manager yielding a worktree
  path at exactly `sha`, and an idempotent `remove_task(tid)`.
- The ledger (T1B3c) is reached only through the `Hooks.ledger_pass` / `Hooks.ledger_completed` callables.
- `Hooks.get_task(tid)` (optional) reads the durable queue entry, so the queue completion is not repeated
  after a crash between `set_task` and the journal save. `Hooks.find_question(kind, subject, tid)`
  (optional) finds a still-open question asked earlier for this record, so a crash between `ask` and the
  blocked-status save never asks twice.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
NOTE_CAP = 2000
NOTES_KEEP = 30
OUTPUT_CAP = 4000
IDENT = ["-c", "user.name=Forge", "-c", "user.email=forge@localhost"]


# --------------------------------------------------------------------------- git and files
def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """The only place this module runs git."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, **NOWIN)


def _git(cwd: Path, *args: str) -> str:
    p = _run(cwd, *args)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(p.stderr or '').strip()[:500]}")
    return (p.stdout or "").strip()


def _is_ancestor(cwd: Path, a: str, b: str) -> bool:
    """True when a is an ancestor of b or equal to it."""
    p = _run(cwd, "merge-base", "--is-ancestor", a, b)
    if p.returncode in (0, 1):
        return p.returncode == 0
    raise RuntimeError(f"git merge-base --is-ancestor {a} {b}: {(p.stderr or '').strip()[:500]}")


def _ref_sha(cwd: Path, ref: str) -> Optional[str]:
    p = _run(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}")
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name != "nt":  # make the rename itself durable where the platform allows it
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


# --------------------------------------------------------------------------- journal
class Journal:
    """One durable record per task at state/merges/<tid>.json."""

    def __init__(self, state: Path):
        self.dir = Path(state) / "merges"

    def _path(self, tid: str) -> Path:
        return self.dir / f"{tid}.json"

    def begin(self, tid: str, cid: str, task_sha: str, base: str, ci_run_id: str, review: dict,
              evidence: dict) -> dict:
        existing = self.load(tid)
        if existing is not None:
            if existing.get("task_sha") != task_sha:
                raise ValueError(f"merge journal for {tid} is for {existing.get('task_sha')}, not {task_sha}")
            return existing
        rec = {"tid": tid, "cid": cid, "task_sha": task_sha, "base": base, "ci_run_id": ci_run_id,
               "review": review, "evidence": evidence, "pass_pid": f"{cid}-pass-{task_sha[:12]}",
               "phase": "merge", "status": "active", "blocked": None, "intent": None, "candidates": [],
               "final_sha": None, "pushed": None, "drift_marked": False, "cleaned": False, "notes": [],
               "answers": 0, "rounds": 0}
        self.save(rec)
        return rec

    def load(self, tid: str) -> Optional[dict]:
        return _read_json(self._path(tid), None)

    def save(self, rec: dict) -> None:
        rec["notes"] = [str(n)[:NOTE_CAP] for n in rec.get("notes") or []][-NOTES_KEEP:]
        _atomic_write(self._path(rec["tid"]), rec)

    def all(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        recs = [_read_json(p, None) for p in self.dir.glob("*.json")]
        return sorted((r for r in recs if r), key=lambda r: r["tid"])

    def active(self) -> list[dict]:
        return [r for r in self.all() if r.get("status") == "active"]

    def blocked_on(self, qid: str) -> list[dict]:
        return [r for r in self.all()
                if r.get("status") == "blocked" and (r.get("blocked") or {}).get("qid") == qid]

    def unblock(self, qid: str, answer: str) -> list[str]:
        tids = []
        for rec in self.blocked_on(qid):
            rec["status"] = "active"
            rec["blocked"] = None
            rec["rounds"] = 0
            rec["answers"] = int(rec.get("answers") or 0) + 1
            rec.setdefault("notes", []).append(f"Ben: {answer}")
            # retry from fresh local and remote tips; rejected candidate state is history only
            rec["phase"] = "ledger" if rec.get("phase") == "ledger" else "merge"
            rec["intent"] = None
            for c in rec.get("candidates") or []:
                if c.get("state") != "approved":
                    c["was"] = c.get("state")
                    c["state"] = "retired"
            self.save(rec)
            tids.append(rec["tid"])
        return tids


# --------------------------------------------------------------------------- approved-merge registry
class ApprovedMerges:
    """state/approved_merges.json: every merge commit that was judged and reviewed, keyed by SHA."""

    def __init__(self, state: Path):
        self.path = Path(state) / "approved_merges.json"

    def _all(self) -> dict:
        return _read_json(self.path, {})

    def add(self, sha: str, record: dict) -> None:
        data = self._all()
        data[sha] = dict(record)
        _atomic_write(self.path, data)

    def has(self, sha: str) -> bool:
        return sha in self._all()

    def get(self, sha: str) -> Optional[dict]:
        return self._all().get(sha)


# --------------------------------------------------------------------------- safe push
def unapproved_merges(wt: Path, layer: str, approved) -> list[str]:
    """Merge commits on `layer` that origin doesn't have yet and that the registry doesn't know."""
    wt = Path(wt)
    tracking = f"refs/remotes/origin/{layer}"
    if _ref_sha(wt, tracking):
        exclude = [tracking]
    else:
        exclude = _git(wt, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").split()
    out = _git(wt, "rev-list", "--merges", f"refs/heads/{layer}", *[f"^{r}" for r in exclude])
    return [sha for sha in out.split() if not approved.has(sha)]


def safe_push(wt: Path, layer: str, approved) -> tuple[str, str]:
    bad = unapproved_merges(wt, layer, approved)
    if bad:
        return "refused", " ".join(bad)
    p = _run(Path(wt), "push", "origin", f"refs/heads/{layer}:refs/heads/{layer}")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode == 0:
        return "ok", out
    low = out.lower()
    if "[rejected]" in low or "non-fast-forward" in low or "fetch first" in low:
        return "rejected", out
    return "error", out


# --------------------------------------------------------------------------- finalizer
def _noop(point: str) -> None:
    return None


@dataclass
class Hooks:
    judge: Callable[[str], dict]
    review: Callable[[dict, dict], dict]
    ask: Callable[[str, str, str, str], str]
    question_open: Callable[[str], bool]
    mark_drift: Callable[[str], None]
    drift_marked: Callable[[str], bool]
    ledger_pass: Callable[[dict], None]
    ledger_completed: Callable[[str], bool]
    set_task: Callable[[str, dict], None]
    crash: Callable[[str], None] = _noop
    get_task: Optional[Callable[[str], Optional[dict]]] = None
    find_question: Optional[Callable[[str, str, str], Optional[str]]] = None


class _Stop(Exception):
    """Internal: the run ended in `blocked`."""


class Finalizer:
    def __init__(self, layer_wt: Path, layer: str, trees, journal: Journal, approved: ApprovedMerges,
                 hooks: Hooks, *, push: bool, max_rounds: int = 3):
        self.wt = Path(layer_wt)
        self.layer = layer
        self.trees = trees
        self.journal = journal
        self.approved = approved
        self.hooks = hooks
        self.push = push
        self.max_rounds = max_rounds

    # ---------------------------------------------------------------- helpers
    def _crash(self, point: str) -> None:
        self.hooks.crash(point)

    def _save(self, rec: dict) -> None:
        self.journal.save(rec)

    def _tip(self) -> str:
        return _git(self.wt, "rev-parse", "HEAD")

    def _reset(self) -> None:
        _git(self.wt, "reset", "-q", "--hard")
        _git(self.wt, "clean", "-q", "-fd")

    def _remote(self) -> Optional[str]:
        p = _run(self.wt, "ls-remote", "origin", f"refs/heads/{self.layer}")
        if p.returncode != 0:
            raise RuntimeError(f"git ls-remote origin failed: {(p.stderr or '').strip()[:500]}")
        line = (p.stdout or "").strip().split("\n")[0].strip()
        return line.split()[0] if line else None

    def _cand_ref(self, tid: str, n: int) -> str:
        return f"refs/forge/candidates/{tid}-{n}"

    def _done_payload(self, rec: dict) -> dict:
        return {"status": "done", "done_commit": rec["task_sha"], "final_sha": rec["final_sha"],
                "merge_candidates": [c["sha"] for c in rec.get("candidates") or [] if c.get("state") == "approved"]}

    # ---------------------------------------------------------------- blocking
    def _block(self, rec: dict, reason: str, detail: str, other: Optional[str] = None) -> None:
        tid = rec["tid"]
        subject = f"Forge merge blocked: task {tid}: {reason}"
        body = (f"Task {tid} (commit {rec['task_sha']}) could not be finalized into {self.layer}.\n"
                f"Reason: {reason}\n\n{detail}\n\n"
                "Nothing was pushed for this task. Other work continues meanwhile; this task waits for your answer.")
        rec["blocking"] = {"reason": reason, "other": other, "subject": subject, "body": body}
        self._save(rec)  # the question/record association survives a crash right after `ask`
        self._finish_block(rec, resumed=False)

    def _finish_block(self, rec: dict, resumed: bool) -> None:
        b = rec["blocking"]
        qid = None
        if b.get("other"):
            for other in self.journal.all():
                ob = other.get("blocked") or {}
                if (other["tid"] != rec["tid"] and other.get("status") == "blocked"
                        and ob.get("reason") == b["reason"] and ob.get("other") == b["other"]
                        and ob.get("qid") and self.hooks.question_open(ob["qid"])):
                    qid = ob["qid"]
                    break
        if qid is None and resumed and self.hooks.find_question is not None:
            qid = self.hooks.find_question("merge", b["subject"], rec["tid"])
        if qid is None:
            self._crash("before:ask")
            qid = self.hooks.ask("merge", b["subject"], b["body"], rec["tid"])
            self._crash("after:ask")
        rec["status"] = "blocked"
        rec["blocked"] = {"reason": b["reason"], "qid": qid, "other": b.get("other")}
        rec["blocking"] = None
        self._save(rec)
        raise _Stop()

    def _count_round(self, rec: dict) -> None:
        rec["rounds"] = int(rec.get("rounds") or 0) + 1
        if rec["rounds"] > self.max_rounds:
            self._block(rec, "too many merge rounds",
                        f"The layer branch kept moving: {rec['rounds'] - 1} merge rounds used "
                        f"(limit {self.max_rounds}).")

    # ---------------------------------------------------------------- run
    def run(self, tid: str) -> str:
        rec = self.journal.load(tid)
        if rec is None:
            raise KeyError(f"no merge journal for {tid}")
        if rec.get("status") == "blocked":
            return "blocked"
        if rec.get("status") == "finished":
            return "finished"
        try:
            if rec.get("blocking"):
                self._finish_block(rec, resumed=True)
            phases = {"merge": self._merge, "sync": self._sync, "candidate": self._candidate, "push": self._push,
                      "drift": self._drift, "cleanup": self._cleanup, "ledger": self._ledger, "queue": self._queue}
            while rec["status"] == "active":
                phase = rec["phase"]
                if phase not in phases:
                    raise RuntimeError(f"merge journal for {tid} has unknown phase {phase!r}")
                phases[phase](rec)
        except _Stop:
            return "blocked"
        return "finished" if rec["status"] == "finished" else "blocked"

    # 1. merge ------------------------------------------------------------
    def _merge(self, rec: dict) -> None:
        s = rec["task_sha"]
        self._reset()
        tip = self._tip()
        intent = rec.get("intent")
        if intent and intent.get("op") == "ff":
            if not (_is_ancestor(self.wt, s, tip) or tip == intent.get("from")):
                self._block(rec, "layer moved unexpectedly",
                            f"A fast-forward from {intent.get('from')} to {intent.get('to')} was in progress, "
                            f"but the layer branch is now at {tip}.")
        if _is_ancestor(self.wt, s, tip):
            pass
        elif _is_ancestor(self.wt, tip, s):
            rec["intent"] = {"op": "ff", "from": tip, "to": s}
            self._save(rec)
            self._crash("before:ff")
            _git(self.wt, "merge", "-q", "--ff-only", s)
            self._crash("after:ff")
        else:
            rec["intent"] = None
            return self._start_candidate(rec, "local", tip, s)
        rec["intent"] = None
        rec["phase"] = "sync"
        self._save(rec)

    # 2. sync -------------------------------------------------------------
    def _sync(self, rec: dict) -> None:
        tip = self._tip()
        if not self.push:
            rec["final_sha"] = tip
            rec["pushed"] = False
            rec["phase"] = "drift"
            self._save(rec)
            return
        r = self._remote()
        if r:
            p = _run(self.wt, "fetch", "-q", "origin", self.layer)
            if p.returncode != 0:
                raise RuntimeError(f"git fetch origin {self.layer} failed: {(p.stderr or '').strip()[:500]}")
        if r is None or _is_ancestor(self.wt, r, tip):
            rec["phase"] = "push"
            self._save(rec)
            return
        self._start_candidate(rec, "divergence", tip, r)

    # 3. candidate ----------------------------------------------------------
    def _start_candidate(self, rec: dict, kind: str, base: str, other: str) -> None:
        self._count_round(rec)
        n = len(rec["candidates"]) + 1
        rec["candidates"].append({"n": n, "kind": kind, "base": base, "other": other, "sha": None,
                                  "state": "creating"})
        rec["phase"] = "candidate"
        rec["intent"] = None
        self._save(rec)

    def _create(self, rec: dict, cand: dict) -> None:
        tid, base, other = rec["tid"], cand["base"], cand["other"]
        ref = self._cand_ref(tid, cand["n"])
        existing = _ref_sha(self.wt, ref)
        if existing:
            parents = _git(self.wt, "rev-list", "--parents", "-n", "1", existing).split()[1:]
            if parents == [base, other]:
                cand.update(sha=existing, parents=parents, state="created")
                self._save(rec)
                return
        self._crash("before:candidate-merge")
        conflicts = None
        with self.trees.throwaway(base) as tw:
            tw = Path(tw)
            p = _run(tw, *IDENT, "merge", "--no-ff", "--no-edit", "-m",
                     f"Forge: merge {other[:12]} into {self.layer} for {tid}", other)
            if p.returncode != 0:
                files = _run(tw, "diff", "--name-only", "--diff-filter=U").stdout.split()
                _run(tw, "merge", "--abort")
                if not files:
                    raise RuntimeError(f"git merge {other} failed: {((p.stdout or '') + (p.stderr or '')).strip()[:500]}")
                conflicts = sorted(files)
            else:
                _git(tw, "update-ref", ref, "HEAD")
                sha = _git(tw, "rev-parse", "HEAD")
        if conflicts is not None:
            cand.update(state="rejected", conflicts=conflicts)
            self._block(rec, "merge_conflict",
                        f"Merging {other} into {base} conflicts in:\n" + "\n".join(f"- {f}" for f in conflicts),
                        other=other)
        self._crash("after:candidate-merge")
        parents = _git(self.wt, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        cand.update(sha=sha, parents=parents, state="created")
        self._save(rec)

    def _candidate(self, rec: dict) -> None:
        cand = rec["candidates"][-1]
        if cand["state"] == "creating":
            self._create(rec, cand)
        if cand["state"] == "created":
            self._crash("before:judge")
            res = self.hooks.judge(cand["sha"])
            self._crash("after:judge")
            cand.update(run_id=res.get("run_id"), passed=bool(res.get("passed")),
                        judge_output=str(res.get("output") or "")[-OUTPUT_CAP:])
            cand["state"] = "judged" if cand["passed"] else "rejected"
            self._save(rec)
            if not cand["passed"]:
                self._block(rec, "judge failed",
                            f"The merge commit {cand['sha']} failed its checks (run {cand['run_id']}):\n"
                            f"{cand['judge_output']}", other=cand["other"])
        if cand["state"] == "judged":
            self._crash("before:review")
            res = self.hooks.review(rec, cand)
            self._crash("after:review")
            reasons = [str(x) for x in (res or {}).get("reasons") or []]
            cand.update(verdict=(res or {}).get("verdict"), reasons=reasons)
            cand["state"] = "reviewed" if cand["verdict"] == "pass" else "rejected"
            self._save(rec)
            if cand["state"] == "rejected":
                self._block(rec, "review failed",
                            f"The reviewer did not pass merge commit {cand['sha']}:\n"
                            + "\n".join(f"- {r}" for r in reasons or ["no reasons given"]), other=cand["other"])
        if cand["state"] == "reviewed":
            self._crash("before:approve")
            self.approved.add(cand["sha"], {"tid": rec["tid"], "kind": cand["kind"], "base": cand["base"],
                                            "other": cand["other"], "parents": cand.get("parents"),
                                            "run_id": cand.get("run_id"), "verdict": cand.get("verdict"),
                                            "reasons": cand.get("reasons")})
            self._crash("after:approve")
            cand["state"] = "approved"
            self._save(rec)
        if cand["state"] != "approved":
            raise RuntimeError(f"candidate {cand['n']} of {rec['tid']} is in state {cand['state']!r}")
        self._move_layer(rec, cand)

    def _move_layer(self, rec: dict, cand: dict) -> None:
        m, base = cand["sha"], cand["base"]
        self._reset()
        tip = self._tip()
        if tip == m or _is_ancestor(self.wt, m, tip):
            pass
        elif tip == base:
            rec["intent"] = {"op": "ff", "from": base, "to": m}
            self._save(rec)
            self._crash("before:candidate-ff")
            _git(self.wt, "merge", "-q", "--ff-only", m)
            self._crash("after:candidate-ff")
        else:
            self._block(rec, "layer moved unexpectedly",
                        f"The approved merge {m} was built on {base}, but the layer branch is now at {tip}.")
        rec["intent"] = None
        rec["phase"] = "sync"
        self._save(rec)

    # 4. push -------------------------------------------------------------
    def _push(self, rec: dict) -> None:
        tip = self._tip()
        if not _is_ancestor(self.wt, rec["task_sha"], tip):
            self._block(rec, "task commit missing from layer",
                        f"The layer tip {tip} does not contain the task commit {rec['task_sha']}.")
        if self._remote() == tip:
            status = "ok"
        else:
            self._crash("before:push")
            status, out = safe_push(self.wt, self.layer, self.approved)
            self._crash("after:push")
            if status == "refused":
                self._block(rec, "push refused: unapproved merge commits",
                            f"These merge commits are on the layer branch but were never judged and reviewed:\n{out}")
            if status == "rejected":
                rec["phase"] = "sync"
                self._count_round(rec)
                self._save(rec)
                return
            if status != "ok":
                raise RuntimeError(f"git push origin {self.layer} failed: {out[:500]}")
        rec["final_sha"] = tip
        rec["pushed"] = True
        rec["phase"] = "drift"
        self._save(rec)

    # 5. drift ------------------------------------------------------------
    def _drift(self, rec: dict) -> None:
        if not self.hooks.drift_marked(rec["tid"]):
            self.hooks.mark_drift(rec["tid"])
        self._crash("after:drift")
        rec["drift_marked"] = True
        rec["phase"] = "cleanup"
        self._save(rec)

    # 6. cleanup ----------------------------------------------------------
    def _cleanup(self, rec: dict) -> None:
        tid = rec["tid"]
        self.trees.remove_task(tid)
        prefix = f"refs/forge/candidates/{tid}-"
        for ref in _git(self.wt, "for-each-ref", "--format=%(refname)", "refs/forge/candidates/").split():
            if ref.startswith(prefix) and ref[len(prefix):].isdigit():
                _git(self.wt, "update-ref", "-d", ref)
        self._crash("after:cleanup")
        rec["cleaned"] = True
        rec["phase"] = "ledger"
        self._save(rec)

    # 7. ledger -----------------------------------------------------------
    def _ledger(self, rec: dict) -> None:
        if not self.hooks.ledger_completed(rec["cid"]):
            self._crash("before:ledger")
            try:
                self.hooks.ledger_pass(rec)
            except Exception as e:  # a refusal; crashes (BaseException) propagate
                if not self.hooks.ledger_completed(rec["cid"]):
                    self._block(rec, "ledger refused completion", f"The ledger refused the pass: {e}")
            self._crash("after:ledger")
        rec["phase"] = "queue"
        self._save(rec)

    # 8. queue ------------------------------------------------------------
    def _already_done(self, tid: str, payload: dict) -> bool:
        if self.hooks.get_task is None:
            return False
        t = self.hooks.get_task(tid) or {}
        return all(t.get(k) == v for k, v in payload.items())

    def _queue(self, rec: dict) -> None:
        payload = self._done_payload(rec)
        if not self._already_done(rec["tid"], payload):
            self.hooks.set_task(rec["tid"], payload)
        self._crash("after:queue")
        rec["status"] = "finished"
        rec["phase"] = "finished"
        self._save(rec)

    # ---------------------------------------------------------------- reconcile
    def reconcile(self, tasks: list[dict]) -> None:
        by_id = {t.get("id"): t for t in tasks}
        have = set()
        for rec in self.journal.all():
            tid = rec["tid"]
            have.add(tid)
            t = by_id.get(tid)
            if t is None:
                continue
            status = t.get("status")
            if rec["status"] in ("active", "blocked") and status == "tests_ok":
                self.hooks.set_task(tid, {"status": "merge_pending"})
            elif rec["status"] == "finished" and status != "done":
                if self.hooks.ledger_completed(rec["cid"]):
                    self.hooks.set_task(tid, self._done_payload(rec))
                else:
                    rec["status"] = "active"
                    rec["phase"] = "ledger"
                    self._save(rec)
            elif status == "done" and rec["status"] != "finished":
                if self.hooks.ledger_completed(rec["cid"]):
                    rec["status"] = "finished"
                    rec["phase"] = "finished"
                else:
                    rec["phase"] = "ledger"
                self._save(rec)
        for t in tasks:
            if t.get("status") == "merge_pending" and t.get("id") not in have:
                self.hooks.set_task(t["id"], {"status": "tests_ok"})
