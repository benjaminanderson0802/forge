"""Bootstrap conductor: runs a queue of tasks through the D-025 team with no human relay.

Spec: docs/specs/bootstrap-conductor.md. Plain code only; AI is reached solely through
the Team's agents (core.agents interface). Ben is reached only by email.

    python -m core.bootstrap init --layer layer-1 --tasks tasks.json
    python -m core.bootstrap run
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.ledger import Ledger, Rejected
from core.usage import Meter

ROLES = {"ci": "ci", "forge-manager": "manager", "forge-executor": "executor",
         "forge-auditor": "auditor", "forge-core": "core", "benjamin": "human"}
NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
TASK_FIELDS = ("id", "title", "section", "files_in_scope", "test_files", "test_cmd")

S_TESTS = {"required": ["files"]}
S_BUILD = {"required": ["status"]}
S_REVIEW = {"required": ["verdict", "reasons"]}
S_TROUBLE = {"required": ["kind", "notes"]}
S_DRIFT = {"required": ["status"]}
S_PLAN = {"required": ["tasks"]}


@dataclass
class Team:
    test_writer: object
    builder: object
    reviewer: object
    troubleshooter: object
    drift_keeper: object
    planner: object


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, **NOWIN)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


class Tampered(Exception):
    """An agent run changed the conductor's own state files (R9)."""


ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _norm(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def parse_test_cmd(cmd: str, test_files: list[str]) -> list[str] | None:
    """R1: accept only `python -m unittest <paths>` where every path is one of test_files."""
    m = re.match(r'^\s*("[^"]+"|\S+)\s+-m\s+unittest\s+(.+?)\s*$', str(cmd))
    if not m:
        return None
    exe = m.group(1).strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not re.match(r"^python(3(\.\d+)?)?(\.exe)?$", exe):
        return None
    allowed = {_norm(x) for x in test_files}
    paths = []
    for tok in m.group(2).split():
        path = _norm(tok) if tok.endswith(".py") or "/" in tok or "\\" in tok else tok.replace(".", "/") + ".py"
        if path not in allowed or ".." in path:
            return None
        paths.append(path)
    return paths or None


def validate_task(t: dict) -> str | None:
    """R1: returns a problem description, or None if the task is safe to run."""
    if not isinstance(t.get("id"), str) or not ID_RE.match(t["id"]):
        return f"bad id {t.get('id')!r}"
    if t.get("kind", "build") == "plan":
        pf = _norm(str(t.get("plan_file", "")))
        return None if pf and ".." not in pf and not re.match(r"^([A-Za-z]:|/)", pf) else "bad plan_file"
    tf, scope = t.get("test_files"), t.get("files_in_scope")
    if not isinstance(tf, list) or not tf or not all(isinstance(x, str) for x in tf):
        return "test_files must be a non-empty list"
    for x in tf:
        n = _norm(x)
        if not n.startswith("tests/") or not n.endswith(".py") or ".." in n:
            return f"bad test file {x!r}"
    if not isinstance(scope, list) or not scope or not all(isinstance(x, str) for x in scope):
        return "files_in_scope must be a non-empty list"
    for x in scope:
        if ".." in x or re.match(r"^([A-Za-z]:|/|\\)", x):
            return f"bad scope entry {x!r}"
    if parse_test_cmd(t.get("test_cmd", ""), tf) is None:
        return "unsafe test_cmd"
    return None


class Conductor:
    def __init__(self, repo: Path, work: Path, state: Path, team: Team, limits: dict, *,
                 owner_email: str, mailer: Callable[[str, str], None], inbox: Callable[[], list[dict]],
                 gh: Callable[[list[str]], tuple[int, str]], clock: Callable[[], datetime] | None = None,
                 judge_cmds: list[str] | None = None, push: bool = True):
        self.repo, self.work, self.state = Path(repo), Path(work), Path(state)
        self.team, self.limits = team, limits
        self.owner = owner_email.strip().lower()
        self.mailer, self.inbox, self.gh = mailer, inbox, gh
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.judge_cmds = list(judge_cmds or [])
        self.push = push
        self.meter = Meter(self.state, clock=self.clock)
        self.state.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ files
    def _read(self, name: str, default):
        p = self.state / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _write(self, name: str, data) -> None:
        p = self.state / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
        os.replace(tmp, p)

    def _queue(self) -> dict:
        return self._read("queue.json", {"layer": "", "tasks": []})

    def _save_queue(self, q: dict) -> None:
        self._write("queue.json", q)

    @property
    def wt(self) -> Path:
        return self.work / self._queue()["layer"]

    # ------------------------------------------------------------------ setup
    def init_queue(self, layer: str, tasks: list[dict]) -> None:
        for t in tasks:
            problem = validate_task(t)
            if problem:
                raise ValueError(f"task {t.get('id')!r}: {problem}")
        (self.state / "roles.json").write_bytes(json.dumps(ROLES, indent=2).encode("utf-8"))
        norm = [self._new_task(t) for t in tasks]
        self._save_queue({"layer": layer, "tasks": norm})
        self._ensure_worktree(layer)

    @staticmethod
    def _new_task(t: dict) -> dict:
        t = dict(t)
        t.setdefault("kind", "build")
        t.setdefault("status", "todo")
        t.setdefault("notes", [])
        t.setdefault("fail_signatures", [])
        t.setdefault("troubleshot", False)
        t.setdefault("fails_since", 0)
        t.setdefault("review_feedback", [])
        t.setdefault("trouble_notes", [])
        t.setdefault("troubleshoots", 0)
        return t

    def _ensure_worktree(self, layer: str) -> None:
        wt = self.work / layer
        if (wt / ".git").exists():
            return
        self.work.mkdir(parents=True, exist_ok=True)
        branches = _git(self.repo, "branch", "--list", layer)
        if branches:
            _git(self.repo, "worktree", "add", str(wt), layer)
        else:
            _git(self.repo, "worktree", "add", "-b", layer, str(wt), "main")
        _git(wt, "config", "user.name", "Forge")
        _git(wt, "config", "user.email", "forge@localhost")

    def _reset_wt(self) -> None:
        _git(self.wt, "reset", "-q", "--hard")
        _git(self.wt, "clean", "-q", "-fd")

    def _changed(self) -> list[str]:
        """Every added, modified, deleted or untracked path in the worktree (NUL-separated: no trimming bugs)."""
        p = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=str(self.wt), capture_output=True,
                           stdin=subprocess.DEVNULL, **NOWIN)
        if p.returncode != 0:
            raise RuntimeError("git error: status failed: " + p.stderr.decode("utf-8", "replace").strip()[:300])
        entries = p.stdout.decode("utf-8", "replace").split("\0")
        files, i = [], 0
        while i < len(entries):
            e = entries[i]
            if len(e) > 3:
                files.append(_norm(e[3:]))
                if e[0] in "RC":  # a rename/copy is followed by its source path
                    i += 1
            i += 1
        return sorted(set(files))

    def _commit(self, paths: list[str], msg: str) -> str:
        if paths:
            _git(self.wt, "add", "-A", "--", *paths)
        if not _git(self.wt, "diff", "--cached", "--name-only"):
            return ""
        _git(self.wt, "commit", "-q", "-m", msg)
        return _git(self.wt, "rev-parse", "HEAD")

    def _push(self) -> None:
        if self.push:
            _git(self.wt, "push", "-q", "-u", "origin", self._queue()["layer"], check=False)

    # ------------------------------------------------------------------ agents
    def _call(self, role: str, prompt: str, schema: dict | None):
        agent = getattr(self.team, role)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + role + "-" + uuid.uuid4().hex[:6]
        d = self.state / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.md").write_bytes(prompt.encode("utf-8"))
        before = self._fingerprint()
        try:
            r = agent.run(prompt, self.wt, schema)
        except Exception as e:  # noqa: BLE001 - an agent crash is a failed result
            from core.agents import AgentResult
            r = AgentResult("", 0, False, repr(e), None, getattr(agent, "provider", "unknown"))
        after = self._fingerprint()
        if after != before:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            (self.state / "KILL").write_text("state tampered during an agent run\n")
            self._log(f"TAMPER during {role} run {run_id}: {changed}")
            self._ask("tamper", f"Forge stopped: a {role} agent changed Forge's own state files",
                      "Files changed during the agent run:\n" + "\n".join(changed) +
                      "\n\nForge is halted (KILL). Nothing from that run was recorded.")
            raise Tampered(", ".join(changed))
        if r.tokens:
            self.meter.add(r.provider or "unknown", r.tokens)
        (d / "output.json").write_bytes(json.dumps({"ok": r.ok, "error": r.error, "text": r.text, "data": r.data,
                                                    "tokens": r.tokens, "provider": r.provider},
                                                   indent=2).encode("utf-8"))
        return r

    def _fingerprint(self) -> dict:
        """R9/R10/R14: every file in state/. The conductor writes nothing there while an agent runs, so nothing
        is exempt. Run records (which only grow) use a fast size+mtime signature; everything else a sha256."""
        fp = {}
        for f in self.state.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(self.state).as_posix()
            if rel.startswith("runs/"):
                st = f.stat()
                fp[rel] = f"{st.st_size}:{st.st_mtime_ns}"
            else:
                fp[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
        return fp

    def _log(self, msg: str) -> None:
        with (self.state / "errors.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")

    def _capped(self) -> bool:
        providers = {getattr(getattr(self.team, f), "provider", None) for f in Team.__dataclass_fields__}
        return any(p and self.meter.over(p, self.limits) for p in providers)

    # ------------------------------------------------------------------ email
    def _ask(self, kind: str, subject: str, body: str, **extra) -> str:
        qs = self._read("questions.json", {})
        qid = f"{kind}-{len(qs) + 1}"
        code = secrets.token_urlsafe(6)[:8]
        qs[qid] = {"kind": kind, "status": "open", "code": code, "subject": subject, "body": body,
                   "delivered": False, **extra}
        self._write("questions.json", qs)
        self._deliver(qid)
        return qid

    def _deliver(self, qid: str) -> None:
        qs = self._read("questions.json", {})
        q = qs[qid]
        try:
            self.mailer(f"[Forge Q-{qid} {q['code']}] {q['subject']}", q["body"])
        except Exception as e:  # noqa: BLE001 - retried every step (R7)
            self._log(f"mail to owner failed for {qid}: {e!r}")
            return
        qs = self._read("questions.json", {})
        qs[qid]["delivered"] = True
        self._write("questions.json", qs)

    def _mail_notice(self, qid: str, subject: str, body: str) -> None:
        """Follow-up email on an existing question (keeps its reply code)."""
        q = self._read("questions.json", {}).get(qid, {})
        try:
            self.mailer(f"[Forge Q-{qid} {q.get('code', '')}] {subject}", body)
        except Exception as e:  # noqa: BLE001
            self._log(f"notice mail failed for {qid}: {e!r}")

    def _handle_inbox(self) -> None:
        for qid, q in self._read("questions.json", {}).items():
            if q.get("delivered") is False:
                self._deliver(qid)
        try:
            messages = self.inbox() or []
        except Exception as e:  # noqa: BLE001 - email trouble never stops the conductor (R7)
            self._log(f"inbox read failed: {e!r}")
            return
        for m in messages:
            sender = re.findall(r"[\w.+-]+@[\w.-]+", str(m.get("from", "")).lower())
            if self.owner not in sender:
                continue
            subject, body = str(m.get("subject", "")), str(m.get("body", ""))
            if re.search(r"\bstop\b", subject + " " + body, re.I):
                (self.state / "KILL").write_text("stopped by owner email\n")
            mq = re.search(r"\[Forge Q-([\w-]+) ([\w-]{8})\]", subject)
            if mq:
                self._answer(mq.group(1), body, mq.group(2))

    def _answer(self, qid: str, body: str, code: str) -> None:
        qs = self._read("questions.json", {})
        q = qs.get(qid)
        if not q or q.get("status") != "open" or not secrets.compare_digest(str(q.get("code", "")), code):
            return
        reply = body.strip().splitlines()[0].strip() if body.strip() else ""
        if q["kind"] == "gate":
            first = reply.split()[0].lower().strip(".!,") if reply.split() else ""
            if first not in {"y", "yes"}:
                return
            pr = str(q["pr"])
            c1, o1 = self.gh(["pr", "edit", pr, "--add-label", "human-approved"])
            c2, o2 = self.gh(["pr", "merge", pr, "--merge", "--delete-branch"]) if c1 == 0 else (c1, o1)
            if c1 != 0 or c2 != 0:  # R3: stay open, tell Ben, a new "y" can retry
                self._log(f"gate merge failed for PR {pr}: {o1} {o2}")
                self._mail_notice(qid, "merge error: approval received, but the merge failed",
                                  f"Error from GitHub:\n{(o2 or o1)[:2000]}\n\nReply y again to retry once it's fixed.")
                return
        elif q["kind"] == "blocked":
            qd = self._queue()
            for t in qd["tasks"]:
                if t["id"] == q.get("task"):
                    t["trouble_notes"].append(f"Ben: {body.strip()}")
                    t["status"] = "tests_ok" if t.get("tests_commit") else "todo"
                    t["fail_signatures"], t["fails_since"], t["troubleshot"], t["troubleshoots"] = [], 0, False, 0
                    t["test_rejects"] = 0
                    t["plan_rejects"] = 0
            self._save_queue(qd)
        elif q["kind"] == "replan":
            qd = self._queue()
            qd.setdefault("notes", []).append(f"Ben: {body.strip()}")
            self._save_queue(qd)
            (self.state / "PAUSED").unlink(missing_ok=True)
        q["status"] = "answered"
        q["answer"] = body.strip()[:2000]
        self._write("questions.json", qs)

    # ------------------------------------------------------------------ main step
    def step(self) -> str:
        self._handle_inbox()
        if (self.state / "KILL").exists():
            return "killed"
        if (self.state / "PAUSED").exists():
            return "paused"
        if self._capped():
            return "capped"
        q = self._queue()
        if q.get("drift_due"):
            try:
                self._drift_check()
            except Tampered:
                return "killed"
            return "worked"
        for t in q["tasks"]:
            if t["status"] in ("todo", "tests_ok"):
                try:
                    self._ensure_worktree(q["layer"])
                    if t["kind"] == "plan":
                        self._plan_stage(t["id"])
                    elif t["status"] == "todo":
                        self._tests_stage(t["id"])
                    else:
                        self._build_stage(t["id"])
                except Tampered:
                    return "killed"
                except (RuntimeError, OSError) as e:  # R8: git or filesystem trouble is a failed attempt
                    self._log(f"stage error on {t['id']}: {e!r}")
                    try:
                        cur = self._task(t["id"])
                        self._update(t["id"], notes=cur["notes"] + [f"git error: {e}"[:300]])
                        if cur["status"] == "tests_ok":
                            self._after_failure(t["id"], f"git error: {e}"[:300], "git-error", "")
                    except Exception as e2:  # noqa: BLE001
                        self._log(f"could not record stage error: {e2!r}")
                    return "error"  # R13: run() backs off on this
                return "worked"
        tasks = q["tasks"]
        qs = self._read("questions.json", {})
        if tasks and all(t["status"] == "done" for t in tasks) and not q.get("drift_due") \
                and not any(v["kind"] == "gate" for v in qs.values()):
            self._gate()
            return "gate"
        return "idle"

    def run(self, max_steps: int | None = None, idle_sleep_s: int = 60, heartbeat: Path | None = None,
            sleep: Callable[[float], None] = time.sleep) -> str:
        """Loop forever (or max_steps). Nothing ends the loop except the kill switch: errors are logged,
        Ben is told once after 3 in a row, and the loop backs off (R12, R13)."""
        n, status, errors, told = 0, "idle", 0, False
        while max_steps is None or n < max_steps:
            try:
                if heartbeat:
                    heartbeat.write_text(f"{os.getpid()} {time.time()}")
                status = self.step()
            except Exception as e:  # noqa: BLE001
                status = "error"
                self._log(f"step crashed: {e!r}")
            n += 1
            if status == "killed":
                return status
            if status == "error":
                errors += 1
                if errors >= 3 and not told:
                    told = True
                    try:
                        self.mailer("[Forge] the conductor keeps hitting an error",
                                    "It keeps retrying with a growing pause. Details: state/bootstrap/errors.log")
                    except Exception:  # noqa: BLE001
                        pass
                sleep(min(idle_sleep_s * errors, 1800))
                continue
            errors, told = 0, False
            if status in ("idle", "paused", "capped", "gate"):
                sleep(idle_sleep_s)
        return status

    # ------------------------------------------------------------------ helpers
    def _update(self, tid: str, **changes) -> dict:
        q = self._queue()
        for t in q["tasks"]:
            if t["id"] == tid:
                t.update(changes)
                self._save_queue(q)
                return t
        raise KeyError(tid)

    def _task(self, tid: str) -> dict:
        return next(t for t in self._queue()["tasks"] if t["id"] == tid)

    def _block(self, tid: str, why: str) -> None:
        t = self._update(tid, status="blocked")
        self._ask("blocked", f"Task {tid} is blocked: {t['title']}",
                  f"Task {tid} ({t['title']}) is blocked.\n\nWhy: {why}\n\nNotes:\n" +
                  "\n".join(t.get("notes", [])[-10:] + t.get("trouble_notes", [])[-5:]) +
                  "\n\nReply to this email with guidance and the task will be retried with it. "
                  "Otherwise Forge continues with other work.", task=tid)

    def _run_tests(self, t: dict) -> tuple[int, str, bool]:
        """R1: run a task's unittest command without a shell. Returns (exit, output, timed_out)."""
        paths = parse_test_cmd(t["test_cmd"], t["test_files"])
        if paths is None:
            return 2, "unsafe test_cmd", False
        try:
            p = subprocess.run([sys.executable, "-m", "unittest", *paths], cwd=str(self.wt), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               timeout=int(self.limits.get("test_timeout_s", 600)), **NOWIN)
            return p.returncode, (p.stdout or "") + (p.stderr or ""), False
        except subprocess.TimeoutExpired:
            return 124, "test command timed out", True

    def _run_cmd(self, cmd: str) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(self.wt), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", stdin=subprocess.DEVNULL,
                               timeout=int(self.limits.get("test_timeout_s", 600)), **NOWIN)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, f"command timed out: {cmd}"

    def _task_prompt(self, t: dict) -> str:
        return (f"TASK {t['id']}: {t['title']}\n\n{t.get('section', '')}\n\n"
                f"Files you may change: {', '.join(t.get('files_in_scope', []))}\n"
                f"Test files (read-only for builders): {', '.join(t.get('test_files', []))}\n"
                f"Done means this passes: {t.get('test_cmd', '')}\n")

    # ------------------------------------------------------------------ stage A: tests
    def _tests_stage(self, tid: str) -> None:
        t = self._task(tid)
        self._reset_wt()
        prompt = ("You are the TEST WRITER. Write only these files: " + ", ".join(t["test_files"]) +
                  ". The tests must fail until the feature exists. Do not write any other file.\n\n" +
                  self._task_prompt(t) + "\nAnswer with JSON: {\"files\": [...], \"summary\": \"...\"}")
        r = self._call("test_writer", prompt, S_TESTS)
        changed = self._changed()
        reason = None
        if not r.ok:
            reason = f"tests rejected: test writer failed ({r.error})"
        elif not changed:
            reason = "tests rejected: no test files written"
        elif any(f not in [_norm(x) for x in t["test_files"]] for f in changed):
            reason = "tests rejected: wrote outside test_files: " + ", ".join(
                f for f in changed if f not in [_norm(x) for x in t["test_files"]])
        else:
            code, out, timed_out = self._run_tests(t)
            ran = re.search(r"Ran ([1-9]\d*) tests?", out)
            if timed_out or not ran:
                reason = "tests rejected: no real failing run (timed out or no tests ran)"
            elif code == 0:
                reason = "tests rejected: weak (they pass before the feature exists)"
        if reason:
            self._reset_wt()
            rejects = t.get("test_rejects", 0) + 1
            self._update(tid, notes=t["notes"] + [reason], test_rejects=rejects)
            if rejects >= 2:
                self._block(tid, reason)
            return
        sha = self._commit(changed, f"{tid}: acceptance tests")
        self._update(tid, status="tests_ok", tests_commit=sha, fails_since=0, fail_signatures=[])

    # ------------------------------------------------------------------ stage B: build
    def _ledger(self) -> Ledger:
        return Ledger(self.state)

    def _apply(self, pid: str, action: str, cid: str, ident: str, payload: dict | None = None) -> bool:
        try:
            self._ledger().apply({"proposal_id": pid, "action": action, "contract_id": cid,
                                  "payload": payload or {}}, ident)
            return True
        except Rejected:
            return False

    def _dead_ends(self) -> list[str]:
        p = self.state / "dead_ends.jsonl"
        if not p.exists():
            return []
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _build_stage(self, tid: str) -> None:
        t = self._task(tid)
        cid = t["id"]
        led = self._ledger()
        if cid not in led.contracts():
            self._apply(f"create-{cid}", "create", cid, "forge-manager", {
                "title": t["title"], "spec_ref": cid, "acceptance": t["test_cmd"],
                "files_in_scope": list(t["files_in_scope"]), "max_attempts": 6, "token_budget": 10 ** 9})
        c = self._ledger().contracts().get(cid, {})
        if c.get("status") == "parked":
            self._block(tid, "ledger parked the contract (attempt or budget limit)")
            return
        tag = f"{cid}-{uuid.uuid4().hex[:8]}"
        self._apply(f"{tag}-claim", "claim", cid, "forge-executor")

        self._reset_wt()
        tests_commit = t["tests_commit"]

        prompt = "You are the BUILDER. Make the tests pass by changing only the files you may change.\n\n" + \
                 self._task_prompt(t)
        if t.get("review_feedback"):
            prompt += "\nREVIEW FEEDBACK:\n" + "\n".join(f"- {x}" for x in t["review_feedback"]) + "\n"
        if t.get("trouble_notes"):
            prompt += "\nTROUBLESHOOTER NOTES:\n" + "\n".join(t["trouble_notes"]) + "\n"
        dead = self._dead_ends()
        if dead:
            prompt += "\nKNOWN DEAD ENDS:\n" + "\n".join(dead) + "\n"
        prompt += ("\nAnswer with JSON: {\"status\": \"done\" | \"blocked\", \"summary\": \"...\"}. "
                   "A blocked answer must also include \"tried\" (at least 2 different routes you actually tried) "
                   "and \"error\" (the real error output); without them it is rejected as an easy way out.")
        r = self._call("builder", prompt, S_BUILD)

        tests = [_norm(x) for x in t["test_files"]]
        changed = self._changed()
        violations = [f for f in changed if f in tests]
        if violations:
            _git(self.wt, "checkout", "-q", "HEAD", "--", *violations)
            _git(self.wt, "clean", "-q", "-f", "--", *violations)
        changed = [f for f in changed if f not in tests]
        out_of_scope = [f for f in changed if not any(fnmatch.fnmatch(f, pat) for pat in t["files_in_scope"])]

        def fail(reason: str, sig: str, output: str = "", submitted: bool = False) -> None:
            _git(self.wt, "reset", "-q", "--hard", tests_commit)
            _git(self.wt, "clean", "-q", "-fd")
            if submitted:
                self._apply(f"{tag}-fail", "fail", cid, "forge-auditor")
            else:
                self._apply(f"{tag}-release", "release", cid, "forge-core")
            if self._ledger().contracts().get(cid, {}).get("status") == "failed":
                self._apply(f"{tag}-reopen", "reopen", cid, "forge-manager")
            self._after_failure(tid, reason, sig, output)

        if not r.ok:
            return fail(f"builder output unusable: {r.error}", f"builder-error:{r.error}")
        if (r.data or {}).get("status") == "blocked":
            d = r.data or {}
            summary = d.get("summary") or "no detail"
            tried, err = d.get("tried"), d.get("error")
            if not (isinstance(tried, list) and len(tried) >= 2 and isinstance(err, str) and err.strip()):
                with (self.state / "easy_outs.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"task": tid, "kind": "easy_out", "summary": summary}) + "\n")
                return fail("blocker rejected: no evidence (easy out)", "easy-out")
            return fail(f"blocker: {summary} (tried: {'; '.join(map(str, tried))}; error: {err})", f"blocker:{summary}")
        if violations:  # D-025 / drill 7: an attempt that touched its own tests can never pass
            return fail("touched test files (reverted): " + ", ".join(violations),
                        "touched tests: " + ",".join(violations))
        if out_of_scope:
            return fail("out of scope: " + ", ".join(out_of_scope), "out of scope: " + ",".join(out_of_scope))

        sha = self._commit(changed, f"{cid}: {t['title']}") or _git(self.wt, "rev-parse", "HEAD")
        self._apply(f"{tag}-report", "run_report", cid, "forge-core", {
            "run_id": tag, "claim": (r.data or {}).get("status"), "commit": sha, "changed": changed,
            "violations": violations, "out_of_scope": []})
        self._apply(f"{tag}-submit", "submit", cid, "forge-executor", {"commit": sha})

        results = [("task tests", *self._run_tests(t)[:2])] + [(cmd, *self._run_cmd(cmd)) for cmd in self.judge_cmds]
        for cmd, code, output in results:
            if code != 0:
                self._apply(f"{tag}-ci", "test_run", cid, "ci", {"run_id": f"{tag}-ci", "commit": sha, "passed": False})
                tail = "\n".join(output.splitlines()[-20:])
                # numbers (timings, line numbers, addresses) vary between identical failures; ignore them
                sig = hashlib.sha256(re.sub(r"\d+", "N", tail).encode()).hexdigest()
                return fail(f"judge failed: {cmd}", sig, tail, submitted=True)
        self._apply(f"{tag}-ci", "test_run", cid, "ci", {"run_id": f"{tag}-ci", "commit": sha, "passed": True})

        diff = _git(self.wt, "diff", f"{tests_commit}..{sha}")
        rv = self._call("reviewer", "You are the REVIEWER (read-only). Check this change against the task. "
                                    "Reject shortcuts, bare-minimum work, drift from the task, and anything that "
                                    "weakens tests.\n\n" + self._task_prompt(t) + "\nDIFF:\n" + diff[:60000] +
                        "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW)
        if not rv.ok:
            return fail(f"reviewer output unusable: {rv.error}", f"reviewer-error:{rv.error}", submitted=True)
        if (rv.data or {}).get("verdict") != "pass":
            reasons = [str(x) for x in (rv.data or {}).get("reasons") or ["no reasons given"]]
            self._update(tid, review_feedback=reasons)
            return fail("review failed: " + "; ".join(reasons), "review:" + "|".join(reasons), submitted=True)

        if not self._apply(f"{tag}-pass", "pass", cid, "forge-auditor", {"run_id": f"{tag}-ci"}):
            return fail("ledger refused the pass (evidence incomplete)", "ledger-refused-pass", submitted=True)
        self._update(tid, status="done", done_commit=sha)
        self._push()
        q = self._queue()
        q["drift_due"] = True  # the drift keeper runs as the next step, so it can be stopped like any agent
        self._save_queue(q)

    def _is_ancestor(self, sha: str) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=str(self.wt),
                              capture_output=True, **NOWIN).returncode == 0

    def _after_failure(self, tid: str, reason: str, sig: str, output: str) -> None:
        t = self._task(tid)
        sigs = t["fail_signatures"] + [sig]
        fails = t.get("fails_since", 0) + 1
        t = self._update(tid, fail_signatures=sigs, fails_since=fails, notes=t["notes"] + [reason])
        zero_progress = len(sigs) >= 2 and sigs[-1] == sigs[-2]
        rounds = t.get("troubleshoots", 0)
        if rounds >= 3:
            if fails >= 2:
                self._block(tid, reason)
            return
        if zero_progress or fails >= 2:
            self._troubleshoot(tid, reason, output)

    def _troubleshoot(self, tid: str, reason: str, output: str) -> None:
        t = self._task(tid)
        self._reset_wt()
        prompt = ("You are the TROUBLESHOOTER. The builder is stuck on this task. Diagnose the cause and give "
                  "concrete notes the next builder attempt can follow. If this route is a dead end, say so and "
                  "name the alternative.\n\n" + self._task_prompt(t) +
                  "\nRECENT FAILURES:\n" + "\n".join(t["notes"][-6:]) +
                  "\n\nLAST JUDGE OUTPUT:\n" + output[-4000:] +
                  "\nAnswer with JSON: {\"kind\": \"fix\" | \"dead_end\", \"notes\": \"...\", \"alternative\": \"...\"}")
        r = self._call("troubleshooter", prompt, S_TROUBLE)
        self._reset_wt()
        notes = t.get("trouble_notes", [])
        if r.ok:
            d = r.data or {}
            notes = notes + [str(d.get("notes", ""))]
            if d.get("kind") == "dead_end":
                with (self.state / "dead_ends.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"task": tid, "notes": d.get("notes", ""),
                                        "alternative": d.get("alternative", "")}) + "\n")
        else:
            notes = notes + [f"(troubleshooter failed: {r.error})"]
        self._update(tid, trouble_notes=notes, troubleshot=True, fails_since=0,
                     troubleshoots=t.get("troubleshoots", 0) + 1)

    # ------------------------------------------------------------------ drift
    def _drift_check(self) -> None:
        q = self._queue()
        design = self.wt / "docs" / "specs" / "layer-1-design.md"
        text = design.read_text(encoding="utf-8") if design.exists() else "(no design file)"
        listing = "\n".join(f"- {t['id']} [{t['status']}] {t['title']}" for t in q["tasks"])
        r = self._call("drift_keeper", "You are the DRIFT KEEPER (read-only). Is this work still on course for "
                                       "the design? Say replan only if it is drifting.\n\nTASKS:\n" + listing +
                       "\n\nDESIGN:\n" + text[:40000] +
                       "\nAnswer with JSON: {\"status\": \"ok\" | \"replan\", \"reasons\": [...]}", S_DRIFT)
        status = (r.data or {}).get("status") if r.ok else None
        q = self._queue()
        if status not in ("ok", "replan"):  # R6: unusable result, retry; escalate after 3
            q["drift_failures"] = q.get("drift_failures", 0) + 1
            self._save_queue(q)
            if q["drift_failures"] >= 3:
                (self.state / "PAUSED").write_text("drift keeper failed 3 times\n")
                self._ask("replan", "Forge paused: the drift check keeps failing",
                          f"The drift keeper returned unusable output 3 times (last error: {r.error}).\n\n"
                          "Reply with guidance to resume.")
            return
        q["drift_due"], q["drift_failures"] = False, 0
        self._save_queue(q)
        if status == "replan":
            reasons = (r.data or {}).get("reasons") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            (self.state / "PAUSED").write_text("drift keeper asked for a re-plan\n")
            self._ask("replan", "Forge paused: the drift keeper wants a re-plan",
                      "Reasons:\n" + "\n".join(f"- {x}" for x in reasons) +
                      "\n\nReply with guidance to resume.")

    # ------------------------------------------------------------------ plan tasks
    def _plan_stage(self, tid: str) -> None:
        t = self._task(tid)
        self._reset_wt()
        plan_file = _norm(t["plan_file"])
        r = self._call("planner", "You are the PLANNER. Write the implementation plan to " + plan_file +
                       " (and no other file), then return its tasks.\n\n" + self._task_prompt(t) +
                       "\nEach task needs: id, title, section, files_in_scope, test_files, test_cmd.\n"
                       "Answer with JSON: {\"tasks\": [...]}", S_PLAN)
        changed = self._changed()
        tasks = (r.data or {}).get("tasks") if r.ok else None
        reason = None
        if not r.ok:
            reason = f"plan rejected: planner failed ({r.error})"
        elif any(f != plan_file for f in changed) or plan_file not in changed:
            reason = "plan rejected: wrote outside plan_file" if any(f != plan_file for f in changed) \
                else "plan rejected: plan file not written"
        elif not isinstance(tasks, list) or not tasks or not all(
                isinstance(x, dict) and all(k in x for k in TASK_FIELDS) for x in tasks):
            reason = "plan rejected: tasks missing required fields"
        elif any(validate_task(dict(x, kind="build")) for x in tasks):
            bad = next(validate_task(dict(x, kind="build")) for x in tasks if validate_task(dict(x, kind="build")))
            reason = "plan rejected: unsafe test_cmd" if "test_cmd" in bad else f"plan rejected: {bad}"
        else:
            existing = {x["id"] for x in self._queue()["tasks"]}
            if any(x["id"] in existing for x in tasks):
                reason = "plan rejected: task ids clash with existing tasks"
        if not reason:
            plan_text = (self.wt / plan_file).read_text(encoding="utf-8")
            rv = self._call("reviewer", "You are the REVIEWER (read-only). Check this plan against the task and "
                                        "design: complete, testable, no placeholders, no drift.\n\n" +
                            self._task_prompt(t) + "\nPLAN:\n" + plan_text[:60000] + "\nTASKS JSON:\n" +
                            json.dumps(tasks)[:20000] +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW)
            if not rv.ok or (rv.data or {}).get("verdict") != "pass":
                reason = "plan review failed: " + "; ".join(str(x) for x in ((rv.data or {}).get("reasons") or [rv.error]))
        if reason:
            self._reset_wt()
            rejects = t.get("plan_rejects", 0) + 1
            self._update(tid, notes=t["notes"] + [reason], plan_rejects=rejects)
            if rejects >= 2:
                self._block(tid, reason)
            return
        self._commit([plan_file], f"{tid}: plan")
        q = self._queue()
        for x in q["tasks"]:
            if x["id"] == tid:
                x["status"] = "done"
        for x in tasks:
            nt = self._new_task({k: x[k] for k in TASK_FIELDS})
            nt["kind"], nt["status"] = "build", "todo"
            q["tasks"].append(nt)
        self._save_queue(q)
        self._push()

    # ------------------------------------------------------------------ gate
    def _gate(self) -> None:
        q = self._queue()
        self._push()
        report = f"{q['layer']} finished its queue. Every task passed tests at its exact commit, the judges, and " \
                 "an independent Codex review.\n\n" + \
                 "\n".join(f"- {t['id']} [{t['status']}] {t['title']}" for t in q["tasks"])
        code, out = self.gh(["pr", "create", "--base", "main", "--head", q["layer"],
                             "--title", f"{q['layer']}: ready for approval", "--body", report])
        m = re.search(r"/pull/(\d+)", out or "")
        if code != 0 or not m:  # R3: no gate question without a real PR; tell Ben (not on every retry)
            n = q.get("gate_errors", 0) + 1
            q["gate_errors"] = n
            self._save_queue(q)
            self._log(f"gate PR create failed: {out}")
            if n == 1 or n % 20 == 0:
                try:
                    self.mailer(f"[Forge] {q['layer']} is done but the pull request failed",
                                f"GitHub said:\n{(out or '')[:2000]}\n\nForge keeps retrying.")
                except Exception as e:  # noqa: BLE001
                    self._log(f"mail failed: {e!r}")
            return
        pr = m.group(1)
        self._ask("gate", f"{q['layer']} is ready: reply y to approve",
                  report + f"\n\nPull request: {out.strip()}\n\nReply y to approve and merge into main.", pr=pr)


# ---------------------------------------------------------------------- real I/O
def gmail_mailer(owner: str) -> Callable[[str, str], None]:
    def send(subject: str, body: str) -> None:
        import keyring
        import smtplib
        from email.message import EmailMessage
        pw = keyring.get_password("forge-gmail", owner)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = owner, owner, subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(owner, pw)
            s.send_message(msg)
    return send


def gmail_inbox(owner: str, state: Path) -> Callable[[], list[dict]]:
    def read() -> list[dict]:
        import email
        import imaplib
        import keyring
        from email.header import decode_header, make_header
        pw = keyring.get_password("forge-gmail", owner)
        out = []
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            m.login(owner, pw)
            m.select("INBOX")
            _, data = m.search(None, '(UNSEEN SUBJECT "[Forge Q-")')
            ids = data[0].split() if data and data[0] else []
            _, data2 = m.search(None, '(UNSEEN FROM "%s" SUBJECT "STOP")' % owner)
            ids += [i for i in (data2[0].split() if data2 and data2[0] else []) if i not in ids]
            for i in ids:
                _, raw = m.fetch(i, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])
                body = ""
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        break
                # keep only the new reply text, not the quoted original
                body = re.split(r"\n\s*On .+wrote:\s*\n", body, maxsplit=1)[0]
                out.append({"from": str(msg.get("From", "")), "subject": str(make_header(decode_header(msg.get("Subject", "")))),
                            "body": body})
                m.store(i, "+FLAGS", "\\Seen")
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        return out
    return read


def gh_cli(repo: Path) -> Callable[[list[str]], tuple[int, str]]:
    def run(args: list[str]) -> tuple[int, str]:
        p = subprocess.run(["gh", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, **NOWIN)
        return p.returncode, p.stdout + p.stderr
    return run


def real_team(limits: dict) -> Team:
    from core.agents import ClaudeAgent, CodexAgent
    t = limits.get("agent_timeout_s", 1800)
    return Team(test_writer=CodexAgent(t, sandbox="workspace-write"),
                builder=ClaudeAgent(t, permission_mode="acceptEdits",
                                    allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash(python:*)"]),
                reviewer=CodexAgent(t, sandbox="read-only"),
                troubleshooter=ClaudeAgent(t, permission_mode="acceptEdits",
                                           allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash(python:*)",
                                                          "WebSearch", "WebFetch"]),
                drift_keeper=ClaudeAgent(t, permission_mode="plan", allowed_tools=["Read", "Glob", "Grep"]),
                planner=ClaudeAgent(t, permission_mode="acceptEdits",
                                    allowed_tools=["Read", "Edit", "Write", "Glob", "Grep"]))


def acquire_lock(state: Path):
    """R11: OS-level exclusive lock held for the life of the process. Returns a handle, or None if taken."""
    state.mkdir(parents=True, exist_ok=True)
    f = open(state / "conductor.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main(argv: list[str]) -> int:
    from core.agents import load_limits
    forge = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "run", "step", "status"])
    ap.add_argument("--layer")
    ap.add_argument("--tasks")
    ap.add_argument("--owner", default="benjaminanderson0802@gmail.com")
    ap.add_argument("--work", default=str(Path.home() / "Forge-work"))
    a = ap.parse_args(argv)
    limits = load_limits(forge)
    state = forge / "state" / "bootstrap"
    if a.cmd == "status":
        q = json.loads((state / "queue.json").read_text(encoding="utf-8")) if (state / "queue.json").exists() else {}
        for t in q.get("tasks", []):
            print(f"{t['status']:9} {t['id']:6} {t['title']}")
        for f in ("KILL", "PAUSED"):
            if (state / f).exists():
                print(f"{f} is set")
        return 0
    c = Conductor(forge, Path(a.work), state, real_team(limits), limits, owner_email=a.owner,
                  mailer=gmail_mailer(a.owner), inbox=gmail_inbox(a.owner, state), gh=gh_cli(forge),
                  judge_cmds=["python drills/run_drills.py", "python -m unittest discover -s tests/core"])
    if a.cmd == "init":
        c.init_queue(a.layer, json.loads(Path(a.tasks).read_text(encoding="utf-8")))
        print("queue ready")
        return 0
    if a.cmd == "step":
        print(c.step())
        return 0
    lock = acquire_lock(state)
    if lock is None:
        return 0  # another conductor holds the lock; the watchdog calls us harmlessly
    try:
        c.mailer("[Forge] conductor started", "The conductor is running in the background. You'll hear from it only "
                 "when something needs you, when a layer is ready for approval, or if it hits trouble.\n\n"
                 "To stop everything: reply STOP to any Forge email.")
    except Exception as e:  # noqa: BLE001
        c._log(f"start mail failed: {e!r}")
    print(c.run(heartbeat=state / "conductor.heartbeat"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
