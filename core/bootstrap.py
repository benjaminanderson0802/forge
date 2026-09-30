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

from core import readiness
from core.ledger import Ledger, Rejected
from core.usage import Meter

ROLES = {"ci": "ci", "forge-manager": "manager", "forge-executor": "executor",
         "forge-auditor": "auditor", "forge-core": "core", "benjamin": "human"}
NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
TASK_FIELDS = ("id", "title", "section", "files_in_scope", "test_files", "test_cmd")
NOTE_CAP, NOTES_KEEP, BODY_CAP = 2000, 30, 20000  # R19
SUBJECT_CAP, CLOSED_KEEP, SENT_IDS_KEEP = 300, 50, 500  # R26, R28


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
    {"id": _STR, "title": _STR, "section": _STR, "files_in_scope": _STRS, "test_files": _STRS, "test_cmd": _STR,
     "needs": {"type": "array", "items": {"type": "string"}}},
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


class NotReady(Exception):
    """D-030: a capability the agent needs lacks usable evidence; nothing was written or launched."""

    def __init__(self, names: dict[str, str]):
        self.names = dict(names)
        super().__init__("; ".join(f"{n}: {why}" for n, why in sorted(self.names.items())))


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


class Conductor:
    def __init__(self, repo: Path, work: Path, state: Path, team: Team, limits: dict, *,
                 owner_email: str, mailer: Callable[[str, str], None], inbox: Callable[[], list[dict]],
                 gh: Callable[[list[str]], tuple[int, str]], clock: Callable[[], datetime] | None = None,
                 judge_cmds: list[str] | None = None, push: bool = True,
                 checks: dict | None = None, probes: dict | None = None):
        self.repo, self.work, self.state = Path(repo), Path(work), Path(state)
        self.team, self.limits = team, limits
        self.owner = owner_email.strip().lower()
        self.mailer, self.inbox, self.gh = mailer, inbox, gh
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.judge_cmds = list(judge_cmds or [])
        self.push = push
        self.meter = Meter(self.state, clock=self.clock)
        self.state.mkdir(parents=True, exist_ok=True)
        # D-030: readiness always runs; None means the real checks and AI probes (there is no "off" mode).
        self.checks = dict(real_checks() if checks is None else checks)
        self.probes = dict(real_probes(limits) if probes is None else probes)

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
        if isinstance(q.get("notes"), list):  # R28
            q["notes"] = [str(x)[:NOTE_CAP] for x in q["notes"]][-NOTES_KEEP:]
        for t in q.get("tasks", []):  # R19: nothing grows without bound
            for key in ("notes", "trouble_notes"):
                if isinstance(t.get(key), list):
                    t[key] = [str(x)[:NOTE_CAP] for x in t[key]][-NOTES_KEEP:]
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
    def _call(self, role: str, prompt: str, schema: dict | None, cwd: Path | None = None, needs=None):
        agent = getattr(self.team, role)
        provider = getattr(agent, "provider", None)
        if provider and self.meter.over(provider, self.limits):  # R37: checked before every launch
            raise Capped(provider)
        self._launch_gate(provider, needs)
        prompt = prompt + self._prompt_blocks(role)
        return self._guarded_run(role, agent, prompt, cwd, schema)

    def _guarded_run(self, label: str, agent, prompt: str, cwd: Path | None, schema: dict | None):
        """R14/R15 guarded agent run: run record, fingerprint before and after, KILL on tamper, metering."""
        role = label
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
        if (self.state / "KILL").exists():
            return self._cap_map()
        old = self._cap_map()
        now = self.clock()
        force = set(force or ())
        wanted = None if names is None else set(names)
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
        if (self.state / "KILL").exists() or (self.state / "PAUSED").exists() or self._capped():
            return {}
        return self._refresh_readiness()

    def _requirements(self, role: str, task: dict | None = None) -> set[str]:
        provider = getattr(getattr(self.team, role), "provider", None)
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

    def _gate_ready(self, cap_map: dict) -> dict[str, str]:
        return self._unready({"git", "github"}, cap_map)

    def _pick_tasks(self, q: dict, cap_map: dict):
        """Runnable tasks in the order they should run: pending troubleshooting first (troubleshooter's
        requirements only), then todo/tests_ok tasks in queue order. Unready tasks record waiting_on and are
        skipped; self._waiting says whether anything was skipped for readiness."""
        tasks = [t for t in q["tasks"] if t["status"] in ("todo", "tests_ok")]
        pending = [t for t in tasks if t["status"] == "tests_ok" and t.get("troubleshoot_pending")]
        rest = [t for t in tasks if t not in pending]
        for t in pending + rest:
            if t in pending:
                role = "troubleshooter"
            elif t.get("kind") == "plan":
                role = "planner"
            else:
                role = "test_writer" if t["status"] == "todo" else "builder"
            unready = self.ready_for(role, t, cap_map)
            waiting = sorted(unready)
            if waiting or t.get("waiting_on"):
                if t.get("waiting_on") != waiting:
                    self._update(t["id"], waiting_on=waiting)
            if waiting:
                self._waiting = True
                continue
            yield t

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
            self._refresh_readiness()  # D-030: before every cycle
        except Tampered:
            return "killed"
        except (RuntimeError, OSError) as e:  # R15 fail-closed preconditions: a stage-like error
            self._log(f"readiness refresh error: {e!r}"[:500])
            return "error"
        q = self._queue()
        cap_map = self._cap_map()
        self._waiting = False
        if q.get("drift_due"):
            if self.ready_for("drift_keeper", cap_map=cap_map):
                self._waiting = True  # the gate stays closed while drift_due is set
            else:
                try:
                    self._drift_check()
                except Capped:
                    return "capped"
                except NotReady:
                    return "not_ready"
                except Tampered:
                    return "killed"
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
            except Capped:  # R37: the attempt was undone by its stage; retried when the cap resets
                return "capped"
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
                and not any(v["kind"] == "gate" for v in qs.values()):
            if self._gate_ready(cap_map):  # the gate needs git and github
                return "not_ready"
            self._gate()
            return "gate"
        return "not_ready" if self._waiting else "idle"

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
        prompt += ("\nAnswer with JSON: {\"status\": \"done\" | \"blocked\", \"summary\": \"...\"}. "
                   "A blocked answer must also include \"tried\" (at least 2 different routes you actually tried) "
                   "and \"error\" (the real error output); without them it is rejected as an easy way out.")
        try:
            r = self._call("builder", prompt, S_BUILD, needs=t.get("needs") or [])
        except (Capped, NotReady):  # R37: release the claim and undo, no failure recorded
            self._apply(f"{tag}-release", "release", cid, "forge-core")
            _git(self.wt, "reset", "-q", "--hard", tests_commit)
            _git(self.wt, "clean", "-q", "-fd")
            raise

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
        try:
            rv = self._call("reviewer", "You are the REVIEWER (read-only). Check this change against the task. "
                                        "Reject shortcuts, bare-minimum work, drift from the task, and anything that "
                                        "weakens tests.\n\n" + self._task_prompt(t) + "\nDIFF:\n" + diff[:60000] +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", S_REVIEW)
        except (Capped, NotReady):  # R37: undo the submitted run like a failed judge, but record no failure
            self._apply(f"{tag}-capped", "fail", cid, "forge-auditor")
            if self._ledger().contracts().get(cid, {}).get("status") == "failed":
                self._apply(f"{tag}-reopen", "reopen", cid, "forge-manager")
            _git(self.wt, "reset", "-q", "--hard", tests_commit)
            _git(self.wt, "clean", "-q", "-fd")
            raise
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
            try:
                self._troubleshoot(tid, reason, output)
            except (Capped, NotReady):  # R38: kept, and run before the next builder attempt
                self._update(tid, troubleshoot_pending={"reason": str(reason)[:NOTE_CAP],
                                                        "output": str(output)[-4000:]})
                raise

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
                     troubleshoots=t.get("troubleshoots", 0) + 1, troubleshoot_pending=None)

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
        try:
            r = self._call("planner", "You are the PLANNER. Write the implementation plan to " + plan_file +
                           " (and no other file), then return its tasks.\n\n" + self._task_prompt(t) +
                           "\nEach task needs: id, title, section, files_in_scope, test_files, test_cmd.\n"
                           "Each task may also list \"needs\": capability names from the capability map (git, github, "
                           "claude, codex, gmail, docker, n8n, ollama, python_libs, browser, or a new name) that its "
                           "Builder needs beyond git and its own AI.\n"
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
        else:
            existing = {x["id"] for x in self._queue()["tasks"]}
            if any(x["id"] in existing for x in tasks):
                reason = "plan rejected: task ids clash with existing tasks"
        if not reason:
            plan_text = (self.wt / plan_file).read_text(encoding="utf-8")
            try:
                rv = self._call("reviewer", "You are the REVIEWER (read-only). Check this plan against the task and "
                                            "design: complete, testable, no placeholders, no drift.\n\n" +
                                self._task_prompt(t) + "\nPLAN:\n" + plan_text[:60000] + "\nTASKS JSON:\n" +
                                json.dumps(tasks)[:20000] +
                                "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}",
                                S_REVIEW)
            except (Capped, NotReady):
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
            if isinstance(x.get("needs"), list):
                nt["needs"] = list(x["needs"])
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
            try:
                c.session_start()  # D-030: readiness before every session, before the smoke test
            except Tampered:
                return 0
            except (RuntimeError, OSError) as e:
                c._log(f"session readiness error: {e!r}"[:500])
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
        except NotReady as e:  # D-030
            problems = ["not ready: " + "; ".join(f"{n}: {why}" for n, why in sorted(e.names.items()))]
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
