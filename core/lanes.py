"""Lanes (amendment R58 of docs/specs/bootstrap-conductor.md): several conductors side by side, sharing one set of caps.

Within one conductor agents run one at a time, because the after-run tamper check (R9/R14) would see the
conductor's own writes for another task. A lane is a separate conductor process with its own state dir, queue,
layer branch and worktrees, so lanes run in parallel and each lane's tamper check sees only its own state.

    python -m core.bootstrap init --lane p2 --layer layer-2 --tasks tasks.json
    python -m core.bootstrap run --lane p2

Layout (main keeps today's paths):

    state/bootstrap/            the "main" lane's state (unchanged)
    state/lanes/<name>/         every other lane's state: lock, queue, questions, runs, capabilities, ...
    state/lanes.json            the lane names the watchdog and the Windows script start (main is implied)
    state/shared/               shared by every lane: meter.json, holds.json, mail_log.json, inbox_seen.json,
                                shared.lock and the global KILL
    state/service/              main's heartbeat; state/service/<name>/ for every other lane
    Forge-work/                 main's worktrees; Forge-work/lanes/<name>/ for every other lane

Shared files are read-modify-written only under an exclusive lock file (msvcrt.locking on Windows, fcntl.flock
elsewhere), with retries, and replaced atomically. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAIN = "main"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")  # no "-": a question id's lane prefix must be unambiguous
# Names that would collide with question kinds (qid prefixes), service files (Windows names are case-insensitive)
# or the state layout.
RESERVED = frozenset({MAIN, "gate", "blocked", "replan", "tamper", "capability", "merge", "shared", "lanes",
                      "bootstrap", "service", "channel", "wake", "kill", "paused", "heartbeat", "status", "routed"})
SHARED_LOCK = "shared.lock"
# Files every lane accumulates into. Written only by conductor code (never by agents), at any time, by any lane:
# so no lane can fingerprint them. Each gets a never-decreases check instead (shrunk()).
ACCUMULATORS = ("meter.json", "holds.json", "mail_log.json", "inbox_seen.json")
TMP_PREFIX = ".tmp-"  # atomic-replace temporaries; transient while another lane writes
ROUTED_KEEP, ROUTED_SEEN_KEEP = 50, 500
MAIL_IDS_KEEP, INBOX_SEEN_KEEP = 500, 2000  # R26: the caps the writers apply (bootstrap.SENT_IDS_KEEP, gmail_inbox)


# ------------------------------------------------------------------ names and paths
def name_problem(name: str) -> str | None:
    if name == MAIN:
        return None
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "a lane name is 1-24 characters: a lowercase letter, then lowercase letters, digits or _"
    if name in RESERVED:
        return f"{name!r} is reserved"
    return None


def state_dir(state_root: Path, lane: str) -> Path:
    return Path(state_root) / "bootstrap" if lane == MAIN else Path(state_root) / "lanes" / lane


def shared_dir(state_root: Path) -> Path:
    return Path(state_root) / "shared"


def work_dir(work_root: Path, lane: str) -> Path:
    return Path(work_root) if lane == MAIN else Path(work_root) / "lanes" / lane


def service_dir(state_root: Path, lane: str) -> Path:
    return Path(state_root) / "service" if lane == MAIN else Path(state_root) / "service" / lane


def channel_dir(state_root: Path, lane: str) -> Path:
    return Path(state_root) / "channel" if lane == MAIN else Path(state_root) / "channel" / "lanes" / lane


def task_name(lane: str) -> str:
    return "Forge conductor" if lane == MAIN else f"Forge conductor {lane}"


def listed(state_root: Path) -> list[str]:
    """"main" plus every valid name in state/lanes.json, in order, without duplicates."""
    data = read_json(Path(state_root) / "lanes.json", [])
    names = [MAIN]
    for n in data if isinstance(data, list) else []:
        if isinstance(n, str) and n not in names and name_problem(n) is None:
            names.append(n)
    return names


def register(state_root: Path, lane: str) -> None:
    """Add a lane to state/lanes.json (Ben's `init --lane`; never during an agent run of that lane)."""
    if lane == MAIN:
        return
    problem = name_problem(lane)
    if problem:
        raise ValueError(problem)
    with SharedLock(shared_dir(state_root)):
        names = [n for n in listed(state_root) if n != MAIN]
        if lane not in names:
            write_json_atomic(Path(state_root) / "lanes.json", names + [lane])


def layer_owner(state_root: Path, layer: str, lane: str) -> str | None:
    """Another lane whose queue already uses this layer branch, if any (two lanes must never share a branch)."""
    for other in listed(state_root):
        if other == lane:
            continue
        q = read_json(state_dir(state_root, other) / "queue.json", {})
        if isinstance(q, dict) and q.get("layer") == layer:
            return other
    return None


def runs_dirs(state_root: Path) -> list[Path]:
    """Every lane's runs/ folder, listed or not (R51 counts launches across all lanes)."""
    out = [Path(state_root) / "bootstrap" / "runs"]
    try:
        with os.scandir(Path(state_root) / "lanes") as it:
            out += [Path(e.path) / "runs" for e in sorted(it, key=lambda e: e.name) if e.is_dir()]
    except OSError:
        pass
    return out


def qid_lane(qid: str, names: list[str]) -> str | None:
    """The non-main lane that owns a question id (`p2-blocked-3` -> "p2"), or None (main's own ids)."""
    head = str(qid).split("-", 1)[0]
    return head if head != MAIN and head in names and "-" in str(qid) else None


# ------------------------------------------------------------------ locking and atomic files
_held = threading.local()


class SharedLock:
    """An exclusive, re-entrant (per thread) lock on <folder>/shared.lock, across processes.

    msvcrt.locking on Windows, fcntl.flock elsewhere, non-blocking with retries until timeout_s; then
    TimeoutError (an OSError, so a conductor stage treats it like any filesystem error)."""

    def __init__(self, folder: Path, timeout_s: float = 60.0):
        self.path = Path(folder) / SHARED_LOCK
        self.timeout_s = timeout_s

    def _state(self) -> dict:
        if not hasattr(_held, "locks"):
            _held.locks = {}
        return _held.locks

    def __enter__(self) -> "SharedLock":
        key = str(self.path.resolve() if self.path.parent.exists() else self.path)
        locks = self._state()
        if key in locks:
            locks[key][1] += 1
            self._key = key
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        deadline, delay = time.monotonic() + self.timeout_s, 0.002
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    f.close()
                    raise TimeoutError(f"could not lock {self.path} within {self.timeout_s:.0f} s")
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
        locks[key] = [f, 1]
        self._key = key
        return self

    def __exit__(self, *_exc) -> None:
        locks = self._state()
        entry = locks.get(self._key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        f = entry[0]
        del locks[self._key]
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            f.close()


def write_bytes_atomic(path: Path, raw: bytes) -> None:
    """Write a temporary file beside `path` and replace it in one step. On Windows a replace fails while a reader
    has the target open, so it is retried for a few seconds."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=TMP_PREFIX)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        for i in range(100):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 99:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_json_atomic(path: Path, data) -> None:
    write_bytes_atomic(path, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


def read_json(path: Path, default):
    for i in range(5):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:  # Windows: a writer is replacing it right now
            time.sleep(0.02 * (i + 1))
        except (OSError, ValueError):
            return default
    return default


def update_json(path: Path, fn: Callable, default, lock: SharedLock):
    """Read-modify-write under the shared lock: fn(data) changes data in place and returns the caller's result."""
    with lock:
        data = read_json(path, None)
        if not isinstance(data, type(default)):
            data = json.loads(json.dumps(default))
        result = fn(data)
        write_json_atomic(path, data)
        return result


# ------------------------------------------------------------------ migration
def migrate(state_root: Path) -> bool:
    """First run with lanes: copy today's main-lane meter, holds, mail log and inbox_seen into state/shared/, so no
    cap resets. Runs once (state/shared/meter.json marks it done); the old copies are left untouched. Returns True
    when it copied."""
    shared, boot = shared_dir(state_root), Path(state_root) / "bootstrap"
    if (shared / "meter.json").exists():
        _top_up_meter(shared, boot)
        return False
    with SharedLock(shared):
        if (shared / "meter.json").exists():
            _top_up_meter(shared, boot)
            return False
        for name in ACCUMULATORS[1:]:  # meter.json last: its existence marks the migration as done
            src = boot / name
            if src.is_file() and not (shared / name).exists():
                write_bytes_atomic(shared / name, src.read_bytes())
        src = boot / "meter.json"
        write_bytes_atomic(shared / "meter.json", src.read_bytes() if src.is_file() else b"{}")
    return True


def _top_up_meter(shared: Path, boot: Path) -> None:
    """Belt and braces for "caps never reset": if the old main-lane meter still counts more for some day and
    provider than the shared one (e.g. an older conductor kept writing it after the copy), the shared meter is raised
    to it. With every conductor on lanes the old file never changes again, so this is then a no-op."""
    old = read_json(boot / "meter.json", None)
    if not isinstance(old, dict) or not old:
        return
    with SharedLock(shared):
        cur = read_json(shared / "meter.json", {})
        cur = cur if isinstance(cur, dict) else {}
        changed = False
        for day, provs in old.items():
            if not isinstance(provs, dict):
                continue
            for prov, n in provs.items():
                if isinstance(n, int) and not isinstance(n, bool):
                    d = cur.setdefault(day, {}) if isinstance(cur.get(day, {}), dict) else None
                    if d is not None and int(_num(d.get(prov, 0))) < n:
                        d[prov], changed = n, True
        if changed:
            write_json_atomic(shared / "meter.json", cur)


# ------------------------------------------------------------------ the never-decreases check
def accumulators(shared: Path) -> dict:
    """The parsed shared accumulate files (None when missing), read under the shared lock."""
    out = {}
    with SharedLock(shared):
        for name in ACCUMULATORS:
            p = Path(shared) / name
            if not p.exists():
                out[name] = None
                continue
            data = read_json(p, _BAD)
            out[name] = data
    return out


_BAD = "<unreadable>"


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v else 0.0


def _when(x) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(x))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def shrunk(before: dict, after: dict, now: datetime) -> list[str]:
    """What an agent run may have taken away from the shared accumulate files. Conductor code only ever adds to
    them (or drops what has aged out or passed a fixed cap), so any other loss is tampering:
      meter.json   no day/provider count may decrease or disappear
      holds.json   no hold may be shortened or removed
      mail_log     no send younger than 23 hours may disappear (the budget); no recent Message-ID either
      inbox_seen   no handled Message-ID may disappear (unless the list is at its cap)
    A file that was readable before and is unreadable or missing after counts as a loss."""
    out = []
    for name in ACCUMULATORS:
        b, a = before.get(name), after.get(name)
        if b is None or b == _BAD:
            continue
        if a is None or a == _BAD:
            out.append(f"shared/{name} (removed or unreadable after the run)")
            continue
        if name == "meter.json" and isinstance(b, dict):
            for day, provs in b.items():
                for prov, n in (provs.items() if isinstance(provs, dict) else []):
                    m = (a.get(day) or {}).get(prov) if isinstance(a, dict) and isinstance(a.get(day), dict) else None
                    if m is None or _num(m) < _num(n):
                        out.append(f"shared/meter.json (decreased: {day} {prov} {n} -> {m})")
        elif name == "holds.json" and isinstance(b, dict):
            for prov, until in b.items():
                tb, ta = _when(until), _when(a.get(prov)) if isinstance(a, dict) else None
                if tb is not None and (ta is None or ta < tb):
                    out.append(f"shared/holds.json (hold on {prov} shortened or removed)")
        elif name == "mail_log.json" and isinstance(b, dict):
            sent_after = set(a.get("sent", [])) if isinstance(a, dict) and isinstance(a.get("sent"), list) else set()
            for x in b.get("sent", []) if isinstance(b.get("sent"), list) else []:
                t = _when(x)
                if t is not None and (now - t).total_seconds() < 23 * 3600 and x not in sent_after:
                    out.append("shared/mail_log.json (a recent send was removed)")
                    break
            ids_b = b.get("ids", []) if isinstance(b.get("ids"), list) else []
            ids_a = a.get("ids", []) if isinstance(a, dict) and isinstance(a.get("ids"), list) else []
            if len(ids_a) < MAIL_IDS_KEEP and not set(ids_b) <= set(ids_a):
                out.append("shared/mail_log.json (sent Message-IDs were removed)")
        elif name == "inbox_seen.json" and isinstance(b, list):
            if not isinstance(a, list) or (len(a) < INBOX_SEEN_KEEP and not set(map(str, b)) <= set(map(str, a))):
                out.append("shared/inbox_seen.json (handled messages were removed)")
    return out


# ------------------------------------------------------------------ routing Ben's answers to their lane
def _routed_path(main_state: Path, lane: str) -> Path:
    return Path(main_state) / "routed" / f"{lane}.json"


def route(main_state: Path, lane: str, message: dict) -> None:
    """Main lane only: keep an owner's reply to a question of `lane` for that lane's conductor to read. The file
    is in main's own state, written by main between its agent runs (so main's tamper check covers it)."""
    p = _routed_path(main_state, lane)
    data = read_json(p, [])
    data = data if isinstance(data, list) else []
    data.append({"rid": uuid.uuid4().hex, "from": str(message.get("from", "")),
                 "subject": str(message.get("subject", "")), "body": str(message.get("body", "")),
                 "message_id": str(message.get("message_id", "") or "")})
    write_json_atomic(p, data[-ROUTED_KEEP:])


def routed_inbox(main_state: Path, lane: str, lane_state: Path) -> Callable[[], list[dict]]:
    """A non-main lane's inbox: the replies main routed to it that it hasn't read yet. The lane only reads main's
    file (writing there could trip main's tamper check); what it has read is kept in its own routed_seen.json."""
    def read() -> list[dict]:
        data = read_json(_routed_path(main_state, lane), [])
        seen = read_json(Path(lane_state) / "routed_seen.json", [])
        seen = [str(x) for x in seen] if isinstance(seen, list) else []
        new = [m for m in (data if isinstance(data, list) else [])
               if isinstance(m, dict) and str(m.get("rid")) not in set(seen)]
        if new:
            write_json_atomic(Path(lane_state) / "routed_seen.json",
                              (seen + [str(m.get("rid")) for m in new])[-ROUTED_SEEN_KEEP:])
        return [{"from": m.get("from", ""), "subject": m.get("subject", ""), "body": m.get("body", ""),
                 "outgoing": False} for m in new]
    return read
