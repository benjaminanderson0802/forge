"""Lanes (amendment R60 of docs/specs/bootstrap-conductor.md): several conductors side by side, sharing one set of caps.

Within one conductor agents run one at a time, because the after-run tamper check (R9/R14) would see the
conductor's own writes for another task. A lane is a separate conductor process with its own state dir, queue,
layer branch and worktrees, so lanes run in parallel and each lane's tamper check sees only its own state.

    python -m core.bootstrap init --lane p2 --layer layer-2 --tasks tasks.json
    python -m core.bootstrap run --lane p2

Layout (main keeps today's paths):

    state/bootstrap/            the "main" lane's state (unchanged)
    state/lanes/<name>/         every other lane's state: lock, queue, questions, runs, capabilities, ...
    state/lanes.json            the lane names the watchdog and the Windows script start (main is implied)
    state/shared/               shared by every lane: meter/, holds/ and mail/ (one file per lane, summed),
                                inbox_seen.json (main's), migrated.json, shared.lock and the global KILL
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
                      "bootstrap", "service", "channel", "wake", "kill", "paused", "heartbeat", "status", "routed",
                      "accounting", "meter", "holds", "mail"})
SHARED_LOCK = "shared.lock"
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


# ------------------------------------------------------------------ shared accounting: one file per lane (R60)
# Each lane writes only its own file in state/shared/meter/, holds/ and mail/ (state/shared/<kind>/<lane>.json); the
# shared total is the sum (meter, mail sends) or the latest (holds) over every lane's file. So no lane ever writes a
# file another lane writes, and a concurrent increment can never be overwritten by a stale copy:
#   - a lane fingerprints its OWN files like the rest of its state (only it writes them, never during its runs);
#   - every other lane's file may only grow during this lane's agent runs (shrunk());
#   - the owning lane keeps, in memory, exactly what it last wrote and checks it before every write, every launch
#     and every step (OwnFiles.verify): a lowered file is a tamper alarm even if it was lowered "in between".
# inbox_seen.json is written only by the main lane (the one mailbox reader) and is treated as main's own file.
KINDS = ("meter", "holds", "mail")
INBOX_SEEN = "inbox_seen.json"
MARKER = "migrated.json"
MANIFEST = "accounting.json"  # every accounting file that must exist (a lane registers its file on first write)


class AccountingError(RuntimeError):
    """Shared accounting (meter, holds, mail log) is missing, unreadable or invalid: fail closed (R15)."""


class AccountingTampered(AccountingError):
    """A lane's own accounting file is not what that lane last wrote."""


def _valid_meter(d) -> bool:
    return isinstance(d, dict) and all(
        isinstance(day, str) and isinstance(provs, dict) and all(
            isinstance(p, str) and isinstance(n, int) and not isinstance(n, bool) and n >= 0
            for p, n in provs.items()) for day, provs in d.items())


def _valid_holds(d) -> bool:
    return isinstance(d, dict) and all(isinstance(k, str) and _when(v) is not None for k, v in d.items())


def _valid_mail(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("sent", []), list) and isinstance(d.get("ids", []), list) \
        and all(_when(x) is not None for x in d.get("sent", []))


def _valid_seen(d) -> bool:
    return isinstance(d, list)


VALID = {"meter": _valid_meter, "holds": _valid_holds, "mail": _valid_mail}


def read_strict(path: Path, valid: Callable) -> object:
    """The parsed file, None when it doesn't exist; AccountingError when it can't be read or isn't valid."""
    p = Path(path)
    for i in range(5):
        try:
            raw = p.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return None
        except PermissionError:  # Windows: a writer is replacing it right now
            if i == 4:
                raise AccountingError(f"{p.name}: can't be read")
            time.sleep(0.02 * (i + 1))
        except OSError as e:
            raise AccountingError(f"{p.name}: can't be read ({type(e).__name__})") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise AccountingError(f"{p.parent.name}/{p.name}: invalid JSON") from e
    if not valid(data):
        raise AccountingError(f"{p.parent.name}/{p.name}: not valid {p.parent.name} data")
    return data


def _canon(data) -> str:
    return json.dumps(data, sort_keys=True)


class OwnFiles:
    """state/shared/<kind>/<lane>.json for every lane: each lane writes only its own file."""

    def __init__(self, shared: Path, kind: str, lane: str | None, lock: SharedLock):
        self.folder, self.kind, self.lane, self.lock = Path(shared) / kind, kind, lane, lock
        self.valid = VALID[kind]
        self._expected: str | None = None  # what this process last read or wrote for its own file

    @property
    def path(self) -> Path:
        return self.folder / f"{self.lane}.json"

    def read_all(self) -> dict:
        """{lane: data} for every lane's file; AccountingError if any is unreadable or invalid."""
        out = {}
        with self.lock:
            try:
                names = sorted(e.name for e in os.scandir(self.folder) if e.is_file())
            except FileNotFoundError:
                return out
            except OSError as e:
                raise AccountingError(f"{self.kind}/ can't be listed ({type(e).__name__})") from e
            for n in names:
                if not n.endswith(".json") or n.startswith(TMP_PREFIX):
                    continue
                data = read_strict(self.folder / n, self.valid)
                if data is not None:
                    out[n[:-5]] = data
            for rel in manifest_files(self.folder.parent):  # a registered file that is gone is never zero usage
                kind, _, name = rel.partition("/")
                if kind == self.kind and name[:-5] not in out:
                    raise AccountingError(f"registered accounting file state/shared/{rel} is missing")
        return out

    def own(self) -> dict:
        """This lane's file, checked against what this process last saw of it."""
        with self.lock:
            data = read_strict(self.path, self.valid)
            if data is None and f"{self.kind}/{self.lane}.json" in manifest_files(self.folder.parent):
                raise AccountingError(f"registered accounting file state/shared/{self.kind}/{self.lane}.json "
                                      "is missing")
            canon = _canon(data)
            if self._expected is not None and canon != self._expected:
                raise AccountingTampered(f"shared/{self.kind}/{self.lane}.json is not what lane {self.lane} "
                                         "last wrote")
            self._expected = canon
            return data if data is not None else {}

    verify = own

    def update(self, fn: Callable):
        with self.lock:
            data = self.own()
            result = fn(data)
            register_file(self.folder.parent, f"{self.kind}/{self.lane}.json", self.lock)  # before the first write
            write_json_atomic(self.path, data)
            self._expected = _canon(data)
            return result


# ------------------------------------------------------------------ the manifest: accounting files that must exist
def _valid_manifest(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("files"), list) and all(isinstance(x, str) for x in d["files"])


def manifest_files(shared: Path) -> list[str]:
    """The registered accounting files (relative to state/shared). AccountingError if the manifest is unreadable.
    A missing manifest is an empty list (accounting_problem decides whether that is allowed)."""
    data = read_strict(Path(shared) / MANIFEST, _valid_manifest)
    return list(data["files"]) if data else []


def register_file(shared: Path, rel: str, lock: SharedLock) -> None:
    """Record that state/shared/<rel> must exist from now on (a lane's first write of its own file). Entries are
    never removed: a registered file that goes missing is an accounting error, never zero usage."""
    with lock:
        files = manifest_files(shared)
        if rel not in files:
            write_json_atomic(Path(shared) / MANIFEST, {"files": files + [rel]})


def _any_accounting(shared: Path) -> bool:
    shared = Path(shared)
    if (shared / MANIFEST).exists() or (shared / INBOX_SEEN).exists():
        return True
    for kind in KINDS:
        try:
            if any(e.is_file() for e in os.scandir(shared / kind)):
                return True
        except FileNotFoundError:
            continue
    return False


# ------------------------------------------------------------------ migration
def _marker(shared: Path) -> dict | None:
    try:
        return read_strict(Path(shared) / MARKER, lambda d: isinstance(d, dict))
    except AccountingError:
        return {"state": "unreadable"}


def migrate(state_root: Path) -> bool:
    """First run with lanes: today's main-lane meter, holds, mail log and inbox_seen become the main lane's shared
    files (state/shared/meter/main.json, holds/main.json, mail/main.json, inbox_seen.json), so no cap resets.

    It runs only when state/shared holds no accounting at all. state/shared/migrated.json is written first as
    {"state": "migrating"} and last as {"state": "done"}; an interrupted migration is finished by the next start
    (no conductor runs until it is done). Once any accounting exists, a missing or unreadable marker is never a
    reason to migrate again: accounting_problem() reports it and every lane fails closed. The old copies are left
    untouched. Returns True when it copied."""
    shared, boot = shared_dir(state_root), Path(state_root) / "bootstrap"
    m = _marker(shared)
    if m is not None and m.get("state") != "migrating":
        return False
    if m is None and _any_accounting(shared):
        return False
    with SharedLock(shared):
        m = _marker(shared)
        if m is not None and m.get("state") != "migrating":
            return False
        if m is None:
            if _any_accounting(shared):
                return False  # never migrate over existing accounting: fail closed instead
            write_json_atomic(shared / MARKER, {"state": "migrating"})
        files = []
        for kind, old in (("holds", "holds.json"), ("mail", "mail_log.json")):
            src = boot / old
            if src.is_file():
                write_bytes_atomic(shared / kind / f"{MAIN}.json", src.read_bytes())
                files.append(f"{kind}/{MAIN}.json")
        if (boot / INBOX_SEEN).is_file():
            write_bytes_atomic(shared / INBOX_SEEN, (boot / INBOX_SEEN).read_bytes())
            files.append(INBOX_SEEN)
        src = boot / "meter.json"
        write_bytes_atomic(shared / "meter" / f"{MAIN}.json", src.read_bytes() if src.is_file() else b"{}")
        files.append(f"meter/{MAIN}.json")
        write_json_atomic(shared / MANIFEST, {"files": files})
        write_json_atomic(shared / MARKER, {"state": "done", "at": datetime.now(timezone.utc).isoformat(),
                                            "from": str(boot)})
    return True


def accounting_problem(shared: Path) -> str | None:
    """Why the shared accounting can't be trusted, or None. Checked before any work is admitted (fail closed)."""
    shared = Path(shared)
    m = _marker(shared)
    if m is None:
        if _any_accounting(shared):
            return "state/shared/migrated.json is missing but accounting exists (it is never migrated over)"
        return "shared accounting was never set up (state/shared/migrated.json missing)"
    if m.get("state") != "done":
        return f"the shared accounting migration is not complete (migrated.json: {m.get('state')})"
    if not (shared / MANIFEST).exists():
        return "the accounting manifest (state/shared/accounting.json) is missing"
    lock = SharedLock(shared)
    try:
        with lock:
            for rel in manifest_files(shared):
                if not (shared / rel).is_file():
                    return f"registered accounting file state/shared/{rel} is missing"
            if f"meter/{MAIN}.json" not in manifest_files(shared):
                return "the main meter is not registered in the accounting manifest"
            for kind in KINDS:
                OwnFiles(shared, kind, None, lock).read_all()
            read_strict(shared / INBOX_SEEN, _valid_seen)
    except AccountingError as e:
        return str(e)
    return None


# ------------------------------------------------------------------ the never-decreases check
_BAD = "<unreadable>"


def accumulators(shared: Path) -> dict:
    """Every lane's accounting file, parsed ({"meter/<lane>.json": data, ..., "inbox_seen.json": data}); _BAD for
    one that can't be read or isn't valid. Read under the shared lock."""
    out = {}
    with SharedLock(shared):
        for kind in KINDS:
            try:
                names = sorted(e.name for e in os.scandir(Path(shared) / kind) if e.is_file())
            except FileNotFoundError:
                names = []
            for n in names:
                if n.endswith(".json") and not n.startswith(TMP_PREFIX):
                    try:
                        out[f"{kind}/{n}"] = read_strict(Path(shared) / kind / n, VALID[kind])
                    except AccountingError:
                        out[f"{kind}/{n}"] = _BAD
        try:
            out[INBOX_SEEN] = read_strict(Path(shared) / INBOX_SEEN, _valid_seen)
        except AccountingError:
            out[INBOX_SEEN] = _BAD
        try:
            out[MANIFEST] = read_strict(Path(shared) / MANIFEST, _valid_manifest)
        except AccountingError:
            out[MANIFEST] = _BAD
    return out


def unreadable(acc: dict) -> list[str]:
    return sorted(k for k, v in acc.items() if v == _BAD)


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
    """What an agent run may have taken away from other lanes' accounting files. Conductor code only ever adds to
    them (or drops what has aged out or passed a fixed cap), so any other loss is tampering:
      meter/*      no day/provider count may decrease or disappear
      holds/*      no hold may be shortened or removed
      mail/*       no send younger than 23 hours may disappear (the budget); no recent Message-ID either
      inbox_seen   no handled Message-ID may disappear (unless the list is at its cap)
    A file that existed before and is unreadable, invalid or missing after counts as a loss."""
    out = []
    for name, b in before.items():
        a = after.get(name)
        if b is None or b == _BAD:
            continue
        if a is None or a == _BAD:
            out.append(f"shared/{name} (removed, unreadable or invalid after the run)")
            continue
        if name.startswith("meter/"):
            for day, provs in b.items():
                for prov, n in provs.items():
                    m = a.get(day, {}).get(prov) if isinstance(a.get(day), dict) else None
                    if m is None or _num(m) < _num(n):
                        out.append(f"shared/{name} (decreased: {day} {prov} {n} -> {m})")
        elif name.startswith("holds/"):
            for prov, until in b.items():
                tb, ta = _when(until), _when(a.get(prov))
                if tb is not None and (ta is None or ta < tb):
                    out.append(f"shared/{name} (hold on {prov} shortened or removed)")
        elif name.startswith("mail/"):
            sent_after = set(a.get("sent", []))
            for x in b.get("sent", []):
                t = _when(x)
                if t is not None and (now - t).total_seconds() < 23 * 3600 and x not in sent_after:
                    out.append(f"shared/{name} (a recent send was removed)")
                    break
            ids_b, ids_a = b.get("ids", []), a.get("ids", [])
            if len(ids_a) < MAIL_IDS_KEEP and not set(map(str, ids_b)) <= set(map(str, ids_a)):
                out.append(f"shared/{name} (sent Message-IDs were removed)")
        elif name == MANIFEST:
            if not set(b["files"]) <= set(a["files"]):
                out.append("shared/accounting.json (registered accounting files were removed)")
        elif name == INBOX_SEEN:
            if len(a) < INBOX_SEEN_KEEP and not set(map(str, b)) <= set(map(str, a)):
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
