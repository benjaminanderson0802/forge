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

from core.finalize import ApprovedMerges, Finalizer, Hooks, Journal, safe_push
from core.ledger import Ledger, Rejected
from core.roles import role_text
from core.usage import Meter
from core.worktrees import Worktrees

ROLES = {"ci": "ci", "forge-manager": "manager", "forge-executor": "executor",
         "forge-auditor": "auditor", "forge-core": "core", "benjamin": "human"}
NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
TASK_FIELDS = ("id", "title", "section", "files_in_scope", "test_files", "test_cmd")
NOTE_CAP, NOTES_KEEP, BODY_CAP = 2000, 30, 20000  # R19
SUBJECT_CAP, CLOSED_KEEP, SENT_IDS_KEEP = 300, 50, 500  # R26, R28
MUTATION_NA = "not applicable: merge commit adds no builder lines"


def _prune_questions(qs: dict) -> dict:
    """R28: keep every open question and the CLOSED_KEEP most recently closed ones."""
    closed = [k for k, v in qs.items() if v.get("status") != "open"]
    closed.sort(key=lambda k: (qs[k].get("closed_at", ""), list(qs).index(k)))
    for k in closed[:-CLOSED_KEEP] if len(closed) > CLOSED_KEEP else []:
        del qs[k]
    return qs


def is_stop(subject: str, cleaned_body: str) -> bool:
    """R27: Ben wrote "stop": the whole subject (Re:/Fwd: removed), or the word "stop" in his new text unless it
    is negated ("don't stop"). Quoted text is already gone. Over-stopping is the safe failure."""
    subj = re.sub(r"^\s*((re|fwd?|aw)\s*:\s*)+", "", subject or "", flags=re.I).strip().strip(".!").lower()
    if subj == "stop":
        return True
    for m in re.finditer(r"\bstop\b", cleaned_body or "", re.I):
        before = [w.replace("\u2019", "'").strip(".,!") for w in cleaned_body[:m.start()].lower().split()[-2:]]
        if not any(w in {"don't", "dont", "not", "never", "no"} for w in before):
            return True
    return False


_QUOTE_STARTS = [
    re.compile(r"(?m)^[ \t]*On [^\n]*(\n[^\n]*)?wrote:[ \t]*$"),
    re.compile(r"(?mi)^[ \t]*-{2,}\s*Original Message\s*-{2,}[ \t]*$"),
    re.compile(r"(?m)^[ \t]*_{10,}[ \t]*$"),
    re.compile(r"(?mi)^[ \t]*From:[^\n]*(\n[^\n]*){0,3}\n[ \t]*(Sent|Date|To):"),
]


def clean_reply(body: str) -> str:
    """R18/R33: only the new text of a reply: nothing from the first quote header onward, no quoted lines, capped."""
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n")  # real email bodies use CRLF
    cut = min((m.start() for rx in _QUOTE_STARTS for m in [rx.search(body)] if m), default=len(body))
    lines = [ln for ln in body[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()[:NOTE_CAP]


_STR, _STRS = {"type": "string"}, {"type": "array", "items": {"type": "string"}}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


# R17: full JSON Schemas; "required" is what the conductor needs, the rest is optional.
S_TESTS = _obj({"files": _STRS, "summary": _STR}, ["files"])
S_BUILD = _obj({"status": {"type": "string", "enum": ["done", "blocked"]}, "summary": _STR, "tried": _STRS,
                "error": _STR}, ["status"])
S_REVIEW = _obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": _STRS}, ["verdict", "reasons"])
S_TROUBLE = _obj({"kind": {"type": "string", "enum": ["fix", "dead_end", "suggestion"]}, "notes": _STR,
                  "alternative": _STR}, ["kind", "notes"])
S_DRIFT = _obj({"status": {"type": "string", "enum": ["ok", "replan"]}, "reasons": _STRS}, ["status"])
S_PLAN = _obj({"tasks": {"type": "array", "items": _obj(
    {"id": _STR, "title": _STR, "section": _STR, "files_in_scope": _STRS, "test_files": _STRS, "test_cmd": _STR},
    list(TASK_FIELDS))}}, ["tasks"])


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


class Capped(Exception):
    """R37: the agent's provider is at its daily token cap; nothing was launched."""


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

    def _write(self, name: str, data, durable: bool = False) -> None:
        p = self.state / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        if not durable:
            tmp.write_bytes(raw)
            os.replace(tmp, p)
            return
        with open(tmp, "wb") as f:  # durable: the bytes reach the disk before the rename, the rename after it
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        if os.name != "nt":
            try:
                fd = os.open(str(self.state), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass

    def _queue(self) -> dict:
        return self._read("queue.json", {"layer": "", "tasks": []})

    def _save_queue(self, q: dict, durable: bool = False) -> None:
        if isinstance(q.get("notes"), list):  # R28
            q["notes"] = [str(x)[:NOTE_CAP] for x in q["notes"]][-NOTES_KEEP:]
        for t in q.get("tasks", []):  # R19: nothing grows without bound
            for key in ("notes", "trouble_notes"):
                if isinstance(t.get(key), list):
                    t[key] = [str(x)[:NOTE_CAP] for x in t[key]][-NOTES_KEEP:]
        self._write("queue.json", q, durable=durable)

    @property
    def wt(self) -> Path:
        return self.work / self._queue()["layer"]

    @property
    def trees(self) -> Worktrees:
        return Worktrees(self.repo, self.work)

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

    def _changed(self, cwd: Path | None = None) -> list[str]:
        """Every added, modified, deleted or untracked path in the worktree (NUL-separated: no trimming bugs)."""
        p = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=str(cwd or self.wt), capture_output=True,
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

    def _commit(self, paths: list[str], msg: str, cwd: Path | None = None) -> str:
        wt = Path(cwd) if cwd else self.wt
        if paths:
            _git(wt, "add", "-A", "--", *paths)
        if not _git(wt, "diff", "--cached", "--name-only"):
            return ""
        _git(wt, "-c", "user.name=Forge", "-c", "user.email=forge@localhost", "commit", "-q", "-m", msg)
        return _git(wt, "rev-parse", "HEAD")

    def _push(self) -> None:
        """Every push goes through finalize.safe_push. Anything but "ok" is a stage error, never a success."""
        if not self.push:
            return
        status, out = safe_push(self.wt, self._queue()["layer"], ApprovedMerges(self.state))
        if status != "ok":
            self._log(f"push {status}: {out[:500]}")
            raise RuntimeError(f"push {status}: {out[:300]}")

    # ------------------------------------------------------------------ agents
    def _call(self, role: str, prompt: str, schema: dict | None, cwd: Path | None = None):
        agent = getattr(self.team, role)
        provider = getattr(agent, "provider", None)
        if provider and self.meter.over(provider, self.limits):  # R37: checked before every launch
            raise Capped(provider)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + role + "-" + uuid.uuid4().hex[:6]
        d = self.state / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.md").write_bytes(prompt.encode("utf-8"))
        try:
            before = self._fingerprint()
        except OSError as e:  # R15: fail closed; state we can't fully check can't be protected
            raise RuntimeError(f"state could not be fingerprinted before agent run: {type(e).__name__}: {e}") from e
        unreadable = sorted(k for k, v in before.items() if v.startswith("unreadable:"))
        if unreadable:  # R15: fail closed; a file we can't read can't be checked for tampering
            raise RuntimeError("state file unreadable before agent run: " + ", ".join(unreadable))
        if not before.get(LOCK_NAME, "lock:0:").startswith("lock:0:"):  # R15: only an empty lock is trusted
            raise RuntimeError(f"state file {LOCK_NAME} is not empty before agent run")
        try:
            r = agent.run(prompt, Path(cwd) if cwd else self.wt, schema)
        except Exception as e:  # noqa: BLE001 - an agent crash is a failed result
            from core.agents import AgentResult
            r = AgentResult("", 0, False, repr(e), None, getattr(agent, "provider", "unknown"))
        try:
            after = self._fingerprint()
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        except Exception as e:  # noqa: BLE001 - R15: a check that can't complete counts as tampering
            changed = [f"state could not be fingerprinted after the run: {type(e).__name__}: {e}"]
        if changed:
            (self.state / "KILL").write_text("state tampered during an agent run\n")
            try:
                self._log(f"TAMPER during {role} run {run_id}: {changed}")
                self._ask("tamper", f"Forge stopped: a {role} agent changed Forge's own state files",
                          "Files changed during the agent run:\n" + "\n".join(changed) +
                          "\n\nForge is halted (KILL). Nothing from that run was recorded.", halt=True)
            except Exception:  # noqa: BLE001 - the halt stands even if the report can't be written
                pass
            raise Tampered(", ".join(changed))
        if r.tokens:
            self.meter.add(r.provider or "unknown", r.tokens)
        (d / "output.json").write_bytes(json.dumps({"ok": r.ok, "error": r.error, "text": r.text, "data": r.data,
                                                    "tokens": r.tokens, "provider": r.provider},
                                                   indent=2).encode("utf-8"))
        return r

    def _fingerprint(self) -> dict:
        """R9/R10/R14: every file in state/. The conductor writes nothing there while an agent runs, so nothing
        is exempt. Run records (which only grow) and the lock file use a size+mtime signature; everything else a
        sha256 (R15)."""
        fp = {}

        def fail(e: OSError) -> None:  # R15: a folder we can't list is an error, never silently skipped
            raise e

        for root, _dirs, files in os.walk(self.state, onerror=fail):
            for name in files:
                f = Path(root) / name
                rel = f.relative_to(self.state).as_posix()
                fp[rel] = self._signature(f, rel)
        return fp

    @staticmethod
    def _signature(f: Path, rel: str) -> str:
        st = f.stat()
        if rel == LOCK_NAME:  # R15: unreadable to us on Windows; kept empty, so identity is enough
            return f"lock:{st.st_size}:{st.st_ino}:{st.st_mtime_ns}"
        if rel.startswith("runs/"):
            return f"{st.st_size}:{st.st_mtime_ns}"
        try:
            return hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:  # R15: never skip; an unreadable file can't match any readable hash
            return f"unreadable:{st.st_size}:{st.st_mtime_ns}"

    def _log(self, msg: str) -> None:
        with (self.state / "errors.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")

    def _capped(self) -> bool:
        providers = {getattr(getattr(self.team, f), "provider", None) for f in Team.__dataclass_fields__}
        return any(p and self.meter.over(p, self.limits) for p in providers)

    # ------------------------------------------------------------------ email
    def _ask(self, kind: str, subject: str, body: str, halt: bool = False, **extra) -> str:
        qs = self._read("questions.json", {})
        seq = self._read("q_seq.json", {"n": len(qs)})
        seq["n"] = int(seq.get("n", 0)) + 1
        self._write("q_seq.json", seq)
        qid = f"{kind}-{seq['n']}"
        code = secrets.token_urlsafe(6)[:8]
        qs[qid] = {"kind": kind, "status": "open", "code": code, "subject": str(subject)[:SUBJECT_CAP],
                   "body": str(body)[:BODY_CAP], "delivered": False, **extra}  # R28
        if halt:
            qs[qid]["halt"] = True  # R32: retried on watchdog starts while KILL is set
        self._write("questions.json", _prune_questions(qs))
        self._deliver(qid, halt=halt)
        return qid

    def _send(self, subject: str, body: str, halt: bool = False) -> bool:
        """R20/R24/R25: every email goes through here. Nothing but a halt alert while KILL is set; at most
        mail_per_hour / mail_per_day attempts (counted before sending); each email gets a recorded Message-ID."""
        if (self.state / "KILL").exists() and not halt:
            return False
        now = self.clock()
        notes = self._read("notices.json", {})
        if halt:  # R24: at most one halt alert every 12 hours
            last = notes.get("halt")
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 12 * 3600:
                return False
        log = self._read("mail_log.json", {"sent": [], "budget_logged": "", "ids": []})
        sent = [x for x in log.get("sent", []) if (now - datetime.fromisoformat(x)).total_seconds() < 86400]
        hour = [x for x in sent if (now - datetime.fromisoformat(x)).total_seconds() < 3600]
        if len(hour) >= int(self.limits.get("mail_per_hour", 6)) or len(sent) >= int(self.limits.get("mail_per_day", 30)):
            stamp = now.strftime("%Y-%m-%dT%H")
            if log.get("budget_logged") != stamp:
                self._log(f"mail budget reached; held back: {subject[:120]}")
                log["budget_logged"] = stamp
            log["sent"] = sent
            self._write("mail_log.json", log)
            return False
        if halt:  # R32: the halt throttle starts only when an attempt actually goes ahead
            notes["halt"] = now.isoformat()
            self._write("notices.json", notes)
        from email.utils import make_msgid
        mid = make_msgid(domain="forge.local")
        log["sent"] = sent + [now.isoformat()]  # R25: the attempt counts even if SMTP fails part-way
        log["ids"] = (list(log.get("ids", [])) + [mid])[-SENT_IDS_KEEP:]
        self._write("mail_log.json", log)
        try:
            if self._mailer_takes_id:
                self.mailer(str(subject)[:SUBJECT_CAP], body[:BODY_CAP], message_id=mid)
            else:
                self.mailer(str(subject)[:SUBJECT_CAP], body[:BODY_CAP])
        except Exception as e:  # noqa: BLE001 - retried later, within budget (R7, R25)
            self._log(f"mail failed: {subject[:120]}: {e!r}")
            return False
        return True

    @property
    def _mailer_takes_id(self) -> bool:
        import inspect
        try:
            ps = inspect.signature(self.mailer).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(p.name == "message_id" or p.kind is inspect.Parameter.VAR_KEYWORD for p in ps)

    def _notice_once(self, key: str, subject: str, body: str, every_h: float = 12) -> bool:
        """R22: a notice goes out at most once per every_h hours, and never while KILL is set."""
        if (self.state / "KILL").exists():
            return False
        now = self.clock()
        notes = self._read("notices.json", {})
        last = notes.get(key)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < every_h * 3600:
            return False
        notes[key] = now.isoformat()  # R25: throttled from the attempt, whatever SMTP does
        self._write("notices.json", notes)
        return self._send(subject, body)

    def _deliver(self, qid: str, halt: bool = False) -> None:
        qs = self._read("questions.json", {})
        q = qs[qid]
        subject = f"[Forge Q-{qid} {q['code']}] {q['subject']}"
        if not self._send(subject, q["body"], halt=halt):
            return
        qs = self._read("questions.json", {})
        qs[qid]["delivered"] = True
        self._write("questions.json", qs)

    def _retry_halts(self) -> None:
        """R32: while KILL is set, the only thing a watchdog start does is retry an undelivered halt alert."""
        for qid, q in self._read("questions.json", {}).items():
            if q.get("halt") and q.get("delivered") is False:
                self._deliver(qid, halt=True)

    def _mail_notice(self, qid: str, subject: str, body: str) -> None:
        """Follow-up email on an existing question (keeps its reply code)."""
        q = self._read("questions.json", {}).get(qid, {})
        self._send(f"[Forge Q-{qid} {q.get('code', '')}] {subject}", body)

    def _handle_inbox(self) -> None:
        for qid, q in self._read("questions.json", {}).items():
            if q.get("delivered") is False:
                self._deliver(qid)
        try:
            messages = self.inbox() or []
        except Exception as e:  # noqa: BLE001 - email trouble never stops the conductor (R7)
            self._log(f"inbox read failed: {e!r}")
            return
        pending = self._read("inbox_pending.json", [])
        batch = (pending if isinstance(pending, list) else []) + list(messages)
        if pending:
            self._write("inbox_pending.json", [])
        own_ids = set(self._read("mail_log.json", {}).get("ids", []))
        for i, m in enumerate(batch):
            try:
                if not isinstance(m, dict):
                    continue
                if m.get("outgoing") or (m.get("message_id") and str(m["message_id"]).strip() in own_ids):
                    continue  # R18/R26: Forge's own mail is never an answer
                sender = re.findall(r"[\w.+-]+@[\w.-]+", str(m.get("from", "")).lower())
                if self.owner not in sender:
                    continue
                subject, body = str(m.get("subject", "")), clean_reply(str(m.get("body", "")))
                if is_stop(subject, body):
                    (self.state / "KILL").write_text("stopped by owner email\n")
                    rest = [dict(x, body=clean_reply(str(x.get("body", "")))) for x in batch[i + 1:]
                            if isinstance(x, dict)]
                    if rest:  # R35: kept for after the restart, never lost
                        self._write("inbox_pending.json", rest[-50:])
                    return  # R24: nothing after a STOP is processed now
                mq = re.search(r"\[Forge Q-([\w-]+) ([\w-]{8})\]", subject)
                if mq:
                    self._answer(mq.group(1), body, mq.group(2))
            except Exception as e:  # noqa: BLE001 - R35: one bad message never blocks the rest
                self._log(f"inbox message failed: {e!r}"[:500])

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
        elif q["kind"] == "merge":  # the only thing that retries a blocked merge record
            Journal(self.state).unblock(qid, body.strip()[:NOTE_CAP])
        elif q["kind"] == "replan":
            qd = self._queue()
            qd["notes"] = ([str(x)[:NOTE_CAP] for x in qd.get("notes", [])] + [f"Ben: {body.strip()}"[:NOTE_CAP]])[-NOTES_KEEP:]
            self._save_queue(qd)
            (self.state / "PAUSED").unlink(missing_ok=True)
        q["status"] = "answered"
        q["answer"] = body.strip()[:2000]
        q["closed_seq"] = self._read("q_seq.json", {}).get("n", 0)
        q["closed_at"] = self.clock().isoformat()
        self._write("questions.json", _prune_questions(qs))

    # ------------------------------------------------------------------ main step
    def step(self) -> str:
        if (self.state / "KILL").exists():  # R21: KILL stops everything, email included
            return "killed"
        self._handle_inbox()
        if (self.state / "KILL").exists():
            return "killed"
        if (self.state / "PAUSED").exists():
            return "paused"
        if self._capped():
            return "capped"
        try:
            self._reconcile_merges()
        except (RuntimeError, OSError, Rejected) as e:
            self._log(f"reconcile error: {e!r}")
            return "error"
        q = self._queue()
        if q.get("drift_due"):
            try:
                self._drift_check()
            except Capped:
                return "capped"
            except Tampered:
                return "killed"
            return "worked"
        active = {r["tid"] for r in Journal(self.state).active()}
        tid = next((t["id"] for t in q["tasks"] if t["id"] in active), None) or min(active, default=None)
        if tid is not None:  # a started finalization comes before any new work; blocked records never run
            try:
                self._ensure_worktree(q["layer"])
                self._finalizer().run(tid)
            except Capped:
                return "capped"
            except Tampered:
                return "killed"
            except (RuntimeError, OSError, Rejected) as e:  # R13; never a failed attempt
                self._log(f"finalize error on {tid}: {e!r}")
                return "error"
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
                except Capped:  # R37: the attempt was undone by its stage; retried when the cap resets
                    return "capped"
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
                and not any(v["kind"] == "gate" for v in qs.values()) \
                and not any(r.get("status") in ("active", "blocked") for r in Journal(self.state).all()):
            try:
                self._gate()
            except (RuntimeError, OSError) as e:  # e.g. a refused push: no pull request is opened
                self._log(f"gate error: {e!r}")
                return "error"
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
                        self._notice_once("error", "[Forge] the conductor keeps hitting an error",
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

    def _run_tests(self, t: dict, cwd: Path | None = None) -> tuple[int, str, bool]:
        """R1: run a task's unittest command without a shell. Returns (exit, output, timed_out)."""
        paths = parse_test_cmd(t["test_cmd"], t["test_files"])
        if paths is None:
            return 2, "unsafe test_cmd", False
        try:
            p = subprocess.run([sys.executable, "-m", "unittest", *paths], cwd=str(cwd or self.wt), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               timeout=int(self.limits.get("test_timeout_s", 600)), **NOWIN)
            return p.returncode, (p.stdout or "") + (p.stderr or ""), False
        except subprocess.TimeoutExpired:
            return 124, "test command timed out", True

    def _run_cmd(self, cmd: str, cwd: Path | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(cwd or self.wt), capture_output=True, text=True, encoding="utf-8",
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
        prompt = (role_text(self.repo, "test_writer") + "\n\nWrite only these files: " + ", ".join(t["test_files"]) +
                  ". The tests must fail until the feature exists. Do not write any other file.\n\n" +
                  self._task_prompt(t) + "\nAnswer with JSON: {\"files\": [...], \"summary\": \"...\"}")
        try:
            r = self._call("test_writer", prompt, S_TESTS)
        except Capped:
            self._reset_wt()
            raise
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
        if Journal(self.state).load(tid) is not None:  # finalization owns this task; never built again
            return
        t = self._task(tid)
        pending = t.get("troubleshoot_pending")
        if pending:  # R38: deferred troubleshooting comes before any further builder attempt
            self._troubleshoot(tid, str(pending.get("reason", "")), str(pending.get("output", "")))
            return
        cid = t["id"]
        led = self._ledger()
        if cid not in led.contracts():
            self._apply(f"create-{cid}", "create", cid, "forge-manager", {
                "title": t["title"], "spec_ref": cid, "acceptance": t["test_cmd"],
                "files_in_scope": list(t["files_in_scope"]), "max_attempts": 6, "token_budget": 10 ** 9})
        c = self._ledger().contracts().get(cid, {})
        if c.get("status") in ("claimed", "submitted"):  # a crash left the last attempt open
            c = self._recover_interrupted(tid, cid, c)
        if c.get("status") == "parked":
            self._block(tid, "ledger parked the contract (attempt or budget limit)")
            return
        tag = f"{cid}-{uuid.uuid4().hex[:8]}"
        self._apply(f"{tag}-claim", "claim", cid, "forge-executor")

        self._reset_wt()
        base = _git(self.wt, "rev-parse", "HEAD")
        twt = self.trees.prepare_task(tid, base)  # the builder works in the task's own worktree

        prompt = role_text(self.repo, "builder") + "\n\nMake the tests pass by changing only the files you may change.\n\n" + \
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
        try:
            r = self._call("builder", prompt, S_BUILD, cwd=twt)
        except Capped:  # R37: release the claim and undo, no failure recorded
            self._apply(f"{tag}-release", "release", cid, "forge-core")
            self._reset_to(twt, base)
            raise

        tests = [_norm(x) for x in t["test_files"]]
        changed = self._changed(cwd=twt)
        violations = [f for f in changed if f in tests]
        if violations:
            _git(twt, "checkout", "-q", "HEAD", "--", *violations)
            _git(twt, "clean", "-q", "-f", "--", *violations)
        changed = [f for f in changed if f not in tests]
        out_of_scope = [f for f in changed if not any(fnmatch.fnmatch(f, pat) for pat in t["files_in_scope"])]

        def fail(reason: str, sig: str, output: str = "", submitted: bool = False) -> None:
            self._reset_to(twt, base)
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

        sha = self._commit(changed, f"{cid}: {t['title']}", cwd=twt) or _git(twt, "rev-parse", "HEAD")
        self._apply(f"{tag}-report", "run_report", cid, "forge-core", {
            "run_id": tag, "claim": (r.data or {}).get("status"), "commit": sha, "changed": changed,
            "violations": violations, "out_of_scope": []})
        self._apply(f"{tag}-submit", "submit", cid, "forge-executor", {"commit": sha})

        with self.trees.throwaway(sha) as jw:  # judges run at exactly this commit, never in a shared checkout
            results = [("task tests", *self._run_tests(t, cwd=jw)[:2])]
            for cmd in self.judge_cmds:
                if results[-1][1] != 0:
                    break
                results.append((cmd, *self._run_cmd(cmd, cwd=jw)))
        for cmd, code, output in results:
            if code != 0:
                self._apply(f"{tag}-ci", "test_run", cid, "ci", {"run_id": f"{tag}-ci", "commit": sha, "passed": False})
                tail = "\n".join(output.splitlines()[-20:])
                # numbers (timings, line numbers, addresses) vary between identical failures; ignore them
                sig = hashlib.sha256(re.sub(r"\d+", "N", tail).encode()).hexdigest()
                return fail(f"judge failed: {cmd}", sig, tail, submitted=True)
        self._apply(f"{tag}-ci", "test_run", cid, "ci", {"run_id": f"{tag}-ci", "commit": sha, "passed": True})

        diff = _git(twt, "diff", f"{base}..{sha}")
        try:
            rv = self._call("reviewer", role_text(self.repo, "reviewer") + "\n\nCheck this change against the task. "
                                        "Reject shortcuts, bare-minimum work, drift from the task, and anything that "
                                        "weakens tests.\n\n" + self._task_prompt(t) + "\nDIFF:\n" + diff[:60000] +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW,
                            cwd=twt)  # the task worktree is at exactly S
        except Capped:  # R37: undo the submitted run like a failed judge, but record no failure
            self._apply(f"{tag}-capped", "fail", cid, "forge-auditor")
            if self._ledger().contracts().get(cid, {}).get("status") == "failed":
                self._apply(f"{tag}-reopen", "reopen", cid, "forge-manager")
            self._reset_to(twt, base)
            raise
        if not rv.ok:
            return fail(f"reviewer output unusable: {rv.error}", f"reviewer-error:{rv.error}", submitted=True)
        if (rv.data or {}).get("verdict") != "pass":
            reasons = [str(x) for x in (rv.data or {}).get("reasons") or ["no reasons given"]]
            self._update(tid, review_feedback=reasons)
            return fail("review failed: " + "; ".join(reasons), "review:" + "|".join(reasons), submitted=True)

        # Reviewed: hand S to the crash-safe finalizer. The ledger pass is applied only there, after the push.
        reasons = [str(x) for x in (rv.data or {}).get("reasons") or []]
        evidence = {"verdict": "pass", "reasons": reasons}
        Journal(self.state).begin(tid, cid, sha, base, f"{tag}-ci", {"verdict": "pass", "reasons": reasons}, evidence)
        self._crash("after:journal-begin")
        self._update(tid, status="merge_pending")
        self._finalizer().run(tid)

    def _reset_to(self, wt: Path, sha: str) -> None:
        _git(wt, "reset", "-q", "--hard", sha)
        _git(wt, "clean", "-q", "-fd")

    def _recover_interrupted(self, tid: str, cid: str, c: dict) -> dict:
        """A contract left claimed or submitted by a crash is released, or failed and reopened, before the next
        attempt. No failure signature: the lost attempt still counts in the ledger's attempts."""
        n = c.get("attempts", 0)
        if c.get("status") == "claimed":
            self._apply(f"{cid}-recover-{n}-release", "release", cid, "forge-core")
        else:
            self._apply(f"{cid}-recover-{n}-fail", "fail", cid, "forge-auditor")
            if self._ledger().contracts().get(cid, {}).get("status") == "failed":
                self._apply(f"{cid}-recover-{n}-reopen", "reopen", cid, "forge-manager")
        cur = self._task(tid)
        self._update(tid, notes=cur["notes"] + ["interrupted attempt recovered"])
        return self._ledger().contracts().get(cid, {})

    # ------------------------------------------------------------------ finalization (T1B3e)
    def _crash(self, point: str) -> None:
        """A named crash point. Does nothing; tests override it to simulate a process dying there."""

    def _reconcile_merges(self) -> None:
        """Every step: ledger cache from its log, journal against the queue, then stale worktrees swept."""
        led = self._ledger()
        if led.events_path.exists() or led.head_path.exists():
            led.reconcile()
        self._finalizer().reconcile(self._queue()["tasks"])
        keep = {t["id"] for t in self._queue()["tasks"] if t.get("status") in ("tests_ok", "merge_pending")}
        self.trees.sweep(keep_tasks=keep)

    def _finalizer(self) -> Finalizer:
        return Finalizer(self.wt, self._queue()["layer"], self.trees, Journal(self.state), ApprovedMerges(self.state),
                         self._hooks(), push=self.push)

    def _hooks(self) -> Hooks:
        def get_task(tid: str) -> dict | None:
            return next((t for t in self._queue()["tasks"] if t["id"] == tid), None)

        def set_task(tid: str, changes: dict) -> None:
            q = self._queue()
            for t in q["tasks"]:
                if t["id"] == tid:
                    t.update(changes)
                    self._save_queue(q, durable=True)
                    return
            raise KeyError(tid)

        def mark_drift(tid: str) -> None:  # one durable write, flushed before the journal records drift_marked
            q = self._queue()
            marks = [x for x in q.get("drift_marks") or [] if isinstance(x, str)]
            if tid not in marks:
                marks.append(tid)
            q["drift_due"], q["drift_marks"] = True, marks
            self._save_queue(q, durable=True)

        def drift_marked(tid: str) -> bool:
            return tid in (self._queue().get("drift_marks") or [])

        def question_open(qid: str) -> bool:
            return self._read("questions.json", {}).get(qid, {}).get("status") == "open"

        def find_question(kind: str, subject: str, tid: str) -> str | None:
            for qid, q in self._read("questions.json", {}).items():
                if (q.get("kind") == kind and q.get("status") == "open" and q.get("task") == tid
                        and q.get("subject") == str(subject)[:SUBJECT_CAP]):
                    return qid
            return None

        def ledger_pass(rec: dict) -> None:
            merges = [{"sha": c["sha"], "parents": list(c.get("parents") or []), "kind": c.get("kind"),
                       "run_id": c.get("run_id"), "verdict": c.get("verdict"), "reasons": list(c.get("reasons") or []),
                       "mutation": MUTATION_NA}
                      for c in rec.get("candidates") or [] if c.get("state") == "approved"]
            payload = dict(rec.get("evidence") or {})
            payload.update(run_id=rec["ci_run_id"], task_commit=rec["task_sha"], final_sha=rec["final_sha"],
                           pushed=rec["pushed"], merges=merges)
            if not self._apply(rec["pass_pid"], "pass", rec["cid"], "forge-auditor", payload) \
                    and self._ledger().completion(rec["cid"]) is None:
                raise RuntimeError(f"ledger refused the pass for {rec['cid']}")

        return Hooks(judge=self._merge_judge, review=self._merge_review,
                     ask=lambda kind, subject, body, tid: self._ask(kind, subject, body, task=tid),
                     question_open=question_open, mark_drift=mark_drift, drift_marked=drift_marked,
                     ledger_pass=ledger_pass,
                     ledger_completed=lambda cid: self._ledger().completion(cid) is not None,
                     set_task=set_task, crash=self._crash, get_task=get_task, find_question=find_question)

    def _candidate_record(self, sha: str) -> dict:
        for rec in Journal(self.state).all():
            if any(c.get("sha") == sha for c in rec.get("candidates") or []):
                return rec
        raise RuntimeError(f"no merge journal holds candidate {sha}")

    def _merge_judge(self, sha: str) -> dict:
        """A merge candidate is judged at exactly its commit: this task's tests, every done task's tests and
        every judge command. Mutation testing does not apply: a merge commit adds no builder lines."""
        rec = self._candidate_record(sha)
        tid, cid = rec["tid"], rec["cid"]
        tasks = self._queue()["tasks"]
        mine = [t for t in tasks if t["id"] == tid]
        done = [t for t in tasks if t["id"] != tid and t.get("status") == "done" and t.get("kind", "build") == "build"
                and t.get("test_cmd")]
        run_id = f"ci-merge-{tid}-{sha[:12]}"
        passed, output = True, ""
        with self.trees.throwaway(sha) as jw:
            checks = [(f"tests of {t['id']}", lambda t=t: self._run_tests(t, cwd=jw)[:2]) for t in mine + done]
            checks += [(cmd, lambda cmd=cmd: self._run_cmd(cmd, cwd=jw)) for cmd in self.judge_cmds]
            for name, run in checks:
                code, out = run()
                if code != 0:
                    passed, output = False, f"{name} failed:\n" + "\n".join(out.splitlines()[-20:])
                    break
        self._apply(run_id, "test_run", cid, "ci", {"run_id": run_id, "commit": sha, "passed": passed})
        return {"passed": passed, "run_id": run_id, "output": output, "evidence": {"mutation": MUTATION_NA}}

    def _merge_review(self, rec: dict, cand: dict) -> dict:
        t = self._task(rec["tid"])
        m = cand["sha"]
        d_base = _git(self.wt, "diff", f"{cand['base']}..{m}")[:30000]
        d_other = _git(self.wt, "diff", f"{cand['other']}..{m}")[:30000]
        judged = (f"run {cand.get('run_id')}: {'passed' if cand.get('passed') else 'failed'}\n"
                  f"{cand.get('judge_output') or ''}")
        notes = "\n".join(str(n) for n in rec.get("notes") or []) or "(none)"
        prompt = (role_text(self.repo, "reviewer") + "\n\nYou are reviewing a MERGE COMMIT created because the layer "
                  "branch moved while this task was being finalized. Check that the merge keeps both sides' work intact, "
                  "resolves nothing wrongly, drops nothing and weakens no tests.\n\n" + self._task_prompt(t) +
                  f"\nMERGE COMMIT: {m} (parents {cand['base']} and {cand['other']})\n"
                  f"\nDIFF {cand['base']}..{m} (what the merge brings to the task's side):\n{d_base}\n"
                  f"\nDIFF {cand['other']}..{m} (what the merge brings to the other side):\n{d_other}\n"
                  f"\nJUDGE RESULT:\n{judged}\n\nBEN'S NOTES:\n{notes}\n"
                  "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}")
        with self.trees.throwaway(m) as rw:
            r = self._call("reviewer", prompt, S_REVIEW, cwd=rw)
        if not r.ok:
            return {"verdict": "fail", "reasons": [f"reviewer output unusable: {r.error}"]}
        d = r.data or {}
        return {"verdict": d.get("verdict"), "reasons": [str(x) for x in d.get("reasons") or []]}

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
            try:
                self._troubleshoot(tid, reason, output)
            except Capped:  # R38: kept, and run before the next builder attempt
                self._update(tid, troubleshoot_pending={"reason": str(reason)[:NOTE_CAP],
                                                        "output": str(output)[-4000:]})
                raise

    def _troubleshoot(self, tid: str, reason: str, output: str) -> None:
        t = self._task(tid)
        self._reset_wt()
        prompt = (role_text(self.repo, "troubleshooter") + "\n\nThe builder is stuck on this task. Diagnose the cause and give "
                  "concrete notes the next builder attempt can follow. If this route is a dead end, say so and "
                  "name the alternative.\n\n" + self._task_prompt(t) +
                  "\nRECENT FAILURES:\n" + "\n".join(t["notes"][-6:]) +
                  "\n\nLAST JUDGE OUTPUT:\n" + output[-4000:] +
                  "\nAnswer with JSON: {\"kind\": \"fix\" | \"dead_end\", \"notes\": \"...\", \"alternative\": \"...\"}")
        with self.trees.throwaway(_git(self.wt, "rev-parse", "HEAD")) as tw:  # scratch: its edits are discarded
            r = self._call("troubleshooter", prompt, S_TROUBLE, cwd=tw)
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
                     troubleshoots=t.get("troubleshoots", 0) + 1, troubleshoot_pending=None)

    # ------------------------------------------------------------------ drift
    def _drift_check(self) -> None:
        q = self._queue()
        design = self.wt / "docs" / "specs" / "layer-1-design.md"
        text = design.read_text(encoding="utf-8") if design.exists() else "(no design file)"
        listing = "\n".join(f"- {t['id']} [{t['status']}] {t['title']}" for t in q["tasks"])
        r = self._call("drift_keeper", role_text(self.repo, "drift_keeper") + "\n\nIs this work still on course for "
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
        try:
            r = self._call("planner", role_text(self.repo, "planner") + "\n\nWrite the implementation plan to " + plan_file +
                           " (and no other file), then return its tasks.\n\n" + self._task_prompt(t) +
                           "\nEach task needs: id, title, section, files_in_scope, test_files, test_cmd.\n"
                           "Answer with JSON: {\"tasks\": [...]}", S_PLAN)
        except Capped:
            self._reset_wt()
            raise
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
            try:
                rv = self._call("reviewer", role_text(self.repo, "reviewer") + "\n\nCheck this plan against the task and "
                                            "design: complete, testable, no placeholders, no drift.\n\n" +
                                self._task_prompt(t) + "\nPLAN:\n" + plan_text[:60000] + "\nTASKS JSON:\n" +
                                json.dumps(tasks)[:20000] +
                                "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}",
                                S_REVIEW)
            except Capped:
                self._reset_wt()
                raise
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
                self._send(f"[Forge] {q['layer']} is done but the pull request failed",
                           f"GitHub said:\n{(out or '')[:2000]}\n\nForge keeps retrying.")
            return
        pr = m.group(1)
        self._ask("gate", f"{q['layer']} is ready: reply y to approve",
                  report + f"\n\nPull request: {out.strip()}\n\nReply y to approve and merge into main.", pr=pr)


# ---------------------------------------------------------------------- real I/O
def gmail_mailer(owner: str) -> Callable[..., None]:
    def send(subject: str, body: str, message_id: str | None = None) -> None:
        import keyring
        import smtplib
        from email.message import EmailMessage
        pw = keyring.get_password("forge-gmail", owner)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = owner, owner, subject
        msg["X-Forge-Outgoing"] = "1"  # R18: lets the inbox reader skip Forge's own mail
        if message_id:
            msg["Message-ID"] = message_id  # R26: recorded, so Forge also knows its own mail by id
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(owner, pw)
            s.send_message(msg)
    return send


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def gmail_inbox(owner: str, state: Path, imap_factory: Callable | None = None,
                clock: Callable[[], datetime] | None = None) -> Callable[[], list[dict]]:
    """R26: peek-only reader. Never changes Ben's read flags; remembers handled Message-IDs in inbox_seen.json;
    the first read only records what is already there."""
    def read() -> list[dict]:
        import email
        import imaplib
        from email.header import decode_header, make_header
        seen_path = Path(state) / "inbox_seen.json"
        first = not seen_path.exists()
        try:
            seen = list(json.loads(seen_path.read_text(encoding="utf-8"))) if not first else []
        except (OSError, ValueError):
            seen = []
        seen_set = set(seen)
        if imap_factory is None:
            import keyring
            pw = keyring.get_password("forge-gmail", owner)
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        else:
            pw = ""
            m = imap_factory()
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        since = now.fromordinal(now.toordinal() - 3)
        day = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
        out: list[dict] = []
        try:
            m.login(owner, pw)
            m.select("INBOX")
            nums: list[bytes] = []
            for crit in (f'(SINCE {day} SUBJECT "[Forge")', f'(SINCE {day} SUBJECT "STOP")'):
                _, data = m.search(None, crit)
                for n in (data[0].split() if data and data[0] else []):
                    if n not in nums:
                        nums.append(n)
            for n in nums:
                try:  # R36: each message on its own; one bad email never blocks the rest
                    _, raw = m.fetch(n, "(BODY.PEEK[HEADER])")
                    head_bytes = _raw_bytes(raw)
                    if not head_bytes:
                        continue  # transport trouble: retried on the next read
                    head = email.message_from_bytes(head_bytes)
                    mid = str(head.get("Message-ID", "")).strip() or "nomid:" + hashlib.sha256(
                        head_bytes).hexdigest()[:24]
                except Exception:  # noqa: BLE001 - transport trouble: retried on the next read
                    continue
                if mid in seen_set:
                    continue
                if first or str(head.get("X-Forge-Outgoing", "")).strip() == "1":
                    seen.append(mid)
                    seen_set.add(mid)
                    continue
                try:
                    _, raw = m.fetch(n, "(BODY.PEEK[])")
                    body_bytes = _raw_bytes(raw)
                except Exception:  # noqa: BLE001 - transport trouble: not marked seen, retried next read
                    continue
                if not body_bytes:
                    continue
                seen.append(mid)  # fetched: from here on it is handled, delivered or skipped as malformed
                seen_set.add(mid)
                try:
                    msg = email.message_from_bytes(body_bytes)
                    body = ""
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True) or b""
                            try:
                                body = payload.decode(part.get_content_charset() or "utf-8", "replace")
                            except LookupError:  # unknown charset
                                body = payload.decode("utf-8", "replace")
                            break
                    try:
                        subj = str(make_header(decode_header(msg.get("Subject", ""))))
                    except (LookupError, UnicodeError, ValueError):
                        subj = str(msg.get("Subject", ""))
                    out.append({"from": str(msg.get("From", "")), "subject": subj, "body": body,
                                "message_id": mid, "outgoing": False})
                except Exception:  # noqa: BLE001 - malformed: skipped for good
                    continue
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        seen_path.write_text(json.dumps(seen[-2000:]), encoding="utf-8")
        return out
    return read


def _raw_bytes(fetched) -> bytes:
    for part in fetched or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return b""


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


LOCK_NAME = "conductor.lock"


def acquire_lock(state: Path):
    """R11: OS-level exclusive lock held for the life of the process. Returns a handle, or None if taken."""
    state.mkdir(parents=True, exist_ok=True)
    f = open(state / LOCK_NAME, "a+")
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
    f.truncate(0)  # R15: the lock file always holds zero bytes, so there is no content to hide
    f.flush()
    return f


SMOKE_ROLES = {  # role: (schema, writes a file?, example answer)
    "test_writer": (S_TESTS, True, '{"files": ["smoke.txt"], "summary": "ok"}'),
    "builder": (S_BUILD, True, '{"status": "done", "summary": "ok"}'),
    "reviewer": (S_REVIEW, False, '{"verdict": "pass", "reasons": []}'),
    "troubleshooter": (S_TROUBLE, None, '{"kind": "fix", "notes": "ok"}'),
    "drift_keeper": (S_DRIFT, False, '{"status": "ok", "reasons": []}'),
    "planner": (S_PLAN, True, '{"tasks": []}'),
}


def smoke(team: Team, workdir: Path, call: Callable | None = None,
          warn: Callable[[str], None] | None = None) -> list[str]:
    """R23/R29: run every role once for real, each in a fresh throwaway repo. Returns problems; empty = passed.
    call(role, prompt, schema, cwd) runs the agent; main passes the conductor's guarded _call."""
    import shutil
    import stat
    from core.agents import schema_ok
    call = call or (lambda role, prompt, schema, cwd: getattr(team, role).run(prompt, cwd, schema))
    Path(workdir).mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    def files(root: Path) -> dict[str, str]:
        found = {}
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for d in dirnames:
                found[(Path(dirpath) / d).relative_to(root).as_posix() + "/"] = "dir"
            for nm in names:
                f = Path(dirpath) / nm
                found[f.relative_to(root).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
        return found

    def unlock(fn, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        fn(path)

    def rm(root: Path) -> None:
        """R39: an agent's child process can keep the folder busy for a moment; retry, then leave it."""
        for attempt in range(5):
            try:
                shutil.rmtree(root, onerror=unlock)
                return
            except OSError as e:
                if attempt == 4:
                    if warn:
                        warn(f"smoke folder left for the next sweep: {root}: {e}"[:500])
                    return
                time.sleep(2)

    for old in Path(workdir).glob("forge-smoke-*"):  # R39: sweep leftovers from earlier runs (best effort)
        if old.is_dir():
            try:
                shutil.rmtree(old, onerror=unlock)
            except OSError:
                pass

    for role, (schema, writes, example) in SMOKE_ROLES.items():
        # Plain mkdir, not mkdtemp: on Windows mkdtemp locks the folder to this user, and Codex's sandbox runs as
        # a separate user, so files it wrote there could not be read back.
        root = Path(workdir) / f"forge-smoke-{role}-{uuid.uuid4().hex[:8]}"
        root.mkdir()
        try:
            _git(root, "init", "-q")
            _git(root, "config", "user.name", "Forge smoke")
            _git(root, "config", "user.email", "forge@localhost")
            (root / "README.md").write_bytes(b"Forge smoke test repo\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "smoke")
            head, snap = _git(root, "rev-parse", "HEAD"), files(root)
            if writes:
                ask = "Create a file named smoke.txt containing the word ok in the current folder. Change nothing else."
            elif writes is False:
                ask = "Do not create or change any files."
            else:
                ask = "You may read files but do not need to change any."
            prompt = f"SMOKE TEST for Forge (role: {role}). {ask} Then answer with ONLY this JSON: {example}"
            r = call(role, prompt, schema, root)
            if not r.ok:
                problems.append(f"{role}: failed: {r.error}"[:500])
            elif not schema_ok(r.data, schema):
                problems.append(f"{role}: answer does not match its schema: {json.dumps(r.data)[:200]}")
            if _git(root, "rev-parse", "HEAD") != head:
                problems.append(f"{role}: made a git commit (not allowed)")
            now = files(root)
            if writes:
                f = root / "smoke.txt"
                if not f.is_file() or "ok" not in f.read_text(encoding="utf-8", errors="replace").lower():
                    problems.append(f"{role}: could not write smoke.txt containing ok (no write access?)")
                extra = sorted(set(now) - set(snap) - {"smoke.txt"}) + sorted(
                    k for k in snap if now.get(k) != snap[k])
                if extra:
                    problems.append(f"{role}: changed more than smoke.txt: {extra[:5]}")
            elif writes is False and now != snap:
                problems.append(f"{role}: changed files but must be read-only")
        except (RuntimeError, OSError) as e:  # git and file errors are problems, never a pass or a crash
            problems.append(f"{role}: {type(e).__name__}: {e}"[:500])
        finally:
            rm(root)
    return problems


def main(argv: list[str]) -> int:
    from core.agents import load_limits
    forge = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "run", "step", "status", "smoke"])
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
    lock = acquire_lock(state)  # R15: every command that can launch agents holds the lock
    if a.cmd == "step":
        if lock is None:
            print("busy")
            return 0
        try:
            print(c.step())
        finally:
            lock.close()
        return 0
    if lock is None:
        return 0  # another conductor holds the lock; the watchdog calls us harmlessly
    try:
        if a.cmd == "run":
            if (c.state / "KILL").exists():  # R32: while stopped, only retry a pending halt alert
                c._retry_halts()
                return 0
            c._handle_inbox()  # R31: a STOP is honoured before anything is launched
            if (c.state / "KILL").exists():
                return 0
            while (c.state / "PAUSED").exists():  # R40: while paused, only wait for Ben's answer
                (c.state / "conductor.heartbeat").write_text(f"{os.getpid()} {time.time()}")
                time.sleep(60)
                if (c.state / "KILL").exists():
                    return 0
                c._handle_inbox()
                if (c.state / "KILL").exists():
                    return 0
        if a.cmd == "smoke" or _smoke_stale(c.state, c.clock()):
            problems = _guarded_smoke(c, Path(a.work), force=a.cmd == "smoke")
            if a.cmd == "smoke":
                print("\n".join(problems) or "smoke test passed")
                return 1 if problems else 0
            if problems:
                c._log("smoke test failed: " + " | ".join(problems))
                c._notice_once("smoke", "[Forge] not started: the live smoke test failed",
                               "Before starting, Forge runs every agent once for real. These failed:\n\n" +
                               "\n".join(problems) + "\n\nForge retries every few minutes.")
                return 0
        c._notice_once("start", "[Forge] conductor started",
                       "The conductor is running in the background. You'll hear from it only when something "
                       "needs you, when a layer is ready for approval, or if it hits trouble.\n\n"
                       "To stop everything: reply STOP to any Forge email.")
        print(c.run(heartbeat=c.state / "conductor.heartbeat"))
    finally:
        lock.close()
    return 0


def _smoke_stale(state: Path, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(json.loads((state / "smoke_ok.json").read_text(encoding="utf-8"))["at"])
        return (now - last).total_seconds() > 86400
    except (OSError, ValueError, KeyError, TypeError):
        return True


def _guarded_smoke(c: Conductor, workdir: Path, force: bool = False) -> list[str]:
    """R29: the smoke test through the conductor's guarded _call (fail-closed checks, tamper guard, metering),
    with a 30-minute wait after a failure and a success stamp only after a full pass."""
    fail = c._read("smoke_fail.json", {})
    if not force and fail.get("at") and (c.clock() - datetime.fromisoformat(fail["at"])).total_seconds() < 1800:
        return ["the last smoke test failed; waiting 30 minutes before retrying"]
    (c.state / "smoke_ok.json").unlink(missing_ok=True)
    if c._capped():
        problems = ["token cap reached; smoke test not run"]
    else:
        try:
            problems = smoke(c.team, workdir, lambda role, prompt, schema, cwd: c._call(role, prompt, schema, cwd=cwd),
                             warn=c._log)
        except Capped as e:  # R37
            problems = [f"token cap reached during the smoke test ({e})"]
        except Tampered as e:
            problems = [f"state files changed during the smoke test (Forge halted): {e}"[:500]]
        except RuntimeError as e:  # fail-closed preconditions (R15)
            problems = [str(e)[:500]]
    if problems:
        c._write("smoke_fail.json", {"at": c.clock().isoformat(), "problems": problems})
    else:
        (c.state / "smoke_fail.json").unlink(missing_ok=True)
        c._write("smoke_ok.json", {"at": c.clock().isoformat()})
    return problems


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
