"""Bootstrap conductor: runs a queue of tasks through the D-025 team with no human relay.

Spec: docs/specs/bootstrap-conductor.md. Plain code only; AI is reached solely through
the Team's agents (core.agents interface). Ben is reached only by email.

    python -m core.bootstrap init --layer layer-1 --tasks tasks.json
    python -m core.bootstrap run
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import inspect
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

from core import channel
from core import coverage as cov_mod
from core import drift as drift_mod
from core import manager as manager_mod
from core import lanes as lanes_mod
from core import readiness, service
from core.finalize import ApprovedMerges, Finalizer, Hooks, Journal, safe_push
from core.ledger import Ledger, Rejected
from core.mutation import changed_lines, run_mutation
from core.roles import role_text
from core.usage import Meter, limit_hold_until
from core.weaktest import empty_implementation, real_failing_run, stub_targets
from core.worktrees import Worktrees

ROLES = {"ci": "ci", "forge-manager": "manager", "forge-executor": "executor",
         "forge-auditor": "auditor", "forge-core": "core", "benjamin": "human"}
NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
TASK_FIELDS = ("id", "title", "section", "files_in_scope", "test_files", "test_cmd")
NOTE_CAP, NOTES_KEEP, BODY_CAP = 2000, 30, 20000  # R19
MIN_SECTION_CHARS = 600  # R41: a task's section is its builder's only instructions
PLAN_REVIEW_MAX = 200_000  # R44: the plan reviewer sees the whole plan, up to this size
JUDGE_TIMEOUT_S = 2400  # R55: a judge command (drills, the parallel suite) may run this long
PLAN_ATTEMPTS = 3  # R45: a plan task is blocked after this many rejections
PLAN_MEMORY_NOTES, PLAN_MEMORY_CHARS = 10, 12000  # R47
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
                "error": _STR, "capability": _STR, "meanwhile": _STR}, ["status"])
S_REVIEW = _obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": _STRS}, ["verdict", "reasons"])
S_PLAN_REVIEW = _obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": _STRS,
                      "task_notes": {"type": "array", "items": _obj({"task": _STR, "note": _STR}, ["task", "note"])}},
                     ["verdict", "reasons"])  # R44
S_TROUBLE = _obj({"kind": {"type": "string", "enum": ["fix", "dead_end", "suggestion"]}, "notes": _STR,
                  "alternative": _STR}, ["kind", "notes"])
S_DRIFT = _obj({"status": {"type": "string", "enum": ["ok", "replan"]}, "reasons": _STRS}, ["status"])
S_PLAN = _obj({"tasks": {"type": "array", "items": _obj(
    {"id": _STR, "title": _STR, "section": _STR, "files_in_scope": _STRS, "test_files": _STRS, "test_cmd": _STR,
     "needs": {"type": "array", "items": {"type": "string"}},
     "covers": {"type": "array", "items": {"type": "string"}},
     "evidence": {"type": "boolean"},
     "depends_on": {"type": "array", "items": {"type": "string"}}},
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


def run_tree(args, cwd: Path, timeout_s: float, *, env: dict | None = None,
             should_stop: Callable[[], bool] | None = None, poll_s: float = 2.0) -> tuple[int, str, str]:
    """Live-run P1: run a test or judge command the way core.agents.launch runs an agent: its own process group
    (POSIX) or tree (Windows), killed as a whole on timeout or stop, and output to a temp file rather than a pipe, so
    a grandchild that keeps stdout open can never make us wait. should_stop is polled every poll_s seconds.
    Returns (exit code, output, why) with why "" (finished), "timeout" or "stopped"."""
    import tempfile
    from core import agents
    kw: dict = dict(NOWIN) if os.name == "nt" else {"start_new_session": True}
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=env, **kw)
        with agents._LIVE_LOCK:  # a stall exit (core.service) kills it with the agents
            agents._LIVE.add(p)
        why = ""
        try:
            deadline = time.monotonic() + float(timeout_s)
            while True:
                left = deadline - time.monotonic()
                try:
                    p.wait(timeout=max(0.01, min(left, poll_s)))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        why = "timeout"
                    elif should_stop is not None and should_stop():
                        why = "stopped"
                    if why:
                        agents._kill_tree(p)
                        try:
                            p.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass
                        break
            if os.name != "nt":  # the command is done: nothing it started may outlive it
                try:
                    os.killpg(p.pid, 9)
                except OSError:
                    pass
        finally:
            with agents._LIVE_LOCK:
                agents._LIVE.discard(p)
        out.seek(0)
        text = out.read().decode("utf-8", "replace")
    return (p.returncode if p.returncode is not None else -9), text, why


_SUITE_CMD = re.compile(r"(^|\s)-m\s+core\.suite(\s|$)")
_TASK_TEST = re.compile(r"tests/core/test_\w+\.py")
MAIN_SYNC_EVERY_S = 600  # R58: origin is fetched for the main sync at most this often


def task_judge_cmds(cmds: list[str], base: str, sha: str, test_files=(), exclude=()) -> list[str]:
    """R57: a per-task judge runs the suite only for what base..sha can affect. Every `-m core.suite` command
    gets --changed <base>..<sha> and --include for each of the task's own tests/core test files; other judge
    commands (the drills) are unchanged. The layer gate and CI run the commands as written: the full suite."""
    out = []
    for cmd in cmds:
        if _SUITE_CMD.search(cmd) and "--changed" not in cmd:
            cmd = f"{cmd} --changed {base}..{sha}"
            for f in test_files or ():
                f = str(f).replace("\\", "/")
                if _TASK_TEST.fullmatch(f):
                    cmd += f" --include {f}"
        if _SUITE_CMD.search(cmd):
            mine = {str(f).replace("\\", "/") for f in test_files or ()}
            for f in sorted({str(x).replace("\\", "/") for x in exclude or ()} - mine):
                if _TASK_TEST.fullmatch(f):  # R59: tests of layer tasks not built yet
                    cmd += f" --exclude {f}"
        out.append(cmd)
    return out


def _cmd_args(cmd: str):
    """A judge command string without a shell: POSIX splits it like a shell would (quotes only); Windows hands the
    command line to CreateProcess as written."""
    import shlex
    return cmd if os.name == "nt" else shlex.split(cmd)


class Tampered(Exception):
    """An agent run changed the conductor's own state files (R9)."""


class Capped(Exception):
    """R37: the agent's provider is at its daily token cap; nothing was launched."""


class NotReady(Exception):
    """D-030: a capability the agent needs lacks usable evidence; nothing was written or launched."""

    def __init__(self, names: dict[str, str]):
        self.names = dict(names)
        super().__init__("; ".join(f"{n}: {why}" for n, why in sorted(self.names.items())))


class Stopped(Capped):
    """R42/R49: Ben stopped Forge. KILL or PAUSED was set before a launch (nothing is launched), appeared while an
    agent ran (its process tree is killed), or was the only state change during the run. A Capped, so every stage
    undoes the attempt exactly as for a cap (R37): never a failure, never tampering. step() reports it via _held()."""


STOP_FILES = ("KILL", "PAUSED")
SHARED_PREFIX = "shared/"  # R60: fingerprint keys of files in state/shared
DROP_PREFIX = "channel/in/"  # fingerprint keys of the answer drop folder (never a state/ path: state has no channel/)
STOP_MARK = "agent stopped:"  # R49: core.agents.Stopped's message; an agent killed by a stop flag reports it


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


def _covers_problem(tasks: list[dict], reqs: dict) -> str | None:
    """R62: every planned task names known, non-repeated requirement ids it covers."""
    for x in tasks:
        cov = x.get("covers")
        who = x.get("id")
        if cov is None:
            return f"plan rejected: covers missing for task {who} (required: non-empty list of requirement ids)"
        if not isinstance(cov, list) or not all(isinstance(c, str) for c in cov):
            return f"plan rejected: covers for task {who} must be a list of requirement ids"
        if not cov:
            return f"plan rejected: covers empty for task {who} (required: non-empty)"
        bad = [c for c in cov if c not in reqs]
        if bad:
            return f"plan rejected: covers unknown requirements for task {who}: {', '.join(bad)}"
        if len(set(cov)) != len(cov):
            return f"plan rejected: covers repeats a requirement for task {who}"
    return None


def validate_task(t: dict) -> str | None:
    """R1: returns a problem description, or None if the task is safe to run."""
    if not isinstance(t.get("id"), str) or not ID_RE.match(t["id"]):
        return f"bad id {t.get('id')!r}"
    if "needs" in t:  # D-030: optional capability names the builder needs
        nd = t["needs"]
        if not isinstance(nd, list) or len(nd) > 10 or not all(
                isinstance(x, str) and readiness.NAME_RE.match(x) for x in nd):
            return "bad needs"
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



def replace_retry(src: Path, dst: Path, tries: int = 6) -> None:
    """os.replace, retried briefly on Windows' sharing violation: a reader (the status page, the live dashboard
    (R64), the tray) that has the target open for a moment makes the rename fail with PermissionError."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.02 * (i + 1))

class Conductor:
    def __init__(self, repo: Path, work: Path, state: Path, team: Team, limits: dict, *,
                 owner_email: str, mailer: Callable[[str, str], None], inbox: Callable[[], list[dict]],
                 gh: Callable[[list[str]], tuple[int, str]], clock: Callable[[], datetime] | None = None,
                 judge_cmds: list[str] | None = None, push: bool = True,
                 checks: dict | None = None, probes: dict | None = None, manager=None,
                 shared: Path | None = None, lane: str = lanes_mod.MAIN):
        self.repo, self.work, self.state = Path(repo), Path(work), Path(state)
        # R60 lanes: `shared` is state/shared (meter, holds, mail log, inbox_seen, the global KILL), shared by every
        # lane. None keeps the single-conductor layout where all of those live in `state`.
        self.shared = Path(shared) if shared is not None else None
        self.lane = lane
        self.team, self.limits = team, limits
        self.owner = owner_email.strip().lower()
        self.mailer, self.inbox, self.gh = mailer, inbox, gh
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.judge_cmds = list(judge_cmds or [])
        self.push = push
        self.state.mkdir(parents=True, exist_ok=True)
        if self.shared is None:
            self.meter = Meter(self.state, clock=self.clock)
        else:
            self.shared.mkdir(parents=True, exist_ok=True)
            self.shared_lock = lanes_mod.SharedLock(self.shared)
            root = self.shared.parent
            lanes_mod.migrate(root)  # R60: once; afterwards a missing shared meter is an error, never a reset
            self.meter = Meter(self.shared, clock=self.clock, lock=self.shared_lock,
                               runs_dirs=lambda: lanes_mod.runs_dirs(root), lane=lane)
            self.mail_files = lanes_mod.OwnFiles(self.shared, "mail", lane, self.shared_lock)
            if lane != lanes_mod.MAIN:  # each lane has its own answer drop folder (never state/lanes/channel)
                self.channel_dir = str(lanes_mod.channel_dir(root, lane))
        # D-030: readiness always runs; None means the real checks and AI probes (there is no "off" mode).
        self.checks = dict(real_checks() if checks is None else checks)
        self.probes = dict(real_probes(limits) if probes is None else probes)
        self.diagnose_probe = None  # None: readiness' own hidden probe (docker ps for n8n); tests inject fakes
        self.diagnose_which = None
        self.manager = manager  # T1C5: the read-only Manager; None keeps the pause-and-ask re-plan
        self.last_run_s = 0.0  # T1C4: duration of the last guarded agent run, on the conductor clock
        self.last_agent_commit: list[str] | None = None  # Live-run P1: files of an agent commit that was undone

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
        if name == "questions.json":
            self._write_ben_queue(data)  # 1E: queue.jsonl, the design's view of the open questions
        if not durable:
            tmp.write_bytes(raw)
            replace_retry(tmp, p)
            return
        with open(tmp, "wb") as f:  # durable: the bytes reach the disk before the rename, the rename after it
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        replace_retry(tmp, p)
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

    # ------------------------------------------------------------------ R60 lanes: shared files and stops
    def _mail_log(self) -> dict:
        """The mail log: this conductor's mail_log.json, or with lanes every lane's mail/<lane>.json merged (sends
        and Message-IDs of all lanes; {} when the shared accounting can't be read: step() refuses work then)."""
        if self.shared is None:
            return self._read("mail_log.json", {})
        try:
            files = self.mail_files.read_all()
        except lanes_mod.AccountingError:
            return {"sent": [], "ids": []}
        return {"sent": [x for d in files.values() for x in d.get("sent", [])],
                "ids": [x for d in files.values() for x in d.get("ids", [])]}

    def _accounting_check(self) -> str | None:
        """R60: before any work is admitted, the shared accounting must be readable and this lane's own files must be
        exactly what it last wrote. Returns None when fine, else the step status: "killed" (tampering: KILL for
        every lane and a halt question) or "not_ready" (unreadable or missing: nothing runs; Ben is asked once)."""
        if self.shared is None:
            return None
        try:
            self.meter.verify()
            self.mail_files.verify()
            problem = lanes_mod.accounting_problem(self.shared)
        except lanes_mod.AccountingTampered as e:
            self._tamper_alarm("accounting check", "between runs", [str(e)])
            return "killed"
        except lanes_mod.AccountingError as e:
            problem = str(e)
        if problem is None:
            return None
        self._log(f"shared accounting can't be trusted; nothing is launched: {problem}"[:500])
        qs = self._read("questions.json", {})
        if not any(isinstance(q, dict) and q.get("kind") == "accounting" and q.get("status") == "open"
                   for q in qs.values()):
            self._ask("accounting", "Forge paused: its usage accounting can't be read",
                      f"Forge's shared usage accounting (state/shared) can't be trusted:\n\n{problem}\n\n"
                      "Nothing is launched until it is fixed, because the token, run and mail caps can't be "
                      "checked. Only you can fix this: restore the file from a backup, or tell the builder what "
                      "happened. Reply to this email when it's done.", halt=True)
        return "not_ready"

    def _mail_reader_only(self) -> bool:
        """R60: main is stopped by its own KILL alone (not the global one) while other lanes exist. Main still reads
        Ben's mailbox then, because it is the only reader: a STOP must still stop every lane, and answers to other
        lanes' questions must still reach them. Nothing else runs."""
        return (self.lane == lanes_mod.MAIN and self.shared is not None and (self.state / "KILL").exists()
                and not (self.shared / "KILL").exists() and len(lanes_mod.listed(self.shared.parent)) > 1)

    def _loop_must_end(self) -> bool:
        """The run loop (and the service's sleep) ends on KILL, unless this conductor is main's mailbox reader."""
        return self._kill_set() and not self._mail_reader_only()

    def _mailbox_only(self) -> None:
        """R60: main's mailbox while main itself is stopped. A STOP sets the global KILL; a reply to another lane's
        question is routed to that lane; a reply to one of main's own questions waits in inbox_pending.json for
        main's restart (R35). Nothing is sent and no question of main's is answered while main is stopped."""
        try:
            messages = self.inbox() or []
        except Exception as e:  # noqa: BLE001 - email trouble never stops the conductor (R7)
            self._log(f"inbox read failed: {e!r}")
            return
        names = lanes_mod.listed(self.shared.parent)
        own_ids = set(self._mail_log().get("ids", []))
        keep = []
        for i, m in enumerate(messages):
            try:
                if not isinstance(m, dict):
                    continue
                if m.get("outgoing") or (m.get("message_id") and str(m["message_id"]).strip() in own_ids):
                    continue
                if not self._from_owner(m.get("from", "")):
                    continue
                subject, body = str(m.get("subject", "")), clean_reply(str(m.get("body", "")))
                if is_stop(subject, body):
                    self._stop_all("stopped by owner email\n")
                    keep += [dict(x, body=clean_reply(str(x.get("body", "")))) for x in messages[i + 1:]
                             if isinstance(x, dict)]
                    break
                mq = re.search(r"\[Forge Q-([\w-]+) ([\w-]{8})\]", subject)
                lane = lanes_mod.qid_lane(mq.group(1), names) if mq else None
                if lane:
                    lanes_mod.route(self.state, lane, {"from": m.get("from", ""), "subject": subject, "body": body,
                                                       "message_id": m.get("message_id", "")})
                else:
                    keep.append(dict(m, body=body))
            except Exception as e:  # noqa: BLE001 - R35: one bad message never blocks the rest
                self._log(f"inbox message failed: {e!r}"[:500])
        if keep:
            pending = self._read("inbox_pending.json", [])
            self._write("inbox_pending.json", ((pending if isinstance(pending, list) else []) + keep)[-50:])

    def _kill_set(self) -> bool:
        """KILL in this lane's state, or the global KILL in state/shared that stops every lane."""
        return (self.state / "KILL").exists() or (self.shared is not None and (self.shared / "KILL").exists())

    def _stop_all(self, reason: str) -> None:
        """Ben's STOP (email or the page): this conductor's KILL, and with lanes the global KILL that stops all."""
        (self.state / "KILL").write_text(reason)
        if self.shared is not None:
            (self.shared / "KILL").write_text(reason)

    def _stop_keys(self) -> tuple:
        return STOP_FILES + ((SHARED_PREFIX + "KILL",) if self.shared is not None else ())

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
        prefix = "forge-task/" if self.lane == lanes_mod.MAIN else f"forge-lane/{self.lane}/"  # R60: per lane
        return Worktrees(self.repo, self.work, branch_prefix=prefix)

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
        t.setdefault("needs", [])
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
    def _call(self, role: str, prompt: str, schema: dict | None, cwd: Path | None = None, needs=None,
              timeout_s: float | None = None, scratch: bool = False):
        self._raise_if_stopped()  # R42/R49: before anything else, including readiness refreshes
        agent = self._agent(role)
        provider = getattr(agent, "provider", None)
        if provider and self.meter.over(provider, self.limits):  # R37/R48/R50/R51: caps, holds, runs per day
            raise Capped(provider)
        self._launch_gate(provider, needs)
        prompt = prompt + self._prompt_blocks(role)
        return self._guarded_run(role, agent, prompt, cwd, schema, timeout_s=timeout_s, scratch=scratch)

    def _agent(self, role: str):
        """T1C5: the Manager is a separate, optional member (Team keeps its fixed six roles)."""
        return getattr(self, "manager", None) if role == "manager" else getattr(self.team, role)

    @staticmethod
    def _takes_timeout(agent) -> bool:
        try:
            return "timeout_s" in inspect.signature(agent.run).parameters
        except (TypeError, ValueError):
            return False

    def _activity(self) -> drift_mod.Activity:
        return drift_mod.Activity(self.state)

    def _since(self, t0: datetime) -> float:
        try:
            return max(0.0, (self.clock() - t0).total_seconds())
        except (TypeError, OverflowError):
            return 0.0

    def _guarded_run(self, label: str, agent, prompt: str, cwd: Path | None, schema: dict | None,
                     timeout_s: float | None = None, scratch: bool = False):
        """R14/R15 guarded agent run: run record, fingerprint before and after, KILL on tamper, metering."""
        role = label
        provider = getattr(agent, "provider", None)
        self._raise_if_stopped()  # R42/R49: checked before every launch, probes included
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + role + "-" + uuid.uuid4().hex[:6]
        d = self.state / "runs" / run_id
        self._admit(provider, d)  # R60: check-and-reserve, atomic across lanes
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
        try:  # R60: other lanes' accounting files are checked for loss instead of fingerprinted
            acc_before = lanes_mod.accumulators(self.shared) if self.shared is not None else None
        except OSError as e:
            raise RuntimeError(f"shared state could not be read before agent run: {type(e).__name__}: {e}") from e
        if acc_before is not None and lanes_mod.unreadable(acc_before):  # R15/R60: fail closed
            raise lanes_mod.AccountingError("shared accounting unreadable before agent run: "
                                            + ", ".join(lanes_mod.unreadable(acc_before)))
        # T1C4: a deadline (the builder's remaining focus time) is passed to agents that accept one; the run's
        # duration on the conductor clock is recorded as active time (never for readiness probes).
        extra = {"timeout_s": timeout_s} if timeout_s is not None and self._takes_timeout(agent) else {}
        if hasattr(agent, "should_stop"):  # R42/R49: a real agent's process tree is killed when a stop flag appears
            agent.should_stop = self._stop_requested
        # Live-run P1: agents may never commit. The layer worktree and the run's own worktree are checked; a
        # scratch worktree (the troubleshooter's throwaway, whose edits are discarded anyway) is only put back.
        layer_wt = self._layer_wt()
        run_wt = Path(cwd) if cwd else layer_wt
        heads = self._heads(*([layer_wt] if layer_wt else []), *([run_wt] if run_wt and not scratch else []))
        scratch_heads = self._heads(run_wt) if scratch and run_wt and run_wt != layer_wt else {}
        self.last_agent_commit = None
        t0 = self.clock()
        try:
            r = agent.run(prompt, Path(cwd) if cwd else self.wt, schema, **extra)
        except Exception as e:  # noqa: BLE001 - an agent crash is a failed result
            from core.agents import AgentResult
            r = AgentResult("", 0, False, repr(e), None, getattr(agent, "provider", "unknown"))
        self.last_run_s = self._since(t0)
        committed = self._undo_agent_commits(heads)  # before anything can raise: no stage ever adopts that HEAD
        self._undo_agent_commits(scratch_heads)
        if committed is not None:
            from core.agents import AgentResult
            self.last_agent_commit = committed
            r = AgentResult(r.text, r.tokens, False, "agent commit rejected (agents may not commit; reset to base): "
                            + (", ".join(committed) or "no files"), None, r.provider)
        try:
            after = self._fingerprint()
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            if acc_before is not None:  # R60: the meter (and the other accumulate files) may never decrease
                changed += lanes_mod.shrunk(acc_before, lanes_mod.accumulators(self.shared), self.clock())
        except Exception as e:  # noqa: BLE001 - R15: a check that can't complete counts as tampering
            changed = [f"state could not be fingerprinted after the run: {type(e).__name__}: {e}"]
        if committed is not None:  # logged only after the after-run fingerprint (the log is a state file)
            self._log(f"{role} run {run_id} made a commit; reset to base. Files: {', '.join(committed)}"[:500])
        if changed and all(k in self._stop_keys() and k not in before for k in changed):  # R42: a stop
            if r.tokens:
                self.meter.add(r.provider or "unknown", r.tokens)
            self._log(f"stop requested during {role} run {run_id} ({', '.join(changed)}); run discarded")
            raise Stopped(", ".join(changed))
        if changed:
            self._tamper_alarm(role, run_id, changed)
            raise Tampered(", ".join(changed))
        if r.tokens:
            self.meter.add(r.provider or "unknown", r.tokens)
        (d / "output.json").write_bytes(json.dumps({"ok": r.ok, "error": r.error, "text": r.text, "data": r.data,
                                                    "tokens": r.tokens, "provider": r.provider},
                                                   indent=2).encode("utf-8"))
        if not r.ok and STOP_MARK in (r.error or ""):  # R42/R49: the agent was killed by a stop; never a failure
            self._log(f"stop requested during {role} run {run_id} (agent stopped); run discarded")
            raise Stopped(", ".join(self._stop_flags()) or "stop")
        if not r.ok:  # R48/R50: a provider's usage limit is a hold until it resets, never a failed attempt
            until = limit_hold_until(f"{r.error or ''}\n{r.text or ''}", self.clock())
            if until is not None:
                held = provider or r.provider or "unknown"
                self.meter.hold(held, until)
                self._log(f"limit hit for {held} during {role} run {run_id}; holding until {until.isoformat()}")
                raise Capped(held)
        if not label.startswith("probe-"):  # after the tamper comparison, like the meter (R14)
            self._activity().add(self.last_run_s)
        return r

    def _tamper_alarm(self, role: str, run_id: str, changed: list[str]) -> None:
        """R9: KILL (with lanes, for every lane: they share caps and state), a log line and a halt question."""
        (self.state / "KILL").write_text("state tampered during an agent run\n")
        if self.shared is not None:  # R60: the lanes share caps and state; a tamper alarm stops them all
            self._stop_all(f"state tampered ({role}, {run_id}) in lane {self.lane}\n")
        try:
            self._log(f"TAMPER during {role} run {run_id}: {changed}")
            self._ask("tamper", f"Forge stopped: a {role} agent changed Forge's own state files",
                      "Files changed during the agent run:\n" + "\n".join(changed) +
                      "\n\nForge is halted (KILL). Nothing from that run was recorded.", halt=True)
        except Exception:  # noqa: BLE001 - the halt stands even if the report can't be written
            pass

    def _admit(self, provider: str | None, d: Path) -> None:
        """R60: launch admission. With lanes, one transaction under the shared lock: this lane's own accounting is
        what it last wrote, the shared accounting is readable, the provider is under its caps, holds and the runs
        per day, and the run is reserved (its run folder, which every lane's runs-per-day count sees) before the lock
        is released. So two lanes can never both take the last run of the day. Without lanes: the run folder."""
        if self.shared is None:
            d.mkdir(parents=True, exist_ok=True)
            return
        with self.shared_lock:
            try:
                self.meter.verify()
                self.mail_files.verify()
            except lanes_mod.AccountingTampered as e:
                self._tamper_alarm("launch admission", d.name, [str(e)])
                raise Tampered(str(e)) from e
            problem = lanes_mod.accounting_problem(self.shared)
            if problem:  # fail closed (R15): caps we can't count admit nothing
                raise lanes_mod.AccountingError(problem)
            if provider and self.meter.over(provider, self.limits):
                raise Capped(provider)
            d.mkdir(parents=True, exist_ok=True)

    def _layer_wt(self) -> Path | None:
        try:
            return self.wt
        except (KeyError, OSError, ValueError, TypeError):  # no queue yet (e.g. a readiness probe or smoke test)
            return None

    @staticmethod
    def _heads(*wts: Path) -> dict:
        """Live-run P1: each git worktree's checked-out ref and HEAD before an agent run."""
        out = {}
        for w in wts:
            w = Path(w)
            if w in out or not (w / ".git").exists():
                continue
            try:
                out[w] = (_git(w, "symbolic-ref", "-q", "HEAD", check=False), _git(w, "rev-parse", "HEAD"))
            except RuntimeError:
                continue
        return out

    def _undo_agent_commits(self, heads: dict) -> list[str] | None:
        """Live-run P1: the scope and protected-file checks look at dirty paths, so an agent that commits (e.g. by
        running git itself) would slip past them. A commit is a violation: every worktree whose ref or HEAD moved is
        put back on its ref and reset hard to its base. Returns what the commits changed (base..HEAD plus the dirty
        paths), or None when nothing moved."""
        moved, files = False, set()
        for w, (ref, base) in heads.items():
            now_ref = _git(w, "symbolic-ref", "-q", "HEAD", check=False)
            head = _git(w, "rev-parse", "HEAD", check=False)
            if head == base and now_ref == ref:
                continue
            moved = True
            if head:
                files.update(_norm(f) for f in _git(w, "diff", "--name-only", "-z", "--no-renames", base, head,
                                                    check=False).split("\0") if f.strip())
            try:
                files.update(self._changed(cwd=w))
            except RuntimeError:
                pass
            if now_ref != ref:
                if ref:
                    _git(w, "checkout", "-q", "-f", ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref)
                else:
                    _git(w, "checkout", "-q", "-f", "--detach", base)
            self._reset_to(w, base)
        return sorted(files) if moved else None

    def _stop_flags(self) -> list[str]:
        flags = [f for f in STOP_FILES if (self.state / f).exists()]
        if self.shared is not None and (self.shared / "KILL").exists():  # R60: the global KILL
            flags.append(SHARED_PREFIX + "KILL")
        return flags

    def _stop_requested(self) -> bool:
        """R42/R49: the check a real agent polls while it runs (core.agents.launch)."""
        return bool(self._stop_flags())

    def _raise_if_stopped(self) -> None:
        flags = self._stop_flags()
        if flags:
            raise Stopped(", ".join(flags))

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
        # Live-run P1: the answer drop folder lives outside state/ (the status page writes there any time), but an
        # agent can read question codes and forge an owner's answer there. Anything that appears in it, or
        # changes, while an agent runs is tampering. A missing folder is simply empty.
        drop = self.channel_in
        if drop.is_dir():
            for root, _dirs, files in os.walk(drop, onerror=fail):
                for name in files:
                    f = Path(root) / name
                    rel = DROP_PREFIX + f.relative_to(drop).as_posix()
                    fp[rel] = self._signature(f, rel)
        # R60: with lanes, the shared folder too, except what other lanes legitimately write at any time: their own
        # accounting files (meter/, holds/, mail/ of other lanes, and inbox_seen.json unless this is main; each
        # checked by lanes.shrunk: they may only grow), the shared lock, and atomic-replace temporaries. This lane's
        # own accounting files, migrated.json, the global KILL and anything else there are fingerprinted.
        if self.shared is not None and self.shared.is_dir():
            own = {f"{k}/{self.lane}.json" for k in lanes_mod.KINDS}
            for root, _dirs, files in os.walk(self.shared, onerror=fail):
                for name in files:
                    f = Path(root) / name
                    rel = f.relative_to(self.shared).as_posix()
                    others = (rel.split("/", 1)[0] in lanes_mod.KINDS and rel not in own) or \
                        (rel == lanes_mod.INBOX_SEEN and self.lane != lanes_mod.MAIN) or \
                        rel == lanes_mod.MANIFEST  # any lane registers its first file at any time (grow-checked)
                    if others or rel == lanes_mod.SHARED_LOCK or name.startswith(lanes_mod.TMP_PREFIX):
                        continue
                    fp[SHARED_PREFIX + rel] = self._signature(f, SHARED_PREFIX + rel)
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
        return any(p and self.meter.over(p, self.limits) for p in providers)  # R37/R48: Meter.over includes holds

    # ------------------------------------------------------------------ readiness (D-030)
    @property
    def _map_path(self) -> Path:
        return self.state / "capabilities.json"

    def _cap_map(self) -> dict:
        return readiness.read_map(self._map_path)

    @staticmethod
    def _age_s(entry, now: datetime) -> float | None:
        """Age of an entry's checked_at in seconds, or None when it is missing, invalid or naive."""
        raw = entry.get("checked_at") if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            return None
        try:
            at = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if at.tzinfo is None or at.utcoffset() is None:
            return None
        return (now - at).total_seconds()

    def _reusable_ok(self, name: str, entry, now: datetime) -> bool:
        ttl = readiness.ok_ttl_s(name, self.limits)
        age = self._age_s(entry, now)
        return ttl > 0 and readiness.broken(entry, now, ttl) is None and age is not None and 0 <= age < ttl

    def _refresh_readiness(self, names=None, force=frozenset()) -> dict:
        """Run the plain checks and (guarded, cap-aware) AI probes, write the capability map and return it.
        names limits the refresh to those capabilities; force bypasses every cache for the names in it."""
        if self._kill_set():
            return self._cap_map()
        old = self._cap_map()
        now = self.clock()
        force = set(force or ())
        wanted = None if names is None else set(names)
        pending = self._read("readiness_force.json", [])
        pending = [n for n in pending if isinstance(n, str)] if isinstance(pending, list) else []
        if pending:  # T1B2d: names Ben replied about are re-checked without any cache
            force |= {n for n in pending if wanted is None or n in wanted}
            left = [n for n in pending if not (wanted is None or n in wanted)]
            if left:
                self._write("readiness_force.json", left)
            else:
                (self.state / "readiness_force.json").unlink(missing_ok=True)
        new = dict(old)  # a targeted refresh keeps every other entry as it was

        todo = {}
        for name, fn in self.checks.items():
            if wanted is not None and name not in wanted:
                continue
            if name not in force and self._reusable_ok(name, old.get(name), now):
                continue  # gmail/browser: the permitted per-check cache
            todo[name] = fn
        if todo:
            new.update(readiness.evaluate(todo, float(self.limits.get("check_timeout_s", 60)), now))

        for name, agent in self.probes.items():
            if wanted is not None and name not in wanted:
                continue
            prev = old.get(name)
            if name not in force:
                if self._reusable_ok(name, prev, now):
                    continue
                age = self._age_s(prev, now)
                if isinstance(prev, dict) and prev.get("ok") is False and age is not None \
                        and 0 <= age < readiness.fail_retry_s(name, self.limits):
                    continue
            if self.meter.over(getattr(agent, "provider", None) or name, self.limits):
                continue  # R37: a capped provider is never probed; the old entry (if any) stays
            probe_dir = self.work / "_probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            r = self._guarded_run(f"probe-{name}", agent, readiness.PROBE_PROMPT, probe_dir, None)
            ok, detail = readiness.probe_ok(r)
            new[name] = {"ok": bool(ok), "detail": str(detail)[:readiness.DETAIL_MAX], "checked_at": now.isoformat()}

        result = {k: v for k, v in new.items() if k in self.checks or k in self.probes}
        readiness.write_map(self._map_path, result)
        return result

    def session_start(self) -> dict:
        """D-030: readiness before every build session. Launches nothing while stopped, paused or capped."""
        if self._kill_set() or (self.state / "PAUSED").exists() or self._capped():
            return {}
        m = self._refresh_readiness()
        try:
            self._route_capabilities(m)
        except Capped:
            pass  # the job waits for the cap to reset; nothing was launched
        self._capability_mail_check()
        return self._cap_map()

    def _requirements(self, role: str, task: dict | None = None) -> set[str]:
        provider = getattr(self._agent(role), "provider", None)
        return readiness.requirements(provider, (task.get("needs") or []) if role == "builder" and task else None)

    def _unready(self, names, cap_map: dict | None = None) -> dict[str, str]:
        m = self._cap_map() if cap_map is None else cap_map
        now = self.clock()
        out = {}
        for n in sorted(names):
            why = readiness.broken(m.get(n), now, readiness.max_age_for(n, self.limits))
            if why is not None:
                out[n] = why
        return out

    def ready_for(self, role: str, task: dict | None = None, cap_map: dict | None = None) -> dict[str, str]:
        """{name: reason} for every capability this role (and task, for the builder) needs but lacks usable
        evidence for. Empty means ready. The one readiness predicate (D-030)."""
        return self._unready(self._requirements(role, task), cap_map)

    def _launch_gate(self, provider: str | None, needs=None) -> None:
        """Before anything is written or launched: every needed capability has usable evidence, or NotReady."""
        req = readiness.requirements(provider, list(needs or []))
        unready = self._unready(req)
        known = set(self.checks) | set(self.probes)
        again = {n for n, why in unready.items()
                 if why.startswith("stale evidence") or (why == "no evidence" and n in known)}
        if again:
            self._refresh_readiness(names=again)  # never probes a capped provider
            unready = self._unready(req)
            if provider and self.meter.over(provider, self.limits):  # a metered probe may have used the budget
                raise Capped(provider)
        if unready:
            raise NotReady(unready)

    def _active_finalization(self, q: dict) -> str | None:
        """The first active (never blocked) merge-journal record, in queue order."""
        active = {r["tid"] for r in Journal(self.state).active()}
        return next((t["id"] for t in q["tasks"] if t["id"] in active), None) or min(active, default=None)

    REVIEW_PENDING = ("creating", "created", "judged")  # candidate states that still lead to the merge reviewer

    def _finalize_unready(self, cap_map: dict, tid: str | None = None) -> dict[str, str]:
        """What the finalization of tid needs for its next phase and lacks. It works in git and, with push on,
        pushes to GitHub. Codex review P1: a merge candidate that still awaits review needs the reviewer too, so
        while the reviewer is down it is not runnable progress (the capability email goes out, other work runs).
        A reviewer needed only in a later phase is still gated by _call itself."""
        unready = self._unready({"git"} | ({"github"} if self.push else set()), cap_map)
        rec = Journal(self.state).load(tid) if tid is not None else None
        cands = (rec or {}).get("candidates") or []
        if (rec or {}).get("phase") == "candidate" and cands and cands[-1].get("state") in self.REVIEW_PENDING:
            unready = {**self.ready_for("reviewer", cap_map=cap_map), **unready}
        return unready

    def _gate_ready(self, cap_map: dict) -> dict[str, str]:
        return self._unready({"git", "github"}, cap_map)

    def _unbuilt_test_files(self, tid: str) -> list[str]:
        """R59: test files of other build tasks in this layer that aren't done. Their tests were committed ahead of
        their code (Stage A runs before earlier tasks merge), so they can't pass yet and aren't this task's judges.
        The layer gate (every task done) and CI still run the whole suite."""
        out = []
        for t in self._queue().get("tasks", []):
            if t.get("id") != tid and t.get("kind", "build") == "build" and t.get("status") != "done":
                out += [str(f) for f in t.get("test_files") or []]
        return out

    def _pick_tasks(self, q: dict, cap_map: dict):
        """Runnable tasks in the order they should run: pending troubleshooting first (troubleshooter's
        requirements only), then todo/tests_ok tasks in queue order. Unready tasks record waiting_on and are
        skipped; self._waiting says whether anything was skipped for readiness."""
        done = {t["id"] for t in q["tasks"] if t.get("status") == "done"}
        tasks = [t for t in q["tasks"] if t["status"] in ("todo", "tests_ok")
                 and all(d in done for d in (t.get("depends_on") or []))]  # R55: dependencies first
        pending = [t for t in tasks if t["status"] == "tests_ok" and t.get("troubleshoot_pending")]
        rest = [t for t in tasks if t not in pending]
        for t in pending + rest:
            waiting = sorted(self._stage_unready(t, cap_map))
            if waiting or t.get("waiting_on"):
                if t.get("waiting_on") != waiting:
                    self._update(t["id"], waiting_on=waiting)
            if waiting:
                self._waiting = True
                continue
            yield t

    def _stage_unready(self, t: dict, cap_map: dict) -> dict[str, str]:
        """What task t's next stage needs and lacks: its role's requirements and, for a build, the reviewer's
        too (R54, Codex review P1: never start a builder whose work the reviewer can't review). The one predicate
        for task selection and for the capability-email hold."""
        role = self._stage_role(t)
        unready = self.ready_for(role, t, cap_map)
        if role == "builder":
            unready = {**self.ready_for("reviewer", cap_map=cap_map), **unready}
        return unready

    @staticmethod
    def _stage_role(t: dict) -> str:
        """The role that runs a task's next stage."""
        if t["status"] == "tests_ok" and t.get("troubleshoot_pending"):
            return "troubleshooter"
        if t.get("kind") == "plan":
            return "planner"
        return "test_writer" if t["status"] == "todo" else "builder"

    # ------------------------------------------------------------------ capability routing (T1B2d, D-030)
    def _role_providers(self) -> set[str]:
        return {p for p in (getattr(getattr(self.team, f), "provider", None) for f in Team.__dataclass_fields__) if p}

    def _open_cap_items(self) -> dict:
        return {k: v for k, v in self._read("questions.json", {}).items()
                if v.get("kind") == "capability" and v.get("status") == "open"}

    def _diagnose(self, name: str, cap_map: dict) -> dict:
        kw = {}
        if self.diagnose_probe is not None:
            kw["probe"] = self.diagnose_probe
        if self.diagnose_which is not None:
            kw["which"] = self.diagnose_which
        return readiness.diagnose(name, cap_map.get(name), cap_map, **kw)

    def _trouble_available(self, cap_map: dict) -> bool:
        provider = getattr(self.team.troubleshooter, "provider", None)
        return not self.ready_for("troubleshooter", cap_map=cap_map) and not (
            provider and self.meter.over(provider, self.limits))

    def _cap_candidates(self, cap_map: dict) -> tuple[list[str], dict, dict]:
        """(ordered candidates, diagnoses, active tasks). Dependents of another candidate are left out."""
        now = self.clock()
        tasks = [t for t in self._queue().get("tasks", []) if t.get("status") not in ("done", "blocked")]
        roles_need = {"git"} | self._role_providers()
        required = set(roles_need)
        for t in tasks:
            required.update(n for n in (t.get("needs") or []) if isinstance(n, str))
        cands = {n for n, e in cap_map.items() if readiness.broken(e, now, readiness.max_age_for(n, self.limits))}
        cands |= {n for n in required if n not in cap_map}
        diag = {n: self._diagnose(n, cap_map) for n in cands}
        cands = {n for n in cands if diag[n].get("depends_on") not in cands}
        waiting = set(roles_need)
        for t in tasks:
            waiting.update(t.get("waiting_on") or [])
            waiting.update(t.get("needs") or [])
        order = sorted(cands, key=lambda n: (n != "git", n not in waiting, n))
        return order, diag, {t["id"]: t for t in tasks}

    def _cap_item_text(self, name: str, entry, d: dict, task_ids: list[str], waiting_desc: str) -> tuple[str, str]:
        condition = d.get("condition", "error")
        subject = f"Forge needs {name} fixed ({condition})"
        detail = entry.get("detail") if isinstance(entry, dict) else None
        detail = str(detail) if detail else ("no readiness evidence yet" if entry is None else "no detail recorded")
        fix = str(d.get("fix", ""))
        if fix.startswith("PowerShell: "):
            how = "Paste this into PowerShell:\n" + fix[len("PowerShell: "):]
        elif fix.startswith("Win + R: "):
            how = "Press Win + R and paste:\n" + fix[len("Win + R: "):]
        else:
            how = fix  # an instruction, not a command (for example: reply with "not needed")
        parts = [f"Forge's readiness check says {name} is not usable.", f"Detail: {detail}", how]
        if d.get("then"):
            parts.append(f"Then: {d['then']}")
        parts.append(f"Waiting on it: {waiting_desc}")
        parts.append(f"If you don't answer, Forge keeps skipping work that needs {name} and re-checks it every cycle.")
        if condition == "no_check":
            parts.append(f"Forge has no automatic check for {name}. If the work doesn't really need it, reply to "
                         "this email with the first line: not needed\nAny other reply is kept for the "
                         "Troubleshooter; only a passing check closes this item.")
        return subject, "\n\n".join(parts)

    def _file_cap_item(self, name: str, cap_map: dict, d: dict, tasks: dict) -> None:
        """Exactly one open capability item per name; updated in place when the condition changes."""
        task_ids = sorted(tid for tid, t in tasks.items() if name in (t.get("needs") or []))
        roles = sorted(r for r in Team.__dataclass_fields__ if name in self._requirements(r))
        stage_waits = sorted(tid for tid, t in tasks.items()
                             if name in self._requirements(self._stage_role(t), t))
        waiting_desc = "; ".join(x for x in (
            ("roles: " + ", ".join(roles)) if roles else "",
            ("tasks: " + ", ".join(sorted(set(task_ids) | set(stage_waits)))) if (task_ids or stage_waits) else "",
        ) if x) or "nothing right now"
        subject, body = self._cap_item_text(name, cap_map.get(name), d, task_ids, waiting_desc)
        for qid, q in self._open_cap_items().items():
            if q.get("capability") == name:
                if q.get("condition") != d.get("condition") or q.get("tasks") != task_ids:
                    qs = self._read("questions.json", {})
                    qs[qid].update(condition=d.get("condition"), tasks=task_ids,
                                   subject=subject[:SUBJECT_CAP], body=body[:BODY_CAP])
                    self._write("questions.json", qs)
                return
        self._ask("capability", subject, body, hold=True, capability=name, condition=d.get("condition"),
                  tasks=task_ids)

    def _route_capabilities(self, cap_map: dict) -> str | None:
        """Route every broken or missing capability: at most one Troubleshooter job per step, everything else
        to Ben's queue (held unless _capability_mail_check releases it). Returns "worked" after a job."""
        now = self.clock()
        routing = self._read("cap_routing.json", {})
        routing = routing if isinstance(routing, dict) else {}
        # 2. resolved: usable evidence closes the item and resets routing state
        self._resolve_caps(cap_map, routing)
        order, diag, tasks = self._cap_candidates(cap_map)
        if not order:
            return None
        max_rounds = int(self.limits.get("cap_trouble_max", 3))
        retry_s = float(self.limits.get("cap_retry_h", 1)) * 3600
        available = self._trouble_available(cap_map)

        def job_possible_now(n: str) -> bool:
            st = routing.get(n) or {}
            last = self._age_s({"checked_at": st.get("last_job")}, now) if st.get("last_job") else None
            return available and bool(diag[n].get("troubleshoot")) and int(st.get("rounds", 0)) < max_rounds \
                and (last is None or last >= retry_s)

        job = next((n for n in order if job_possible_now(n)), None)
        for n in order:  # everything that can't get a job, now or later, goes to Ben
            st = routing.get(n) or {}
            if not (available and diag[n].get("troubleshoot") and int(st.get("rounds", 0)) < max_rounds):
                self._file_cap_item(n, cap_map, diag[n], tasks)
        if job is None:
            return None
        self._cap_job(job, cap_map, diag[job], tasks, routing)
        return "worked"

    def _resolve_caps(self, cap_map: dict, routing: dict) -> None:
        now = self.clock()
        usable = {n for n, e in cap_map.items()
                  if readiness.broken(e, now, readiness.max_age_for(n, self.limits)) is None}
        qs = self._read("questions.json", {})
        changed = False
        for q in qs.values():
            if q.get("kind") == "capability" and q.get("status") == "open" and q.get("capability") in usable:
                q["status"], q["closed_at"] = "resolved", now.isoformat()
                q["closed_seq"] = self._read("q_seq.json", {}).get("n", 0)
                changed = True
        if changed:
            self._write("questions.json", _prune_questions(qs))
        gone = [n for n in routing if n in usable]
        if gone:
            for n in gone:
                del routing[n]
            self._write("cap_routing.json", routing)

    def _cap_job(self, name: str, cap_map: dict, d: dict, tasks: dict, routing: dict) -> None:
        entry = cap_map.get(name)
        detail = entry.get("detail") if isinstance(entry, dict) else None
        waiting = sorted(tid for tid, t in tasks.items() if name in (t.get("needs") or []))
        prompt = (f"You are the TROUBLESHOOTER. CAPABILITY FIX JOB: {name}\n\n"
                  f"Forge's plain-code readiness check says the capability {name} is not usable, and work waits "
                  "on it. Make it work, then say what you did.\n\n"
                  f"Detail: {detail if detail else 'no readiness evidence yet'}\n"
                  f"Diagnosed condition: {d.get('condition')}\n"
                  f"Suggested fix line: {d.get('fix')}\n" +
                  (f"Then: {d.get('then')}\n" if d.get("then") else "") +
                  f"Tasks waiting on it: {', '.join(waiting) or 'none named'}\n\n"
                  "RULES:\n"
                  "- Free, vetted tools only (D-032): a known publisher, not on the do-not-install list, no account "
                  "needed.\n"
                  "- No paid services and no new accounts; anything like that is Ben's call.\n"
                  "- Never read, print or log secrets (passwords, tokens, keys).\n"
                  "- Nothing visible on Ben's screen: no windows, no taking over his mouse or keyboard.\n"
                  "- Work in this folder; never touch Forge's state files.\n\n"
                  "Answer with JSON: {\"kind\": \"fix\" | \"dead_end\" | \"suggestion\", \"notes\": \"...\", "
                  "\"alternative\": \"...\"}")
        folder = self.work / "_capfix"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            r = self._call("troubleshooter", prompt, S_TROUBLE, cwd=folder)  # Capped and Tampered propagate
        except NotReady:
            r = None
        st = dict(routing.get(name) or {})
        st["rounds"] = int(st.get("rounds", 0)) + 1
        st["last_job"] = self.clock().isoformat()
        routing[name] = st
        self._write("cap_routing.json", routing)
        if r is not None and r.ok and (r.data or {}).get("kind") == "dead_end":
            with (self.state / "dead_ends.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"capability": name, "notes": (r.data or {}).get("notes", ""),
                                    "alternative": (r.data or {}).get("alternative", "")}) + "\n")
        m = self._refresh_readiness(names={name}, force={name})
        self._resolve_caps(m, self._read("cap_routing.json", {}))

    def _capability_mail_check(self) -> None:
        """D-023: an instant email only when Ben alone can unblock all progress; otherwise items stay held."""
        items = self._open_cap_items()
        if not items:
            return
        m = self._cap_map()
        q = self._queue()
        if q.get("drift_due") and not self.ready_for("drift_keeper", cap_map=m):
            return
        ftid = self._active_finalization(q)
        if ftid is not None and not self._finalize_unready(m, ftid):
            return  # a finalization (T1B3e) whose next phase can run is progress too
        for t in q.get("tasks", []):
            if t.get("status") in ("todo", "tests_ok") and not self._stage_unready(t, m):
                return
        order, diag, _ = self._cap_candidates(m)
        routing = self._read("cap_routing.json", {})
        if not self.ready_for("troubleshooter", cap_map=m):  # usable now, or once its provider's cap resets
            max_rounds = int(self.limits.get("cap_trouble_max", 3))
            if any(diag[n].get("troubleshoot") and int((routing.get(n) or {}).get("rounds", 0)) < max_rounds
                   for n in order):
                return  # a Troubleshooter job is still possible, now or later
        for qid, it in items.items():
            if it.get("hold") or it.get("delivered") is False:
                qs = self._read("questions.json", {})
                qs[qid]["hold"] = False
                self._write("questions.json", qs)
                self._deliver(qid)

    MAP_BLOCK_CAP, DEAD_BLOCK_CAP, DEAD_LINES = 4000, 20000, 50

    def _map_block(self) -> str:
        head = "CAPABILITY MAP (plain-code readiness check; blocker claims that contradict it are rejected):"
        m, now = self._cap_map(), self.clock()
        if not m:
            return head + "\n(no readiness evidence yet)"
        lines = []
        for name in sorted(m):
            e = m[name]
            why = readiness.broken(e, now, readiness.max_age_for(name, self.limits))
            detail = e.get("detail", "") if isinstance(e, dict) else ""
            at = e.get("checked_at", "?") if isinstance(e, dict) else "?"
            state = "OK" if why is None else f"BROKEN ({why})"
            lines.append(f"- {name}: {state} - {detail} (checked {at})")
        text = head + "\n" + "\n".join(lines)
        if len(text) > self.MAP_BLOCK_CAP:
            text = text[:self.MAP_BLOCK_CAP - 4].rstrip() + "\n..."
        return text

    def _dead_block(self) -> str:
        lines = self._dead_ends()[-self.DEAD_LINES:]
        while lines and len("\n".join(lines)) > self.DEAD_BLOCK_CAP:
            lines = lines[1:]  # drop the oldest first
        if not lines:
            return ""
        return "KNOWN DEAD ENDS:\n" + "\n".join(lines)[-self.DEAD_BLOCK_CAP:]

    def _prompt_blocks(self, role: str) -> str:
        """Appended to every prompt (never prepended): the capability map; dead ends for builder/troubleshooter."""
        out = "\n\n" + self._map_block() + "\n"
        if role in ("builder", "troubleshooter"):
            dead = self._dead_block()
            if dead:
                out += "\n" + dead + "\n"
        return out

    # ------------------------------------------------------------------ email
    def _ask(self, kind: str, subject: str, body: str, halt: bool = False, hold: bool = False, **extra) -> str:
        if self._channel_on:  # 1E / D-023: every question states its default; only instant kinds mail at once
            body = f"{body}\n\nIf you don't answer: {extra.get('default') or channel.default_for(kind)}"
            if not halt and not hold and not channel.is_instant(kind):
                hold, extra = True, dict(extra, digest=True)
        qs = self._read("questions.json", {})
        seq = self._read("q_seq.json", {"n": len(qs)})
        seq["n"] = int(seq.get("n", 0)) + 1
        self._write("q_seq.json", seq)
        qid = f"{kind}-{seq['n']}" if self.lane == lanes_mod.MAIN else f"{self.lane}-{kind}-{seq['n']}"  # R60
        code = secrets.token_urlsafe(6)[:8]
        qs[qid] = {"kind": kind, "status": "open", "code": code, "subject": str(subject)[:SUBJECT_CAP],
                   "body": str(body)[:BODY_CAP], "delivered": False, **extra}  # R28
        if halt:
            qs[qid]["halt"] = True  # R32: retried on watchdog starts while KILL is set
        if hold:
            qs[qid]["hold"] = True  # T1B2d: stored, not sent (the digest or an instant-email check sends it)
        self._write("questions.json", _prune_questions(qs))
        if not hold:
            self._deliver(qid, halt=halt)
        return qid

    def _send(self, subject: str, body: str, halt: bool = False) -> bool:
        """R20/R24/R25: every email goes through here. Nothing but a halt alert while KILL is set; at most
        mail_per_hour / mail_per_day attempts (counted before sending); each email gets a recorded Message-ID."""
        if self._kill_set() and not halt:
            return False
        if not halt and self._quiet_now():  # 1E / D-023: quiet hours; nothing is attempted, nothing counted
            return False
        now = self.clock()
        notes = self._read("notices.json", {})
        if halt:  # R24: at most one halt alert every 12 hours
            last = notes.get("halt")
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 12 * 3600:
                return False
        from email.utils import make_msgid

        def reserve(log: dict, others=()) -> str | None:  # R60: one locked transaction across every lane
            sent = [x for x in log.get("sent", []) if (now - datetime.fromisoformat(x)).total_seconds() < 86400]
            every = sent + [x for x in others if (now - datetime.fromisoformat(x)).total_seconds() < 86400]
            hour = [x for x in every if (now - datetime.fromisoformat(x)).total_seconds() < 3600]
            if len(hour) >= int(self.limits.get("mail_per_hour", 6)) or \
                    len(every) >= int(self.limits.get("mail_per_day", 30)):
                stamp = now.strftime("%Y-%m-%dT%H")
                if log.get("budget_logged") != stamp:
                    self._log(f"mail budget reached; held back: {subject[:120]}")
                    log["budget_logged"] = stamp
                log["sent"] = sent
                return None
            mid = make_msgid(domain="forge.local")
            log["sent"] = sent + [now.isoformat()]  # R25: the attempt counts even if SMTP fails part-way
            log["ids"] = (list(log.get("ids", [])) + [mid])[-SENT_IDS_KEEP:]
            return mid

        if self.shared is None:
            log = self._read("mail_log.json", {"sent": [], "budget_logged": "", "ids": []})
            mid = reserve(log)
            self._write("mail_log.json", log)
        else:  # R60: every lane's sends count; only this lane's own mail file is written
            try:
                with self.shared_lock:
                    others = [x for ln, d in self.mail_files.read_all().items() if ln != self.lane
                              for x in d.get("sent", [])]
                    mid = self.mail_files.update(lambda log: reserve(log, others))
            except lanes_mod.AccountingError as e:  # fail closed: a budget we can't count sends nothing
                self._log(f"mail held back, shared mail accounting can't be trusted: {e}"[:500])
                return False
        if mid is None:
            return False
        if halt:  # R32: the halt throttle starts only when an attempt actually goes ahead
            notes["halt"] = now.isoformat()
            self._write("notices.json", notes)
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
        if self._kill_set() or self._quiet_now():  # 1E: a quiet-hours notice waits, it isn't lost
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
            if q.get("delivered") is False and not q.get("hold"):
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
        own_ids = set(self._mail_log().get("ids", []))
        names = lanes_mod.listed(self.shared.parent) if self.shared is not None and self.lane == lanes_mod.MAIN \
            else []
        for i, m in enumerate(batch):
            try:
                if not isinstance(m, dict):
                    continue
                if m.get("outgoing") or (m.get("message_id") and str(m["message_id"]).strip() in own_ids):
                    continue  # R18/R26: Forge's own mail is never an answer
                if not self._from_owner(m.get("from", "")):
                    continue
                subject, body = str(m.get("subject", "")), clean_reply(str(m.get("body", "")))
                if is_stop(subject, body):
                    self._stop_all("stopped by owner email\n")  # R60: a STOP stops every lane
                    rest = [dict(x, body=clean_reply(str(x.get("body", "")))) for x in batch[i + 1:]
                            if isinstance(x, dict)]
                    if rest:  # R35: kept for after the restart, never lost
                        self._write("inbox_pending.json", rest[-50:])
                    return  # R24: nothing after a STOP is processed now
                mq = re.search(r"\[Forge Q-([\w-]+) ([\w-]{8})\]", subject)
                if mq and lanes_mod.qid_lane(mq.group(1), names):  # R60: main hands it to the lane that asked
                    lanes_mod.route(self.state, lanes_mod.qid_lane(mq.group(1), names),
                                    {"from": m.get("from", ""), "subject": subject, "body": body,
                                     "message_id": m.get("message_id", "")})
                elif mq:
                    self._answer(mq.group(1), body, mq.group(2))
            except Exception as e:  # noqa: BLE001 - R35: one bad message never blocks the rest
                self._log(f"inbox message failed: {e!r}"[:500])
        self._take_channel_answers()  # 1E: answers from the status page (and any later channel)

    def _from_owner(self, sender) -> bool:
        """1E: exactly one address, and it is the owner's. The owner's address in a display name
        ("ben@..." <someone@else>) or next to another address is someone else."""
        from email.utils import getaddresses
        addrs = [a.strip().lower() for _, a in getaddresses([str(sender or "")]) if a.strip()]
        return len(addrs) == 1 and addrs[0] == self.owner

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
                    t["focus_s"] = 0
            self._save_queue(qd)
        elif q["kind"] == "merge":  # the only thing that retries a blocked merge record
            Journal(self.state).unblock(qid, body.strip()[:NOTE_CAP])
        elif q["kind"] == "capability":
            if not self._answer_capability(qs, q, reply, body):
                return
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

    def _answer_capability(self, qs: dict, q: dict, reply: str, body: str) -> bool:
        """A reply never makes a capability ready. It is kept and forces a re-check; only a first line of
        exactly "not needed" drops the need from the item's tasks and closes it. Returns True to close."""
        name = str(q.get("capability", ""))
        q["replies"] = ([str(x)[:NOTE_CAP] for x in q.get("replies", [])] + [body.strip()[:NOTE_CAP]])[-NOTES_KEEP:]
        first = reply.strip().rstrip(".!?,;: ").strip().lower()
        if first == "not needed" and name and name != "git" and name not in self._role_providers():
            ids = set(q.get("tasks") or [])
            qd = self._queue()
            for t in qd["tasks"]:
                if t["id"] in ids and name in (t.get("needs") or []):
                    t["needs"] = [n for n in t["needs"] if n != name]
            self._save_queue(qd)
            return True
        force = self._read("readiness_force.json", [])
        force = [n for n in force if isinstance(n, str)] if isinstance(force, list) else []
        if name and name not in force:
            self._write("readiness_force.json", force + [name])
        self._write("questions.json", _prune_questions(qs))
        return False

    # ------------------------------------------------------------------ Ben's channel (1E, D-021 to D-024)
    local_tz = None  # None: the PC's own zone (Ben's local time); tests set a fixed zone
    page_url = "http://127.0.0.1:8765"
    channel_dir = None  # None: <state>/../channel, outside the fingerprinted state (the page writes there any time)
    STALL_EVERY_S, DIGEST_RETRY_S, DIGEST_IDS_KEEP = 12 * 3600, 3600, 500

    @property
    def _channel_on(self) -> bool:
        """The digest / quiet-hours policy is on only when the charter limits set digest_hour (D-035: switched on
        deliberately, after a watched cycle)."""
        return self.limits.get("digest_hour") is not None

    def _local_now(self) -> datetime:
        return channel.to_local(self.clock(), self.local_tz)

    def _quiet_now(self) -> bool:
        return self._channel_on and channel.is_quiet(self._local_now(), int(self.limits.get("quiet_start", 23)),
                                                     int(self.limits.get("quiet_end", 7)))

    @property
    def channel_in(self) -> Path:
        return Path(self.channel_dir or (self.state.parent / "channel")) / "in"

    def _write_ben_queue(self, questions) -> None:
        try:
            channel.write_queue(self.state / "queue.jsonl", questions if isinstance(questions, dict) else {})
        except OSError as e:  # the view is a convenience; questions.json stays the source of truth
            self._log(f"queue.jsonl write failed: {e!r}"[:500])

    def _take_channel_answers(self) -> None:
        """Answers dropped by the status page (or a later channel) are checked exactly like email replies: an open
        question and its code. A STOP creates KILL at once; answers after it are kept for after the restart."""
        if self._kill_set():
            return
        answers = channel.take_answers(self.channel_in)
        for i, a in enumerate(answers):
            try:
                text = clean_reply(a["answer"])
                if is_stop("", text):
                    self._stop_all(f"stopped by owner via {a['source']}\n")
                    for rest in answers[i + 1:]:
                        channel.drop_answer(self.channel_in, rest["qid"], rest["code"], rest["answer"], rest["source"])
                    return
                kind = str((self._read("questions.json", {}).get(a["qid"]) or {}).get("kind", ""))
                if kind in channel.EMAIL_ONLY_KINDS:  # Live-run P1: approvals only by the owner's own email
                    self._log((f"refused a {kind} answer for Q-{a['qid']} from {a['source']}: "
                               "approvals are accepted only by email from the owner")[:500])
                    continue
                self._answer(a["qid"], text, a["code"])
            except Exception as e:  # noqa: BLE001 - one bad answer never blocks the rest
                self._log(f"channel answer failed: {e!r}"[:500])

    def _mail_used(self) -> tuple[int, int]:
        now = self.clock()
        ages = []
        for x in self._mail_log().get("sent", []):
            try:
                ages.append((now - datetime.fromisoformat(x)).total_seconds())
            except (TypeError, ValueError):
                continue
        return sum(1 for a in ages if a < 3600), sum(1 for a in ages if a < 86400)

    def _work_can_run(self, q: dict) -> bool:
        if any(t.get("status") in ("todo", "tests_ok") for t in q.get("tasks", [])):
            return True
        try:  # a finalization is runnable work only if its next phase's needs are ready (Codex review P1)
            tid = self._active_finalization(q)
            return tid is not None and not self._finalize_unready(self._cap_map(), tid)
        except (RuntimeError, OSError, ValueError, KeyError):
            return True  # unsure: no early digest

    def _channel_tick(self) -> None:
        """D-023 digest: at most one a local day, at or after digest_hour, when there is something to report; and
        an early one (at most every 12 hours) when nothing can run and Ben hasn't yet heard of a held question.
        Every attempt is recorded before sending and retried at most hourly; all mail goes through _send."""
        if not self._channel_on or self._kill_set() or self._quiet_now():
            return
        try:
            now, local = self.clock(), self._local_now()
            today = local.date().isoformat()
            st = self._read("digest.json", {})
            st = st if isinstance(st, dict) else {}

            def age(key: str) -> float:
                try:
                    return (now - datetime.fromisoformat(st[key])).total_seconds()
                except (KeyError, TypeError, ValueError):
                    return float("inf")

            if age("tried_at") < self.DIGEST_RETRY_S:
                return
            qs = self._read("questions.json", {})
            items = channel.queue_items(qs)
            q = self._queue()
            tasks = q.get("tasks", [])
            summary = json.dumps(sorted((str(t.get("id")), str(t.get("status"))) for t in tasks if isinstance(t, dict)))
            included = set(st.get("included", []))
            fresh = [i["id"] for i in items if i["via"] == "digest" and i["id"] not in included]
            daily = local.hour >= int(self.limits["digest_hour"]) and st.get("sent_day") != today
            if daily and not items and summary == st.get("summary", json.dumps([])):
                st["sent_day"] = today  # nothing open, nothing changed: no email
                self._write("digest.json", st)
                return
            early = bool(fresh) and age("stall_at") >= self.STALL_EVERY_S and not self._work_can_run(q)
            if not (daily or early):
                return
            st["tried_at"] = now.isoformat()  # R25: the attempt is recorded first
            self._write("digest.json", st)
            subject, body = channel.build_digest(
                qs, tasks, owner=self.owner, local_now=local, mail_used=self._mail_used(),
                mail_caps=(int(self.limits.get("mail_per_hour", 6)), int(self.limits.get("mail_per_day", 30))),
                page_url=self.page_url)
            if not self._send(subject, body):
                return
            st["included"] = (list(st.get("included", [])) + [i["id"] for i in items if i["id"] not in included]
                              )[-self.DIGEST_IDS_KEEP:]
            st["summary"] = summary
            if daily:
                st["sent_day"] = today
            if early:
                st["stall_at"] = now.isoformat()
            self._write("digest.json", st)
        except Exception as e:  # noqa: BLE001 - the digest never stops the conductor
            self._log(f"digest error: {e!r}"[:500])

    # ------------------------------------------------------------------ main step
    def step(self) -> str:
        if self._kill_set():  # R21: KILL stops everything, email included
            if self._mail_reader_only():  # R60: except the mailbox, while other lanes may still need it
                self._mailbox_only()
            return "killed"
        self._handle_inbox()
        if self._kill_set():
            return "killed"
        acct = self._accounting_check()  # R60: caps that can't be counted admit no work (fail closed)
        if acct is not None:
            return acct
        self._channel_tick()  # 1E: the daily digest (a no-op unless the channel policy is on)
        if (self.state / "PAUSED").exists():
            return "paused"
        if self._capped():
            return "capped"
        # D-030 / T1B3e order: readiness first (every cycle, before any launch), then crash recovery of the
        # ledger, the merge journal and the worktrees, then capability routing, then work.
        try:
            fresh = self._refresh_readiness()
        except Tampered:
            return "killed"
        except Capped:
            return self._held()
        except (RuntimeError, OSError) as e:  # R15 fail-closed preconditions: a stage-like error
            self._log(f"readiness refresh error: {e!r}"[:500])
            return "error"
        try:
            self._reconcile_merges()
        except (RuntimeError, OSError, Rejected) as e:
            self._log(f"reconcile error: {e!r}")
            return "error"
        self._sync_with_main()  # R58: plain git, never an agent; never fails the step
        try:
            routed = self._route_capabilities(fresh)
        except Tampered:
            return "killed"
        except Capped:
            return self._held()
        except (RuntimeError, OSError) as e:
            self._log(f"capability routing error: {e!r}"[:500])
            return "error"
        if routed == "worked":
            return "worked"
        self._capability_mail_check()
        q = self._queue()
        cap_map = self._cap_map()
        self._waiting = False
        try:  # T1C5: merges are recorded before any stall threshold is checked (plain code, no agent)
            dstate, deferred = self._drift_bookkeeping(q)
        except (RuntimeError, OSError, Rejected) as e:
            self._log(f"drift bookkeeping error: {e!r}"[:500])
            return "error"
        if (q.get("drift_due") or dstate.get("stall")) and not deferred:
            if self.ready_for("drift_keeper", cap_map=cap_map):
                self._waiting = True  # the gate stays closed while drift_due is set
            else:
                try:
                    self._drift_check()
                except Capped:
                    return self._held()
                except NotReady:
                    return "not_ready"
                except Tampered:
                    return "killed"
                return "worked"
        tid = self._active_finalization(q)
        if tid is not None:  # a started finalization comes before any new work; blocked records never run
            if self._finalize_unready(cap_map, tid):  # D-030: it waits like any launch; other work may run
                self._waiting = True
            else:
                try:
                    self._ensure_worktree(q["layer"])
                    self._finalizer().run(tid)
                except Capped:
                    return self._held()
                except NotReady:  # the merge reviewer's gate; the record stays where it was
                    return "not_ready"
                except Tampered:
                    return "killed"
                except (RuntimeError, OSError, Rejected) as e:  # R13; never a failed attempt
                    self._log(f"finalize error on {tid}: {e!r}")
                    return "error"
                return "worked"
        if (drift_mod.load(self.state) or {}).get("replan"):  # T1C5: a pending re-plan comes before new work
            if getattr(self, "manager", None) is not None and self.ready_for("manager", cap_map=cap_map):
                return "not_ready"
            try:
                self._ensure_worktree(q["layer"])
                self._replan_stage()
            except Capped:  # R37/R42
                return self._held()
            except NotReady:
                return "not_ready"
            except Tampered:
                return "killed"
            except (RuntimeError, OSError, Rejected) as e:  # R13: the re-plan stays pending
                self._log(f"replan error: {e!r}"[:500])
                return "error"
            return "worked"
        for t in self._pick_tasks(q, cap_map):
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
            except Capped:  # R37/R42: the attempt was undone by its stage; retried when the cap resets
                return self._held()
            except NotReady:  # D-030: undone exactly like Capped; retried once evidence is usable
                return "not_ready"
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
                and not self._drift_busy() \
                and not any(v["kind"] == "gate" for v in qs.values()) \
                and not any(r.get("status") in ("active", "blocked") for r in Journal(self.state).all()):
            if self._gate_ready(cap_map):  # the gate needs git and github
                return "not_ready"
            try:
                self._gate()
            except Capped:  # R42/R49: a stop during the gate's full suite is never a failure
                return self._held()
            except (RuntimeError, OSError) as e:  # e.g. a refused push: no pull request is opened
                self._log(f"gate error: {e!r}")
                return "error"
            return "gate"
        return "not_ready" if self._waiting else "idle"

    def _held(self) -> str:
        """R42: why an undone attempt stopped: Ben's stop, a pause, or a token cap."""
        if self._kill_set():
            return "killed"
        if (self.state / "PAUSED").exists():
            return "paused"
        return "capped"

    def run(self, max_steps: int | None = None, idle_sleep_s: int = 60, heartbeat: Path | None = None,
            sleep: Callable[[float], None] = time.sleep, on_step: Callable[[str], None] | None = None) -> str:
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
            if status == "killed" or self._kill_set():  # T1D2: a stop pressed mid-step ends the loop
                if not self._mail_reader_only():
                    return "killed"
                if on_step:  # R60: main stopped alone stays up as the mailbox reader for the other lanes
                    try:
                        on_step(status)
                    except Exception as e:  # noqa: BLE001
                        self._log(f"on_step hook failed: {e!r}"[:500])
                sleep(idle_sleep_s)
                continue
            if on_step:  # T1D2: the always-on service (core.service): status, pacing; never fatal
                try:
                    on_step(status)
                except Exception as e:  # noqa: BLE001
                    self._log(f"on_step hook failed: {e!r}"[:500])
            if status == "error":
                errors += 1
                if errors >= 3 and not told:
                    told = True
                    try:
                        self._notice_once("error", "[Forge] the conductor keeps hitting an error",
                                          "It keeps retrying with a growing pause. Details: state/bootstrap/errors.log"
                                          if self.lane == lanes_mod.MAIN else
                                          f"It keeps retrying with a growing pause. Lane {self.lane}. "
                                          f"Details: state/lanes/{self.lane}/errors.log")
                    except Exception:  # noqa: BLE001
                        pass
                sleep(min(idle_sleep_s * errors, 1800))
                continue
            errors, told = 0, False
            if status in ("idle", "paused", "capped", "gate", "not_ready"):
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
        return self._exec_tests(t, cwd=cwd)

    def _exec_tests(self, t: dict, cwd: Path | None = None) -> tuple[int, str, bool]:
        """The real test run behind _run_tests. Bytecode goes to a fresh private cache that is deleted afterwards,
        so no run reads a stale .pyc (e.g. of a same-size stub) or leaves one in the worktree."""
        paths = parse_test_cmd(t["test_cmd"], t["test_files"])
        if paths is None:
            return 2, "unsafe test_cmd", False
        import shutil
        import tempfile
        cache = tempfile.mkdtemp(prefix="forge-pyc-")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPYCACHEPREFIX=cache)
        t0 = self.clock()
        try:
            code, out, why = run_tree([sys.executable, "-m", "unittest", *paths], cwd or self.wt,
                                      int(self.limits.get("test_timeout_s", 600)), env=env,
                                      should_stop=self._stop_requested)
            if why == "stopped":  # R42/R49: a stop is never a failed run
                raise Stopped(", ".join(self._stop_flags()) or "stop")
            if why == "timeout":
                return 124, "test command timed out", True
            return code, out, False
        finally:
            shutil.rmtree(cache, ignore_errors=True)
            self._activity().add(self._since(t0))  # T1C4: judges are active work

    def _run_tests_on_stub(self, t: dict) -> tuple[int, str, bool]:
        """The empty-implementation run of Stage A (always the real runner, isolated bytecode cache). Stage A works
        in the layer worktree: its code plus the writer's uncommitted tests is exactly what is being checked."""
        return self._exec_tests(t, cwd=self.wt)

    def _empty_impl_check(self, t: dict, changed: list[str]) -> str | None:
        """R4 on an empty implementation: stub the in-scope modules, run the tests, restore. Returns a rejection
        note or None. Raises RuntimeError if the worktree is not exactly back to the writer's test files."""
        tracked = [_norm(x) for x in _git(self.wt, "ls-files").splitlines() if x.strip()]
        to_stub, to_create = stub_targets(list(t["files_in_scope"]), tracked, list(t["test_files"]))
        created_files: list[Path] = []
        created_dirs: list[Path] = []
        preexisting: dict[Path, bytes] = {}
        originals: dict[Path, bytes] = {}  # the exact bytes of every stubbed module, written back afterwards
        try:
            for rel in to_stub:
                f = self.wt / rel
                raw = f.read_bytes()
                originals[f] = raw
                try:
                    text = empty_implementation(raw.decode("utf-8"))
                except (SyntaxError, ValueError):  # unparsable (or undecodable): the empty module is an empty file
                    text = ""
                f.write_bytes(text.encode("utf-8"))
            for rel in to_create:
                f = self.wt / rel
                missing = []
                d = f.parent
                while d != self.wt and not d.exists():
                    missing.append(d)
                    d = d.parent
                for d in reversed(missing):
                    d.mkdir()
                    created_dirs.append(d)
                if f.exists():
                    preexisting[f] = f.read_bytes()
                else:
                    created_files.append(f)
                f.write_bytes(b"")
            code, out, timed_out = self._run_tests_on_stub(t)
        finally:
            # Integration fix C: never git's checkout, which (core.autocrlf=true on Windows) writes CRLF bytes
            for f, raw in originals.items():
                f.write_bytes(raw)
            for f in created_files:
                f.unlink(missing_ok=True)
            for f, raw in preexisting.items():
                f.write_bytes(raw)
            for d in sorted(created_dirs, key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        if self._changed() != sorted(changed):
            raise RuntimeError("empty implementation restore failed")
        why = real_failing_run(code, out, timed_out)
        if why == "passed":
            return "tests rejected: weak (they pass on an empty implementation)"
        if why:
            return f"tests rejected: no real failing run on the empty implementation ({why})"
        return None

    def _run_cmd(self, cmd: str, cwd: Path | None = None) -> tuple[int, str]:
        t0 = self.clock()
        try:
            return self._run_cmd_timed(cmd, cwd)
        finally:
            self._activity().add(self._since(t0))  # T1C4: judges are active work

    def _run_cmd_timed(self, cmd: str, cwd: Path | None = None) -> tuple[int, str]:
        try:
            code, out, why = run_tree(_cmd_args(cmd), cwd or self.wt,  # R55: the whole suite needs longer
                                      int(self.limits.get("judge_timeout_s", JUDGE_TIMEOUT_S)),
                                      should_stop=self._stop_requested)
        except (OSError, ValueError) as e:  # not runnable (missing program, bad quoting): a failed judge
            return 127, f"command could not start: {cmd}: {e!r}"
        if why == "stopped":  # R42/R49: a stop is never a failed judge
            raise Stopped(", ".join(self._stop_flags()) or "stop")
        if why == "timeout":
            return 124, f"command timed out: {cmd}"
        return code, out

    def _task_prompt(self, t: dict) -> str:
        return (f"TASK {t['id']}: {t['title']}\n\n{t.get('section', '')}\n\n"
                f"Files you may change: {', '.join(t.get('files_in_scope', []))}\n"
                f"Test files (read-only for builders): {', '.join(t.get('test_files', []))}\n"
                f"Done means this passes: {t.get('test_cmd', '')}\n")

    # ------------------------------------------------------------------ stage A: tests
    def _covers_text(self, t: dict) -> str:
        """R63: the requirements an evidence task proves, with their spec text when the spec has them."""
        spec = self._spec()
        reqs = spec[1] if spec else {}
        ids = [str(c) for c in t.get("covers") or []]
        return ("\nREQUIREMENTS TO PROVE (covers):\n" + "\n".join(f"{c}: {reqs.get(c, '')}".rstrip(": ")
                                                           for c in ids) + "\n") if ids else ""

    def _evidence_review(self, t: dict, sha: str, twt) -> str:
        """R63: an evidence task is judged on its tests: they must prove every requirement in covers."""
        if t.get("evidence") is not True:
            return ""
        text = []
        for f in t["test_files"]:
            try:
                text.append(f"--- {f}\n" + _git(twt, "show", f"{sha}:{_norm(f)}"))
            except Exception:  # noqa: BLE001 - a missing file is shown as missing
                text.append(f"--- {f}: missing")
        return ("\nEVIDENCE TASK (R63): the code already existed, so the diff may be empty. Fail it when the "
                "tests below don't prove every requirement in covers." + self._covers_text(t) +
                "\nTESTS:\n" + "\n".join(text)[:40000] + "\n")

    def _tests_stage(self, tid: str) -> None:
        t = self._task(tid)
        self._reset_wt()
        evidence = t.get("evidence") is True  # R63
        goal = (" The code already exists: these are evidence tests (R63). They must prove the requirements in "
                "covers against the existing code, so they must pass now, and must fail if the in-scope code were "
                "emptied." + self._covers_text(t) if evidence else " The tests must fail until the feature exists.")
        prompt = (role_text(self.repo, "test_writer") + "\n\nWrite only these files: " + ", ".join(t["test_files"]) +
                  "." + goal + " Do not write any other file.\n\n" +
                  self._task_prompt(t) + "\nAnswer with JSON: {\"files\": [...], \"summary\": \"...\"}")
        try:
            r = self._call("test_writer", prompt, S_TESTS)
        except (Capped, NotReady):
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
            why = real_failing_run(code, out, timed_out)
            if evidence:  # R63: a real passing run, then the unchanged empty-implementation check
                if why == "passed":
                    reason = self._empty_impl_check(t, changed)
                elif why:
                    reason = f"tests rejected: no real passing run ({why})"
                else:
                    reason = ("tests rejected: evidence tests fail on the current code (re-cut as an ordinary "
                              "task that fixes the gap): " + (out or "")[-1500:])
            elif why == "passed":
                reason = "tests rejected: weak (they pass before the feature exists)"
            elif why:
                reason = "tests rejected: no real failing run (timed out or no tests ran)"
            else:  # R4 again on an empty implementation (layer-1 design 3.1)
                reason = self._empty_impl_check(t, changed)
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
        focus_limit = self._focus_limit()
        used = self._focus_used(t)
        if used >= focus_limit:  # D-026: the budget is spent (e.g. no handoff recorded yet): hand off, launch nothing
            self._after_failure(tid, self._focus_reason(focus_limit), "focus-limit", "", handoff=True, focus=True)
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
        if t.get("evidence") is True:  # R63
            prompt += ("\nEVIDENCE TASK (R63): the tests already pass on the current code. Change nothing unless a "
                       "test or judge fails; then answer done.\n")
        if t.get("review_feedback"):
            prompt += "\nREVIEW FEEDBACK:\n" + "\n".join(f"- {x}" for x in t["review_feedback"]) + "\n"
        if t.get("trouble_notes"):
            prompt += "\nTROUBLESHOOTER NOTES:\n" + "\n".join(t["trouble_notes"]) + "\n"
        prompt += ("\nAnswer with JSON: {\"status\": \"done\" | \"blocked\", \"summary\": \"...\"}. "
                   "A blocked answer must also include all four of: \"tried\" (at least 2 different routes you "
                   "actually tried), \"error\" (the real error output), \"capability\" (the capability-map name you "
                   "need, or a new short name) and \"meanwhile\" (what you will work on instead); without all four "
                   "it is rejected as an easy way out.")
        remaining = focus_limit - used
        try:
            r = self._call("builder", prompt, S_BUILD, cwd=twt, needs=t.get("needs") or [], timeout_s=remaining)
        except (Capped, NotReady):  # R37: release the claim and undo, no failure recorded
            self._apply(f"{tag}-release", "release", cid, "forge-core")
            self._reset_to(twt, base)
            raise
        spent = self.last_run_s  # D-026 focus time: builder run time only, never waits or probes
        committed = getattr(self, "last_agent_commit", None)
        self._update(tid, focus_s=used + spent)

        tests = [_norm(x) for x in t["test_files"]]
        changed = self._changed(cwd=twt)
        violations = [f for f in changed if f in tests]
        if violations:
            _git(twt, "checkout", "-q", "HEAD", "--", *violations)
            _git(twt, "clean", "-q", "-f", "--", *violations)
        changed = [f for f in changed if f not in tests]
        out_of_scope = [f for f in changed if not any(fnmatch.fnmatch(f, pat) for pat in t["files_in_scope"])]

        def fail(reason: str, sig: str, output: str = "", submitted: bool = False,
                 payload: dict | None = None, handoff: bool = False, focus: bool = False) -> None:
            self._reset_to(twt, base)
            if submitted:
                self._apply(f"{tag}-fail", "fail", cid, "forge-auditor", payload)
            else:
                self._apply(f"{tag}-release", "release", cid, "forge-core")
            if self._ledger().contracts().get(cid, {}).get("status") == "failed":
                self._apply(f"{tag}-reopen", "reopen", cid, "forge-manager")
            self._after_failure(tid, reason, sig, output, handoff=handoff, focus=focus)

        if committed is not None:  # Live-run P1: the commit was already undone; the attempt is out of scope
            hit = [f for f in committed if f in tests]
            why = "out of scope: agent commit rejected (reset to base): " + (", ".join(committed) or "no files")
            if hit:
                why += "; touched test files: " + ", ".join(hit)
            return fail(why, "out of scope: agent commit: " + ",".join(committed))
        exceeded = spent >= remaining
        if exceeded and not (r.ok and (r.data or {}).get("status") == "blocked"):
            # D-026: 20 minutes of builder time without passing: the Troubleshooter takes it. A blocker claim is
            # still checked first (it is a handoff of its own, and a rejected one is an easy-out on record).
            return fail(self._focus_reason(focus_limit), "focus-limit", str((r.error or "") if not r.ok else ""),
                        handoff=True, focus=True)
        if not r.ok:
            return fail(f"builder output unusable: {r.error}", f"builder-error:{r.error}")
        if (r.data or {}).get("status") == "blocked":
            d = r.data or {}
            summary = d.get("summary") or "no detail"
            rejected = self._check_blocker(t, d, tag, cid, twt, base)  # Capped/NotReady: undone, re-raised
            if rejected:  # D-031: every rejection is an easy-out, logged against the builder
                self._record_easy_out(tid, tag, cid, rejected, d, summary)
                return fail(rejected, "easy-out", handoff=exceeded, focus=exceeded)
            tried, err, cap = d.get("tried"), d.get("error"), d["capability"]
            cur = self._task(tid)
            needs = list(cur.get("needs") or [])
            if cap not in needs:  # gating holds the builder; routing fixes it or asks Ben (even with no check)
                needs.append(cap)
            self._update(tid, needs=needs, notes=cur["notes"] + [
                f"blocker accepted: needs {cap}; meanwhile: {d['meanwhile'].strip()}"])
            # handoff: the first accepted blocker goes to the Troubleshooter now, not after a second failure the
            # readiness gate would never let happen
            return fail(f"blocker: {summary} (tried: {'; '.join(map(str, tried))}; error: {err})", f"blocker:{summary}",
                        output=str(err), handoff=True, focus=exceeded)
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
            started = time.monotonic()
            try:
                results = [("task tests", *self._run_tests(t, cwd=jw)[:2])]
                baseline = time.monotonic() - started
                for cmd in task_judge_cmds(self.judge_cmds, base, sha, t.get("test_files") or [],
                                           self._unbuilt_test_files(t["id"])):
                    if results[-1][1] != 0:
                        break
                    results.append((cmd, *self._run_cmd(cmd, cwd=jw)))
            except Stopped:  # R42/R49: KILL during a judge: undone exactly like a stopped reviewer, never a failure
                self._apply(f"{tag}-withdraw", "withdraw", cid, "forge-core")
                self._reset_to(twt, base)
                raise
            for cmd, code, output in results:
                if code != 0:
                    self._apply(f"{tag}-ci", "test_run", cid, "ci",
                                {"run_id": f"{tag}-ci", "commit": sha, "passed": False})
                    tail = "\n".join(output.splitlines()[-20:])
                    # numbers (timings, line numbers, addresses) vary between identical failures; ignore them
                    sig = hashlib.sha256(re.sub(r"\d+", "N", tail).encode()).hexdigest()
                    return fail(f"judge failed: {cmd}", sig, tail, submitted=True)
            self._apply(f"{tag}-ci", "test_run", cid, "ci", {"run_id": f"{tag}-ci", "commit": sha, "passed": True})
            # D-038: always before the reviewer, on the builder's own lines, at exactly S (never the layer checkout)
            mres = self._mutation_judge(t, base, sha, baseline, jw)
        mutation = mres.as_dict()

        diff = _git(twt, "diff", f"{base}..{sha}")
        try:
            rv = self._call("reviewer", role_text(self.repo, "reviewer") + "\n\nCheck this change against the task. "
                                        "Reject shortcuts, bare-minimum work, drift from the task, and anything that "
                                        "weakens tests.\n\n" + self._task_prompt(t) + self._evidence_review(t, sha, twt) +
                            "\nDIFF:\n" + diff[:60000] +
                            self._mutation_evidence(mres) +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW,
                            cwd=twt)  # the task worktree is at exactly S
        except (Capped, NotReady):  # R37 / Codex review P1: the reviewer could not run (unready, capped or
            # stopped). Undo the submitted attempt without counting it: no ledger attempt, no false claim, no
            # failure signature. A "fail" here would park contracts during a reviewer outage.
            self._apply(f"{tag}-withdraw", "withdraw", cid, "forge-core")
            self._reset_to(twt, base)
            raise
        if not rv.ok:
            return fail(f"reviewer output unusable: {rv.error}", f"reviewer-error:{rv.error}", submitted=True,
                        payload={"verdict": None, "reasons": [], "mutation": mutation, "gate": "reviewer_error"})
        verdict = (rv.data or {}).get("verdict")
        verdict = verdict if verdict in ("pass", "fail") else None
        given = (rv.data or {}).get("reasons")
        given = [str(x) for x in given] if isinstance(given, list) else []
        survivor_feedback = [f"surviving mutant {m.id}: {m.original} -> {m.replacement} at line {m.line}: "
                             "add or strengthen code so the tests catch it" for m in mres.survivors]
        if not mres.passed:  # the mutation gate fails the attempt whatever the verdict
            ids = mres.survivor_ids()
            reason = "mutation gate: " + mres.reason + ("; survivors: " + ", ".join(ids) if ids else "")
            sig = (hashlib.sha256(("mutation:" + "|".join(sorted(ids))).encode("utf-8")).hexdigest()
                   if mres.complete else "mutation-incomplete")
            self._update(tid, review_feedback=survivor_feedback + given)
            return fail(reason, sig, self._survivor_listing(mres), submitted=True,
                        payload={"verdict": verdict, "reasons": given, "mutation": mutation, "gate": "mutation"})
        if verdict != "pass":
            reasons = given or ["no reasons given"]
            self._update(tid, review_feedback=survivor_feedback + reasons)
            return fail("review failed: " + "; ".join(reasons), "review:" + "|".join(reasons), submitted=True,
                        payload={"verdict": verdict, "reasons": given, "mutation": mutation, "gate": "review"})

        # Reviewed: hand S to the crash-safe finalizer. The ledger pass is applied only there, after the push.
        # The evidence is exactly the pass payload the build stage prepares (P1B1): verdict, reasons, mutation.
        evidence = {"verdict": "pass", "reasons": given, "mutation": mutation}
        Journal(self.state).begin(tid, cid, sha, base, f"{tag}-ci", {"verdict": "pass", "reasons": given}, evidence)
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
        cand = next(c for c in rec.get("candidates") or [] if c.get("sha") == sha)
        cmds = task_judge_cmds(self.judge_cmds, cand["base"], sha, mine[0].get("test_files") if mine else [],
                               self._unbuilt_test_files(mine[0]["id"] if mine else ""))
        with self.trees.throwaway(sha) as jw:
            checks = [(f"tests of {t['id']}", lambda t=t: self._run_tests(t, cwd=jw)[:2]) for t in mine + done]
            checks += [(cmd, lambda cmd=cmd: self._run_cmd(cmd, cwd=jw)) for cmd in cmds]  # R57: fast
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

    def _mutation_judge(self, t: dict, base: str, sha: str, baseline: float, root: Path):
        """Layer-1 design 3.4: mutate the builder's changed in-scope lines (base..sha) and run the task tests on each
        mutant, in `root`, a throwaway worktree at exactly sha. It must be exactly sha afterwards (else an R13
        stage error)."""
        changed = self._mutation_targets(t, base, sha, root)
        timeout = float(self.limits.get("test_timeout_s", 600))
        t0 = self.clock()
        try:
            mres = self._run_mutation(root, changed, t, timeout, baseline)
            if t.get("evidence") is True and mres.total == 0:  # R63: no mutants proves nothing
                mres.passed = False
                mres.reason = ("evidence task: no mutants were generated from the in-scope code its tests "
                               "name, so the tests prove nothing about it")
        finally:
            self._activity().add(self._since(t0))  # T1C4: judges are active work
        if self._changed(cwd=root) or _git(root, "rev-parse", "HEAD") != sha:
            self._reset_to(root, sha)
            raise RuntimeError("mutation left changes")
        return mres

    def _mutation_targets(self, t: dict, base: str, sha: str, root: Path) -> dict[str, set[int]]:
        """Changed in-scope lines (base..sha). R63: an evidence task also targets every line of each in-scope
        function or method its test files name as a word; if nothing is selected, every line of the in-scope .py
        files."""
        tests = {_norm(x) for x in t["test_files"]}

        def in_scope(f: str) -> bool:
            return f not in tests and any(fnmatch.fnmatch(f, pat) for pat in t["files_in_scope"])

        diff = _git(root, "diff", "-U0", f"{base}..{sha}")
        out: dict[str, set[int]] = {f: set(lines) for f, lines in changed_lines(diff).items() if in_scope(f)}
        if t.get("evidence") is not True:
            return out

        def is_test(f: str) -> bool:  # R63: evidence never mutates any test file, this task's or another's
            return f.startswith("tests/") or "/tests/" in f or Path(f).name.startswith("test_")

        out = {f: lines for f, lines in out.items() if not is_test(f)}
        words = set()
        for f in tests:
            try:
                words |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", (root / f).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                pass
        files = [_norm(x) for x in _git(root, "ls-files").splitlines() if x.strip()]
        files = [f for f in files if f.endswith(".py") and in_scope(f) and not is_test(f)]
        named: dict[str, set[int]] = {}
        texts: dict[str, str] = {}
        for f in files:
            try:
                texts[f] = (root / f).read_text(encoding="utf-8")
                tree = ast.parse(texts[f])
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in words:
                    named.setdefault(f, set()).update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if not named:  # the tests name no in-scope code: target it all
            named = {f: set(range(1, len(texts[f].splitlines()) + 1)) for f in texts}
        for f, lines in named.items():
            out.setdefault(f, set()).update(lines)
        return out

    def _run_mutation(self, root: Path, changed: dict, t: dict, timeout: float, baseline: float):
        return run_mutation(root, changed,
                            [sys.executable, "-m", "unittest", *(parse_test_cmd(t["test_cmd"], t["test_files"]) or [])],
                            mutation_min=float(self.limits.get("mutation_min", 0.8)),
                            budget_s=float(self.limits.get("mutation_budget_s", self.limits.get("test_timeout_s", 600))),
                            per_mutant_timeout_s=min(timeout, max(5.0, 3 * baseline)),
                            max_mutants=int(self.limits.get("mutation_max_mutants", 25)))

    @staticmethod
    def _survivor_listing(mres) -> str:
        if not mres.survivors:
            return "- survivors: none"
        return "\n".join(f"- {m.id}: {m.original} -> {m.replacement} survived (line {m.line})" for m in mres.survivors)

    def _mutation_evidence(self, mres) -> str:
        return ("\nMUTATION EVIDENCE:\n" + mres.reason +
                f"\nscore: {mres.score:.2f} (mutation_min {float(self.limits.get('mutation_min', 0.8)):.2f})"
                f"\ncomplete: {'yes' if mres.complete else 'no'}\n" + self._survivor_listing(mres) + "\n")

    def _check_blocker(self, t: dict, d: dict, tag: str, cid: str, twt: Path, base: str) -> str | None:
        """D-031: None when a blocked claim is accepted, else the rejection reason. Checked in order: evidence,
        the capability map, then the reviewer. A capped or unready reviewer undoes the attempt and re-raises."""
        tried, err, cap, meanwhile = d.get("tried"), d.get("error"), d.get("capability"), d.get("meanwhile")
        routes = {x.strip() for x in tried if isinstance(x, str) and x.strip()} if isinstance(tried, list) else set()
        if not (len(routes) >= 2 and isinstance(err, str) and err.strip() and isinstance(cap, str)
                and readiness.NAME_RE.match(cap) and isinstance(meanwhile, str) and meanwhile.strip()):
            return "blocker rejected: no evidence (easy out)"
        m = self._cap_map()
        entry = m.get(cap)
        if readiness.broken(entry, self.clock(), readiness.max_age_for(cap, self.limits)) is None:
            return (f"blocker rejected: contradicts capability map ({cap} is ok: {entry.get('detail', '')}, "
                    f"checked {entry.get('checked_at')})")
        if entry is not None:
            shown = json.dumps(entry, sort_keys=True)
        elif cap in self.checks or cap in self.probes:
            shown = "no evidence yet"
        else:
            shown = "no automatic check exists"
        claim = {k: d.get(k) for k in ("summary", "tried", "error", "capability", "meanwhile")}
        try:
            rv = self._call("reviewer", role_text(self.repo, "reviewer") + "\n\nThe builder claims it is BLOCKED and cannot "
                                        "finish this task. Check the claim, not the code: fail claims without real "
                                        "attempts. Pass only if it shows at least 2 genuinely different routes that were "
                                        "actually tried, a real error, and a need that truly can't be met with what "
                                        "Forge has.\n\n" + self._task_prompt(t) +
                            "\nBLOCKER CLAIM:\n" + json.dumps(claim, indent=2)[:8000] +
                            f"\n\nCAPABILITY MAP ENTRY for {cap}: {shown}\n\nTHE ATTEMPT'S CHANGES:\n" +
                            self._attempt_diff(twt, base)[:40000] +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW,
                            cwd=twt)  # the attempt's work is in the task worktree, never the layer checkout
        except (Capped, NotReady):  # R37: undone, no failure, no easy-out, and not counted (the reviewer's outage)
            self._apply(f"{tag}-withdraw", "withdraw", cid, "forge-core")
            self._reset_to(twt, base)
            raise
        if not rv.ok or (rv.data or {}).get("verdict") != "pass":
            reasons = [str(x) for x in ((rv.data or {}).get("reasons") or [rv.error or "no reasons given"])]
            return "blocker rejected by reviewer: " + "; ".join(reasons)
        return None

    def _attempt_diff(self, twt: Path, base: str) -> str:
        """The attempt's uncommitted work in the task worktree: tracked changes as a diff, new files with their
        (capped) text."""
        out = _git(twt, "diff", base, check=False)
        for f in self._changed(cwd=twt):
            p = twt / f
            if p.is_file() and not _git(twt, "ls-files", "--", f, check=False):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = "(unreadable)"
                out += f"\n--- new file: {f}\n{text[:4000]}"
        return out or "(no changes)"

    def _record_easy_out(self, tid: str, tag: str, cid: str, reason: str, d: dict, summary: str) -> None:
        """D-031: a rejected blocker claim, logged against the builder and reported to the ledger before the
        claim is released."""
        cap = d.get("capability") if isinstance(d.get("capability"), str) else None
        with (self.state / "easy_outs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"task": tid, "agent": "builder", "kind": "easy_out", "reason": reason,
                                "capability": cap, "summary": str(summary)[:NOTE_CAP],
                                "at": self.clock().isoformat()}) + "\n")
        self._apply(f"{tag}-report", "run_report", cid, "forge-core", {
            "run_id": tag, "claim": "blocked", "commit": None, "changed": [], "violations": [], "out_of_scope": [],
            "easy_out": {"reason": reason, "capability": cap}})

    def _is_ancestor(self, sha: str) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=str(self.wt),
                              capture_output=True, **NOWIN).returncode == 0

    def _focus_limit(self) -> float:
        try:
            return float(self.limits.get("builder_focus_s", 1200))
        except (TypeError, ValueError):
            return 1200.0

    @staticmethod
    def _focus_used(t: dict) -> float:
        v = t.get("focus_s") or 0
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0.0

    @staticmethod
    def _focus_reason(limit: float) -> str:
        return f"focus limit: {limit / 60:g} minutes of builder time without passing (D-026); handed to the Troubleshooter"

    def _after_failure(self, tid: str, reason: str, sig: str, output: str, handoff: bool = False,
                       focus: bool = False) -> None:
        t = self._task(tid)
        sigs = t["fail_signatures"] + [sig]
        fails = t.get("fails_since", 0) + 1
        t = self._update(tid, fail_signatures=sigs, fails_since=fails, notes=t["notes"] + [reason])
        zero_progress = len(sigs) >= 2 and sigs[-1] == sigs[-2]
        rounds = t.get("troubleshoots", 0)
        if rounds >= 3:
            if fails >= 2 or focus:  # D-026: focus spent and no troubleshooter round left
                self._block(tid, reason)
            return
        if zero_progress or fails >= 2 or handoff:
            try:
                self._troubleshoot(tid, reason, output)
            except (Capped, NotReady):  # R38: kept, and run before the next builder attempt
                self._update(tid, troubleshoot_pending={"reason": str(reason)[:NOTE_CAP],
                                                        "output": str(output)[-4000:]})
                raise

    def _attach_review_notes(self, tasks: list, notes, plan_file: str) -> list:
        """R44: the plan reviewer's non-blocking notes are appended to the tasks they affect (and the plan file)."""
        if not isinstance(notes, list):
            return tasks
        ids = {str(x.get("id")) for x in tasks}
        clean: list[tuple[str, str]] = []  # (task id or "" for plan-wide, note), in the reviewer's order
        for n in notes:
            if not isinstance(n, dict):
                continue
            text = str(n.get("note") or "").strip()[:NOTE_CAP]
            if text:
                task = str(n.get("task") or "")
                clean.append((task if task in ids else "", text))
        if not clean:
            return tasks
        out = []
        for x in tasks:
            x = dict(x)
            mine_or_wide = [(k, m) for k, m in clean if k in ("", str(x.get("id")))][:10]  # the first 10, in order
            mine = [m for k, m in mine_or_wide if k]
            wide = [m for k, m in mine_or_wide if not k]
            if mine:
                x["section"] = str(x["section"]) + "\n\nREVIEWER NOTES (handle and test these):\n" + \
                    "\n".join(f"- {m}" for m in mine)
            if wide:
                x["section"] = str(x["section"]) + "\n\nPLAN-WIDE REVIEWER NOTES:\n" + \
                    "\n".join(f"- {m}" for m in wide)
            out.append(x)
        lines = [f"- {k or '(plan-wide)'}: {m}" for k, m in clean]
        f = self.wt / plan_file
        f.write_text(f.read_text(encoding="utf-8").rstrip("\n") + "\n\n## Reviewer notes\n\n" + "\n".join(lines) +
                     "\n", encoding="utf-8")
        return out

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
            r = self._call("troubleshooter", prompt, S_TROUBLE, cwd=tw, scratch=True)
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
        self._update(tid, trouble_notes=notes, troubleshot=True, fails_since=0, focus_s=0,
                     troubleshoots=t.get("troubleshoots", 0) + 1, troubleshoot_pending=None)

    # ------------------------------------------------------------------ drift
    def _drift_check(self) -> None:
        """Design §3.7 / D-026: the drift keeper, after every merge and whenever a stall rule fired. It sees the
        tasks, the spec coverage map and the stall facts. A stall forces a re-plan whatever it answers. The
        re-plan is saved durably before drift_due and the stall are cleared (T1C5)."""
        q = self._queue()
        design = self.wt / self._spec_rel()  # R61: the lane's own design
        text = design.read_text(encoding="utf-8") if design.exists() else "(no design file)"
        listing = "\n".join(f"- {t['id']} [{t['status']}] {t['title']}" for t in q["tasks"])
        d0 = drift_mod.load(self.state) or {}
        trigger = (d0.get("stall") or {}).get("trigger")
        facts = self._coverage_block(q) + (f"\n\nSTALL RULE FIRED: {trigger}. A re-plan is required (D-026); give the "
                                           "reasons the work stalled and what the re-plan must change." if trigger else "")
        try:
            r = self._call("drift_keeper", role_text(self.repo, "drift_keeper") + "\n\nIs this work still on course "
                                           "for the design? Say replan only if it is drifting. Coverage must rise with "
                                           "every merge.\n\nTASKS:\n" + listing + "\n\n" + facts +
                           "\n\nDESIGN:\n" + text[:40000] +
                           "\nAnswer with JSON: {\"status\": \"ok\" | \"replan\", \"reasons\": [...]}", S_DRIFT)
        except Capped:  # R42: a stopped run leaves no edits behind
            if self.wt.exists():
                self._reset_wt()
            raise
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
        reasons = (r.data or {}).get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        reasons = [str(x) for x in reasons]
        d = drift_mod.load(self.state) or drift_mod.adopt([], [], False, self._activity().total())
        trigger = (d.get("stall") or {}).get("trigger")
        replan = status == "replan" or bool(trigger)
        use_manager = replan and getattr(self, "manager", None) is not None
        if replan:
            if use_manager and not d.get("replan"):
                drift_mod.new_replan(d, ([f"stall rule: {trigger}"] if trigger else []) + reasons,
                                     trigger or "drift keeper")
            d["stall"] = None
            drift_mod.restart_window(d, self._activity().total())
            drift_mod.save(self.state, d)  # durable before drift_due is cleared: a crash never loses the re-plan
            self._crash("after:replan-pending")
        q = self._queue()
        q["drift_due"], q["drift_failures"] = False, 0
        self._save_queue(q, durable=True)
        if replan and not use_manager:  # no Manager configured: pause and ask Ben, as before
            (self.state / "PAUSED").write_text("drift keeper asked for a re-plan\n")
            self._ask("replan", "Forge paused: the drift keeper wants a re-plan",
                      ("Stall rule: " + trigger + "\n\n" if trigger else "") + "Reasons:\n" +
                      "\n".join(f"- {x}" for x in reasons) + "\n\nReply with guidance to resume.")

    # ------------------------------------------------------------------ coverage, stall rules, re-plans (T1C5)
    def _spec_rel(self) -> str:
        """R61: the design this layer is built against. A lane's queue may name its own ("spec_file", set by
        init --spec); otherwise limits["spec_file"], else the Layer 1 design."""
        q = self._queue()
        return _norm(str(q.get("spec_file") or self.limits.get("spec_file", "docs/specs/layer-1-design.md")))

    def _spec(self) -> tuple[str, dict] | None:
        """(spec text, requirements) from the layer worktree, or None when there is no usable spec."""
        rel = self._spec_rel()
        if ".." in rel or re.match(r"^([A-Za-z]:|/)", rel):
            return None
        try:
            text = (self.wt / rel).read_text(encoding="utf-8")
            reqs = cov_mod.parse_requirements(text)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return (text, reqs) if reqs else None

    def _coverage_block(self, q: dict) -> str:
        spec = self._spec()
        if spec is None:
            return "COVERAGE: unavailable (no spec with numbered sections)"
        tasks = q["tasks"]
        cov = cov_mod.compute(spec[1], tasks, cov_mod.verified_done(tasks, self._ledger()))
        d = drift_mod.load(self.state) or {}
        return ("COVERAGE (verified by the ledger; requirement id [status] text):\n" + cov.report(20000) +
                f"\nMerges in a row without coverage gain: {d.get('no_gain', 0)}")

    def _int_limit(self, key: str, default: float) -> float:
        try:
            return float(self.limits.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _drift_bookkeeping(self, q: dict) -> tuple[dict, set]:
        """Every step, plain code: adopt the baseline on first use, record each newly marked merge exactly once
        (gain measured on the same task set, only ledger-verified work counts), then check the stall rules.
        Returns (drift state, merges deferred because their finalization is still running)."""
        activity = self._activity().total()
        tasks = q["tasks"]
        marks = [x for x in q.get("drift_marks") or [] if isinstance(x, str)]
        verified = cov_mod.verified_done(tasks, self._ledger())
        d = drift_mod.load(self.state)
        dirty = d is None
        if d is None:
            done = {t["id"] for t in tasks if t.get("status") == "done"}
            d = drift_mod.adopt(marks, verified | done, bool(q.get("drift_due")), activity)
        journal = Journal(self.state)
        deferred = set()
        for tid in marks:
            if tid not in d["counted"]:
                rec = journal.load(tid)
                if rec is not None and rec.get("status") == "active":
                    deferred.add(tid)
        spec = self._spec()
        score = None
        if spec is not None:
            reqs = spec[1]

            def score(done: set, reqs=reqs):
                return cov_mod.compute(reqs, tasks, done).score
        if drift_mod.record_merges(d, marks, verified, activity, score, deferred):
            dirty = True
        if not d.get("stall") and not d.get("replan"):
            n = int(self._int_limit("drift_no_gain_merges", 3))
            idle_s = self._int_limit("drift_no_merge_active_s", 7200)
            work_left = any(t.get("kind", "build") == "build" and t.get("status") in ("todo", "tests_ok", "merge_pending")
                            for t in tasks)
            if spec is not None and drift_mod.no_gain_due(d, n):
                d["stall"] = {"trigger": f"no coverage gain in {n} merges", "active_s": activity}
            elif work_left and drift_mod.idle_due(d, activity, idle_s):
                d["stall"] = {"trigger": f"no merge in {idle_s / 3600:g} active hours", "active_s": activity}
            dirty = dirty or bool(d.get("stall"))
        if dirty:
            drift_mod.save(self.state, d)
        return d, deferred

    def _drift_busy(self) -> bool:
        d = drift_mod.load(self.state) or {}
        return bool(d.get("stall") or d.get("replan"))

    def _replan_stage(self) -> None:
        """The Manager re-plans from the ledger and the spec only, in a throwaway checkout it may not change.
        Its proposal is validated completely, reviewed, validated again at acceptance and appended in one durable
        queue write that also records the re-plan id; only then is the pending re-plan cleared."""
        d = drift_mod.load(self.state)
        rp = (d or {}).get("replan")
        if not rp:
            return
        q = self._queue()
        if rp["id"] in (q.get("replans") or []):  # accepted before a crash: just finish the bookkeeping
            self._finish_replan(d)
            return
        spec = self._spec()
        if getattr(self, "manager", None) is None:
            return self._escalate_replan(d, "no Manager is configured")
        if spec is None:
            return self._escalate_replan(d, "there is no usable spec to plan from")
        if int(d.get("auto_replans", 0)) >= int(self._int_limit("max_auto_replans", 2)):
            return self._escalate_replan(d, "re-plans keep happening without coverage rising")
        text, reqs = spec
        led = self._ledger()
        tasks = q["tasks"]
        cov = cov_mod.compute(reqs, tasks, cov_mod.verified_done(tasks, led))
        prompt = manager_mod.build_prompt(text, cov, manager_mod.ledger_rows(led), rp.get("reasons") or [])
        tip = _git(self.wt, "rev-parse", "HEAD")
        with self.trees.throwaway(tip) as mw:
            r = self._call("manager", prompt, manager_mod.S_MANAGER, cwd=mw)
            wrote = self._changed(cwd=mw)
        problems: list[str] = []
        new: list[dict] = []
        if not r.ok:
            problems = [f"manager failed ({r.error})"]
        elif wrote:
            problems = ["manager is read-only but changed: " + ", ".join(wrote[:20])]
        else:
            existing = {t["id"] for t in tasks} | set(led.contracts())
            new, problems = manager_mod.validate_proposal(r.data, reqs, existing)
        if not problems:
            rv = self._call("reviewer", role_text(self.repo, "reviewer") + "\n\nCheck this RE-PLAN from the Manager "
                                        "against the spec and the coverage map: it must make coverage rise, be "
                                        "testable, small tasks, no placeholders, no drift.\n\nWHY:\n" +
                            "\n".join(f"- {x}" for x in rp.get("reasons") or []) + "\n\n" + cov.report(20000) +
                            "\n\nPROPOSED TASKS:\n" + json.dumps(new, indent=1)[:40000] +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW)
            if not rv.ok or (rv.data or {}).get("verdict") != "pass":
                problems = ["replan review failed: " + "; ".join(
                    str(x) for x in ((rv.data or {}).get("reasons") or [rv.error or "no reasons given"]))]
        if not problems:
            problems = self._accept_replan(rp, new)
        if problems:
            self._reject_replan(problems)

    def _accept_replan(self, rp: dict, new: list[dict]) -> list[str]:
        """Validate once more against the queue and ledger as they are now, then append in one durable write."""
        spec = self._spec()
        if spec is None:
            return ["the spec is no longer usable"]
        q = self._queue()
        existing = {t["id"] for t in q["tasks"]} | set(self._ledger().contracts())
        clean, problems = manager_mod.validate_proposal({"tasks": new}, spec[1], existing)
        if not problems:
            problems = [f"task {x['id']!r}: {why}" for x in clean for why in [validate_task(dict(x, kind="build"))] if why]
        if problems:
            return problems
        for x in clean:
            nt = self._new_task(x)
            nt["kind"], nt["status"] = "build", "todo"
            q["tasks"].append(nt)
        q["replans"] = [str(i) for i in q.get("replans") or []][-200:] + [rp["id"]]
        q["notes"] = [str(n)[:NOTE_CAP] for n in q.get("notes") or []] + [
            f"re-plan {rp['id']} ({rp.get('trigger')}) added: " + ", ".join(x["id"] for x in clean)]
        self._save_queue(q, durable=True)
        self._crash("after:replan-accepted")
        self._finish_replan(drift_mod.load(self.state))
        return []

    def _finish_replan(self, d: dict) -> None:
        d["replan"] = None
        d["auto_replans"] = int(d.get("auto_replans", 0)) + 1
        drift_mod.restart_window(d, self._activity().total())
        drift_mod.save(self.state, d)

    def _reject_replan(self, problems: list[str]) -> None:
        d = drift_mod.load(self.state)
        rp = d["replan"]
        rp["attempts"] = int(rp.get("attempts", 0)) + 1
        rp["notes"] = ([str(x)[:NOTE_CAP] for x in rp.get("notes") or []] + [str(x)[:NOTE_CAP] for x in problems])[-NOTES_KEEP:]
        if rp["attempts"] >= 2:
            return self._escalate_replan(d, "the Manager's proposals were rejected twice")
        drift_mod.save(self.state, d)

    def _escalate_replan(self, d: dict, why: str) -> None:
        """PAUSED and a replan question to Ben; the pending re-plan is cleared only after both exist."""
        rp = d.get("replan") or {}
        (self.state / "PAUSED").write_text("a re-plan needs Ben\n")
        self._ask("replan", "Forge paused: a re-plan needs you",
                  f"Forge needs a re-plan and can't make one itself: {why}.\n\nTrigger: {rp.get('trigger')}\n\n"
                  "Reasons:\n" + "\n".join(f"- {x}" for x in rp.get("reasons") or []) +
                  ("\n\nRejected proposals:\n" + "\n".join(f"- {x}" for x in rp.get("notes") or []) if rp.get("notes") else "") +
                  "\n\nReply with guidance to resume.")
        d["replan"] = None
        d["auto_replans"] = 0
        drift_mod.restart_window(d, self._activity().total())
        drift_mod.save(self.state, d)

    # ------------------------------------------------------------------ plan tasks
    def _plan_stage(self, tid: str) -> None:
        t = self._task(tid)
        self._reset_wt()
        plan_file = _norm(t["plan_file"])
        spec = None if t.get("no_coverage") else self._spec()  # R62
        reqs = spec[1] if spec else {}
        cover_text = ("Each task must also list \"covers\": the ids of the layer spec requirements below that "
                      "its acceptance tests prove (at least one, no repeats). Claim only what the task's section "
                      "states as acceptance criteria.\nREQUIREMENTS (id: text):\n" +
                      "\n".join(f"{k}: {v}" for k, v in reqs.items()) + "\n") if reqs else ""
        try:
            prior = [str(n) for n in t.get("notes", [])
                     if str(n).startswith(("plan rejected", "plan review failed"))][-PLAN_MEMORY_NOTES:]  # R47
            while prior and sum(len(n) for n in prior) > PLAN_MEMORY_CHARS:
                prior = prior[1:]
            r = self._call("planner", role_text(self.repo, "planner") + "\n\nWrite the implementation plan to " + plan_file +
                           " (and no other file), then return its tasks.\n\n" + self._task_prompt(t) +
                           "\nEach task needs: id, title, section, files_in_scope, test_files, test_cmd.\n"
                           "Each task may also list \"needs\": capability names from the capability map (git, github, "
                           "claude, codex, gmail, docker, n8n, ollama, python_libs, browser, or a new name) that its "
                           "Builder needs beyond git and its own AI.\n"
                           "Each task may also list \"depends_on\": ids of earlier tasks in this plan whose code it "
                           "uses. It is not started (tests or build) until those are done and merged, and its "
                           "builder works on top of their code (R55).\n" + cover_text +
                           "A task may set \"evidence\": true (R63) when the existing code already meets its "
                           "requirements: its tests must then pass on the current code and prove them, and the "
                           "builder changes nothing. Requirements the code does not meet yet get ordinary tasks.\n"
                           "IMPORTANT (R41): each task's section is the ONLY instruction the test writer and the "
                           "builder will see. Make it complete and self-contained: what to build, exact interfaces "
                           "and signatures, behaviour, edge cases, dependencies on earlier tasks, and the acceptance "
                           f"criteria the tests must check (at least {MIN_SECTION_CHARS} characters). Every task must "
                           "be fully doable by a builder that may change ONLY its files_in_scope: no steps for Ben, "
                           "the conductor, or files outside that scope.\n"
                           "Before answering (R45), check every task against the numbered rules in "
                           "docs/specs/bootstrap-conductor.md and the decisions in docs/DECISIONS.md: any contradiction "
                           "with them will be rejected as blocking.\n" +
                           ("\nYOUR EARLIER ATTEMPTS WERE REJECTED FOR (fix ALL of these; none may come back):\n" +
                            "\n".join(prior) + "\n" if prior else "") +
                           "Answer with JSON: {\"tasks\": [...]}", S_PLAN)
        except (Capped, NotReady):
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
        elif any(len(str(x.get("section", ""))) < MIN_SECTION_CHARS for x in tasks):  # R41
            thin = [f"{x.get('id')} ({len(str(x.get('section', '')))} chars)" for x in tasks
                    if len(str(x.get("section", ""))) < MIN_SECTION_CHARS]
            reason = ("plan rejected: task section too thin (a task's section is its builder's only instructions; "
                      f"need at least {MIN_SECTION_CHARS} characters): " + ", ".join(thin))
        else:
            existing = {x["id"] for x in self._queue()["tasks"]}
            if any(x["id"] in existing for x in tasks):
                reason = "plan rejected: task ids clash with existing tasks"
        if not reason and reqs:  # R62
            reason = _covers_problem(tasks, reqs)
        if not reason and not reqs and isinstance(tasks, list):  # R62: ignored claims never reach the reviewer
            tasks = [{k: v for k, v in x.items() if k != "covers"} for x in tasks]
        claimed = ""
        if not reason and reqs:  # R62: the reviewer sees what each claimed requirement says
            ids = [c for x in tasks for c in x["covers"]]
            claimed = ("\nREQUIREMENTS CLAIMED IN covers (id: text):\n" +
                       "\n".join(f"{k}: {reqs[k]}" for k in dict.fromkeys(ids)) + "\n")
        if not reason:
            plan_text = (self.wt / plan_file).read_text(encoding="utf-8")
            size = len(plan_text) + len(json.dumps(tasks)) + len(claimed)
            if size > PLAN_REVIEW_MAX:  # R44: the reviewer must see the whole plan
                reason = f"plan rejected: plan too large for review ({size} characters); split this plan task"
        if not reason:
            try:
                rv = self._call("reviewer", role_text(self.repo, "reviewer") + "\n\nCheck this plan against the task and "
                                            "design: complete, testable, no placeholders, no drift.\n"
                                            "Fail ONLY for blocking problems (R44): a requirement of this plan task "
                                            "that no task covers; a task that contradicts docs/DECISIONS.md or the "
                                            "design; a task that can't be done within its files_in_scope; wrong "
                                            "ordering or dependencies between tasks; placeholders or thin sections" +
                                            ("; a task whose covers claims a requirement that no acceptance "
                                             "criteria in its section prove (R62: covers must be backed by "
                                             "matching acceptance criteria in the task's section, or it is "
                                             "blocking)" if reqs else "") + ". "
                                            "Edge cases, extra tests and implementation details are NOT reasons to "
                                            "fail: put each in task_notes against the task id it affects, and they "
                                            "will be added to that task's instructions.\n\n" +
                                self._task_prompt(t) + "\nPLAN:\n" + plan_text + "\nTASKS JSON:\n" +
                                json.dumps(tasks) + claimed +
                                "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...], "
                                "\"task_notes\": [{\"task\": \"<task id>\", \"note\": \"...\"}]}",
                                S_PLAN_REVIEW)
            except (Capped, NotReady):
                self._reset_wt()
                raise
            if not rv.ok or (rv.data or {}).get("verdict") != "pass":
                reason = "plan review failed: " + "; ".join(str(x) for x in ((rv.data or {}).get("reasons") or [rv.error]))
        if reason:
            self._reset_wt()
            rejects = t.get("plan_rejects", 0) + 1
            self._update(tid, notes=t["notes"] + [reason], plan_rejects=rejects)
            if rejects >= PLAN_ATTEMPTS:  # R45
                self._block(tid, reason)
            return
        tasks = self._attach_review_notes(tasks, (rv.data or {}).get("task_notes"), plan_file)  # R44
        self._commit([plan_file], f"{tid}: plan")
        q = self._queue()
        for x in q["tasks"]:
            if x["id"] == tid:
                x["status"] = "done"
        for x in tasks:
            nt = self._new_task({k: x[k] for k in TASK_FIELDS})
            nt["kind"], nt["status"] = "build", "todo"
            if isinstance(x.get("needs"), list):
                nt["needs"] = list(x["needs"])
            if x.get("evidence") is True:  # R63
                nt["evidence"] = True
            if reqs:  # R62: validated above; ignored without a spec or under no_coverage
                nt["covers"] = list(x["covers"])
            deps = x.get("depends_on")
            if isinstance(deps, list):  # R55: built only after these tasks are done (merged)
                nt["depends_on"] = [str(d) for d in deps if isinstance(d, str) and d != nt["id"]]
            q["tasks"].append(nt)
        self._save_queue(q)
        self._push()

    # ------------------------------------------------------------------ gate
    def _gate(self) -> None:
        if not self._gate_suite():  # R57: the full suite passes at the layer tip before any PR to main
            return
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


    def _gate_suite(self) -> bool:
        """R57: per-task judges run only the affected modules, so the layer gate runs every judge command as
        written (the drills and the FULL suite) at exactly the layer tip, once per tip. A failure is logged and
        told to Ben once per tip; no pull request is opened until a later tip passes."""
        if not self.judge_cmds:
            return True
        sha = _git(self.wt, "rev-parse", "HEAD")
        prev = self._queue().get("gate_suite") or {}
        if prev.get("sha") == sha:
            return bool(prev.get("passed"))
        failed = ""
        with self.trees.throwaway(sha) as jw:
            for cmd in self.judge_cmds:
                code, out = self._run_cmd(cmd, cwd=jw)  # Stopped propagates: nothing is recorded
                if code != 0:
                    failed = f"{cmd} failed (exit {code}):\n" + "\n".join(out.splitlines()[-30:])
                    break
        q = self._queue()
        q["gate_suite"] = {"sha": sha, "passed": not failed, "at": self.clock().isoformat()}
        self._save_queue(q)
        if failed:
            self._log(f"gate full suite failed at {sha}: {failed}"[:1000])
            self._send(f"[Forge] {q['layer']} is done but the full suite fails",
                       f"Before opening the pull request to main, Forge ran the full suite at {sha}. It failed:\n\n"
                       f"{failed[:3000]}\n\nNo pull request was opened. Forge runs it again when the layer changes.")
        return not failed

    # ------------------------------------------------------------------ R58: layers stay current with main
    def _repair_sync_approval(self, wt: Path, layer: str, head: str = "HEAD") -> None:
        """R58: a crash between git's merge commit and its registration leaves merges safe push would refuse
        forever. Every merge in the layer's history that is Forge's own "Sync <layer> with main" merge, or a merge
        that came from main (reachable from the local origin/main ref), is registered if it isn't. Runs on every
        sync call, before the rate limit, so a restart repairs at once even after later commits moved HEAD."""
        try:
            log = _git(wt, "log", "--merges", "--format=%H %P%x1f%s", head, check=False)
            main = _git(wt, "rev-parse", "--verify", "-q", "refs/remotes/origin/main^{commit}", check=False)
            from_main = set(_git(wt, "rev-list", "--merges", main).split()) if main else set()
            approved = ApprovedMerges(self.state)
            for line in log.splitlines():
                ids, _, subject = line.partition("\x1f")
                parts = ids.split()
                if len(parts) < 3:
                    continue
                sha, parents = parts[0], parts[1:]
                if approved.has(sha):
                    continue
                if subject == f"Sync {layer} with main" and len(parents) == 2:
                    approved.add(sha, {"kind": "main_sync", "base": parents[0], "other": parents[1],
                                       "parents": parents, "repaired": True})
                elif sha in from_main:
                    approved.add(sha, {"kind": "main", "repaired": True})
                else:
                    continue
                self._log(f"main sync: registered unregistered merge {sha[:12]} (interrupted sync)")
        except (RuntimeError, OSError) as e:
            self._log(f"main sync: approval repair failed: {e!r}"[:300])

    def _sync_busy(self, q: dict, wt: Path) -> str:
        """R58: why the layer can't take a main merge right now ("" when it can)."""
        if Journal(self.state).active():
            return "a finalization is active"
        if any(v.get("kind") == "gate" and v.get("status") == "open" for v in self._read("questions.json", {}).values()):
            return "a gate pull request is open"  # its tip was judged by the full suite: never move it under review
        contracts = self._ledger().contracts()
        for t in q.get("tasks", []):
            if t.get("status") == "tests_ok" and contracts.get(t["id"], {}).get("status") in ("claimed", "submitted"):
                return f"task {t['id']} has an active claim"
        for t in q.get("tasks", []):
            try:
                tp = self.trees.task_path(t["id"])
            except ValueError:
                continue
            if (tp / ".git").exists() and self._changed(cwd=tp):
                return f"task worktree {t['id']} has uncommitted work"
        if self._changed(cwd=wt):
            return "the layer worktree has uncommitted work"
        return ""

    def _sync_with_main(self) -> str:
        """R58: when nothing is mid-stage, merge origin/main into the layer (no-ff, by Forge) so new task
        worktrees start from current main code. Fetches at most every MAIN_SYNC_EVERY_S. A conflict is aborted
        and asked about once ("merge" question); the layer keeps its old base. Returns what happened."""
        try:
            q = self._queue()
            layer = q.get("layer") or ""
            wt = self.work / layer
            if not layer or not (wt / ".git").exists():
                return "no layer"
            self._repair_sync_approval(wt, layer)  # first, so a restart repairs even when rate-limited
            st = self._read("main_sync.json", {})
            now = self.clock()
            last = st.get("fetched_at")
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < MAIN_SYNC_EVERY_S:
                        return "rate limited"
                except (TypeError, ValueError):
                    pass
            if not _git(wt, "remote", "get-url", "origin", check=False):
                return "no origin"
            busy = self._sync_busy(q, wt)
            if busy:
                return "busy: " + busy
            st["fetched_at"] = now.isoformat()
            self._write("main_sync.json", st)
            try:
                _git(wt, "fetch", "-q", "origin", "main")
            except RuntimeError as e:
                self._log(f"main sync: fetch failed: {e}"[:500])
                return "fetch failed"
            main = _git(wt, "rev-parse", "--verify", "-q", "refs/remotes/origin/main^{commit}", check=False)
            head = _git(wt, "rev-parse", "HEAD")
            if not main or subprocess.run(["git", "merge-base", "--is-ancestor", main, head], cwd=str(wt),
                                          capture_output=True, stdin=subprocess.DEVNULL, **NOWIN).returncode == 0:
                return "current"  # nothing on main that the layer lacks
            qs = self._read("questions.json", {})
            c = st.get("conflict") or {}
            if c.get("main") == main and c.get("layer") == head and (qs.get(c.get("qid")) or {}).get("status") == "open":
                return "conflict pending"  # asked already; nothing has moved and Ben hasn't answered
            p = subprocess.run(["git", "-c", "user.name=Forge", "-c", "user.email=forge@localhost", "merge", "--no-ff",
                                "--no-edit", "-m", f"Sync {layer} with main", main], cwd=str(wt), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, **NOWIN)
            if p.returncode != 0:
                files = _git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
                _git(wt, "merge", "--abort", check=False)
                self._reset_to(wt, head)
                if not files:
                    self._log(f"main sync: merge of {main[:12]} failed: {(p.stdout + p.stderr).strip()}"[:500])
                    return "merge failed"
                self._log(f"main sync: {layer} conflicts with main {main[:12]} in {', '.join(files)}; merge aborted, "
                          f"keeping the old base"[:500])
                qid = next((k for k, v in qs.items() if v.get("kind") == "merge" and v.get("sync")
                            and v.get("status") == "open"), None)
                if qid is None:
                    qid = self._ask("merge", f"Forge: {layer} can't take the latest main (merge conflict)",
                                    f"Merging origin/main ({main[:12]}) into {layer} ({head[:12]}) conflicts in:\n"
                                    + "\n".join(f"- {f}" for f in files) +
                                    f"\n\nThe merge was aborted. {layer} keeps building on its old base. Resolve the "
                                    "conflict on the layer branch (or tell Forge what to do) and reply; Forge tries "
                                    "again after your answer or when main or the layer moves.",
                                    sync=True, main=main, layer_sha=head,
                                    default=f"{layer} keeps building on its old base")
                st["conflict"] = {"main": main, "layer": head, "qid": qid, "files": files}
                self._write("main_sync.json", st)
                return "conflict"
            new = _git(wt, "rev-parse", "HEAD")
            approved = ApprovedMerges(self.state)  # safe_push only pushes merges it knows: this one, and main's own
            for m in [x for x in _git(wt, "rev-list", "--merges", main, f"^{head}").split() if x]:
                if not approved.has(m):
                    approved.add(m, {"kind": "main", "via": new})
            approved.add(new, {"kind": "main_sync", "base": head, "other": main, "parents": [head, main]})
            st.pop("conflict", None)
            self._write("main_sync.json", st)
            stale = [v for v in qs.values() if v.get("kind") == "merge" and v.get("sync") and v.get("status") == "open"]
            for v in stale:  # a sync question that no longer applies is closed
                v.update(status="answered", answer=f"resolved: synced cleanly at {new}", closed_at=now.isoformat())
            if stale:
                self._write("questions.json", qs)
            self._log(f"main sync: merged origin/main {main[:12]} into {layer} ({head[:12]} -> {new[:12]})")
            if self.push:
                self._push()
            return "synced"
        except (RuntimeError, OSError, ValueError) as e:  # never fails the step; retried at the next fetch
            self._log(f"main sync error: {e!r}"[:500])
            return "error"


# ---------------------------------------------------------------------- real I/O
MAIL_TIMEOUT_S = 60  # Live-run P1: every IMAP/SMTP socket operation gives up after this; a hang is never silent


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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=MAIL_TIMEOUT_S) as s:
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
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=MAIL_TIMEOUT_S)  # P1: never block forever
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
        lanes_mod.write_bytes_atomic(seen_path, json.dumps(seen[-2000:]).encode("utf-8"))  # R60: other lanes read it
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


def real_checks() -> dict:
    """The real plain-code readiness checks (D-030). AI probes are separate: see real_probes."""
    return dict(readiness.PLAIN_CHECKS)


def real_probes(limits: dict) -> dict:
    """The real AI probe agents; constructing them launches nothing."""
    return readiness.probe_agents(limits)


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


def real_manager(limits: dict):
    """T1C5: the Manager is Claude, read-only (design §2): plan mode, reading tools only."""
    from core.agents import ClaudeAgent
    return ClaudeAgent(limits.get("agent_timeout_s", 1800), permission_mode="plan", allowed_tools=["Read", "Glob", "Grep"])


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


SMOKE_SWEEP_AGE_S = 3600  # R60: with lanes, only this lane's smoke folders older than this are swept


def smoke(team: Team, workdir: Path, call: Callable | None = None,
          warn: Callable[[str], None] | None = None, lane: str | None = None) -> list[str]:
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

    # R39: sweep leftovers from earlier runs (best effort). R60: with lanes, only this lane's own folders
    # (forge-smoke-<lane>-*) and only those older than an hour: another process may be using a newer one.
    tag = f"{lane}-" if lane else ""
    for old in Path(workdir).glob(f"forge-smoke-{tag}*"):
        if lane:
            try:
                if time.time() - old.stat().st_mtime < SMOKE_SWEEP_AGE_S:
                    continue
            except OSError:
                continue
        if old.is_dir():
            try:
                shutil.rmtree(old, onerror=unlock)
            except OSError:
                pass

    for role, (schema, writes, example) in SMOKE_ROLES.items():
        # Plain mkdir, not mkdtemp: on Windows mkdtemp locks the folder to this user, and Codex's sandbox runs as
        # a separate user, so files it wrote there could not be read back.
        root = Path(workdir) / f"forge-smoke-{tag}{role}-{uuid.uuid4().hex[:8]}"
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
    ap.add_argument("--spec", help="R61: the design this layer is built against (default: limits spec_file)")
    ap.add_argument("--tasks")
    ap.add_argument("--owner", default="benjaminanderson0802@gmail.com")
    ap.add_argument("--work", default=str(Path.home() / "Forge-work"))
    ap.add_argument("--lane", default=lanes_mod.MAIN, help="R60: which lane's conductor (default: main)")
    ap.add_argument("--force", action="store_true", help="init: replace a queue that still has tasks")
    a = ap.parse_args(argv)
    problem = lanes_mod.name_problem(a.lane)
    if problem:
        print(f"bad --lane: {problem}")
        return 2
    limits = load_limits(forge)
    root = forge / "state"
    state, shared = lanes_mod.state_dir(root, a.lane), lanes_mod.shared_dir(root)
    work = lanes_mod.work_dir(Path(a.work), a.lane)
    if a.cmd == "init" and not a.force:  # R60: init never silently replaces a lane's queue
        try:
            existing = json.loads((state / "queue.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            existing = {}
        except (OSError, ValueError):
            existing = {"tasks": ["unreadable"]}
        if isinstance(existing, dict) and existing.get("tasks"):
            print(f"lane {a.lane} already has a queue with {len(existing['tasks'])} task(s) "
                  f"(layer {existing.get('layer')!r}); pass --force to replace it, or use another --lane")
            return 2
    if a.cmd == "status":
        q = json.loads((state / "queue.json").read_text(encoding="utf-8")) if (state / "queue.json").exists() else {}
        for t in q.get("tasks", []):
            print(f"{t['status']:9} {t['id']:6} {t['title']}")
        try:
            qs = json.loads((state / "questions.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            qs = {}
        for qid, v in (qs.items() if isinstance(qs, dict) else []):
            if isinstance(v, dict) and v.get("kind") == "capability" and v.get("status") == "open":
                sent = "held" if v.get("hold") else ("sent" if v.get("delivered") else "not sent yet")
                print(f"capability {v.get('capability')}: {v.get('condition')} ({sent}) Q-{qid}")
        for f in ("KILL", "PAUSED"):
            if (state / f).exists():
                print(f"{f} is set")
        if (shared / "KILL").exists():
            print("global KILL is set (every lane is stopped)")
        return 0
    if a.cmd == "init":  # R60: task ids are unique across lanes (task branches, ledger and questions name them)
        new_ids = {t.get("id") for t in json.loads(Path(a.tasks).read_text(encoding="utf-8")) if isinstance(t, dict)}
        for other in lanes_mod.listed(root):
            if other == a.lane:
                continue
            q = lanes_mod.read_json(lanes_mod.state_dir(root, other) / "queue.json", {})
            clash = sorted(new_ids & {t.get("id") for t in (q.get("tasks", []) if isinstance(q, dict) else [])
                                      if isinstance(t, dict)})
            if clash:
                print(f"task id(s) {', '.join(map(str, clash))} already exist in lane {other}; task ids must be "
                      "unique across lanes")
                return 2
    lanes_mod.migrate(root)  # R60: once, and only when state/shared has no accounting at all (never for status)
    # R60: only the main lane reads Ben's email; it routes replies to other lanes' questions to those lanes.
    inbox = gmail_inbox(a.owner, shared) if a.lane == lanes_mod.MAIN else \
        lanes_mod.routed_inbox(lanes_mod.state_dir(root, lanes_mod.MAIN), a.lane, state)
    c = Conductor(forge, work, state, real_team(limits), limits, owner_email=a.owner,
                  mailer=gmail_mailer(a.owner), inbox=inbox, gh=gh_cli(forge),
                  manager=real_manager(limits),
                  judge_cmds=["python drills/run_drills.py", "python -m core.suite"],
                  shared=shared, lane=a.lane)
    if a.cmd == "init":
        owner = lanes_mod.layer_owner(root, a.layer, a.lane)
        if owner is not None:
            print(f"layer branch {a.layer!r} already belongs to lane {owner!r}; each lane needs its own")
            return 2
        work.mkdir(parents=True, exist_ok=True)
        c.init_queue(a.layer, json.loads(Path(a.tasks).read_text(encoding="utf-8")))
        if a.spec:  # R61
            qd = c._queue()
            qd["spec_file"] = _norm(a.spec)
            c._save_queue(qd)
        lanes_mod.register(root, a.lane)
        print("queue ready" if a.lane == lanes_mod.MAIN else f"queue ready for lane {a.lane}")
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
    health = None
    try:
        if a.cmd == "run":
            health = service.Health(lanes_mod.service_dir(root, a.lane), limits).start()  # T1D2, R60: per lane
            if not _start_session(c, health):
                return 0
            if c._kill_set():  # R60: the mailbox reader only: no smoke test, no start notice, no work
                print(service.Service(c, lanes_mod.service_dir(root, a.lane), limits, health=health).serve())
                return 0
        if a.cmd == "smoke" or _smoke_stale(c.state, c.clock()):
            problems = _guarded_smoke(c, work, force=a.cmd == "smoke")  # R60: the lane's own work root
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
        print(service.Service(c, lanes_mod.service_dir(root, a.lane), limits, health=health).serve())  # T1D2
    finally:
        if health:
            health.stop()
        lock.close()
    return 0


def _start_session(c: Conductor, health, pause_s: float = 60) -> bool:
    """The run command's startup, under stall detection (Live-run P1): every piece of work (the inbox read,
    readiness, and the smoke test that follows) runs in the "step" phase, so a hang is caught like a hung step;
    only the paused wait is exempt. Returns False when the service must not start (KILL, a stop, tampering)."""
    health.set_phase("step")
    if c._kill_set():  # R32: while stopped, only retry a pending halt alert
        c._retry_halts()
        return c._mail_reader_only()  # R60: main stopped alone still reads the mailbox for the other lanes
    c._handle_inbox()  # R31: a STOP is honoured before anything is launched
    if c._kill_set():
        return False
    while (c.state / "PAUSED").exists():  # R40: while paused, only wait for Ben's answer
        (c.state / "conductor.heartbeat").write_text(f"{os.getpid()} {time.time()}")
        health.set_phase("paused")
        time.sleep(pause_s)
        health.set_phase("step")
        if c._kill_set():
            return False
        c._handle_inbox()
        if c._kill_set():
            return False
    try:
        c.session_start()  # D-030: readiness before every session, before the smoke test
    except Tampered:
        return False
    except (RuntimeError, OSError) as e:
        c._log(f"session readiness error: {e!r}"[:500])
    return True


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
                             warn=c._log, **({"lane": c.lane} if c.shared is not None else {}))
        except Stopped as e:  # R42/R49
            problems = [f"stopped during the smoke test ({e})"]
        except Capped as e:  # R37
            problems = [f"token cap reached during the smoke test ({e})"]
        except NotReady as e:  # D-030
            problems = ["not ready: " + "; ".join(f"{n}: {why}" for n, why in sorted(e.names.items()))]
        except Tampered as e:
            problems = [f"state files changed during the smoke test (Forge halted): {e}"[:500]]
        except RuntimeError as e:  # fail-closed preconditions (R15)
            problems = [str(e)[:500]]
    if problems and c._stop_requested():  # R49: a stop is not a failed smoke test (no 30-minute wait)
        pass
    elif problems:
        c._write("smoke_fail.json", {"at": c.clock().isoformat(), "problems": problems})
    else:
        (c.state / "smoke_fail.json").unlink(missing_ok=True)
        c._write("smoke_ok.json", {"at": c.clock().isoformat()})
    return problems


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
