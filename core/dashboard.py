"""Forge's live dashboard data (amendment R64 of docs/specs/bootstrap-conductor.md).

    python -m core.dashboard --json [--root PATH]

`snapshot(forge_root, now)` answers, for every lane: who is doing what task right now, each task's stage and an
estimated time to finish it, the lane's next checkpoint (its layer's queue and spec coverage), the project's phases,
tokens per provider against the caps with the burn rate, and the last 12 hours of runs for a Gantt chart.

Every estimate comes from this machine's own run history (run folders: start = the folder's UTC stamp, end = the
mtime of its output.json), with the number of samples it rests on, or a stated default when there is no history.

Read-only by design: it never writes a file (no locks, no repairs) and never raises on a missing, partial or corrupt
file; anything it can't read is None ("unknown"). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import coverage as cov_mod
from core import lanes as lanes_mod
from core.ledger import _hash

PROVIDERS = ("claude", "codex")
PLAN_STAGES = ("planner", "reviewer")
BUILD_STAGES = ("test_writer", "builder", "judge", "reviewer", "merge")
STAGES = ("planner", "test_writer", "builder", "judge", "reviewer", "merge")
DEFAULT_S = {"planner": 900, "test_writer": 600, "builder": 900, "judge": 900, "reviewer": 300, "merge": 600}
FIRST_STAGE = {"todo": "test_writer", "tests_ok": "builder", "merge_pending": "merge"}
DEFAULT_TASKS_PER_PLAN = 6
HEARTBEAT_STALE_S = 180.0
JUDGE_GAP_MAX_S = 6 * 3600.0
TIMELINE_H = 12
DEFAULT_SPEC = "docs/specs/layer-1-design.md"
RUN_RE = re.compile(r"^(\d{8}T\d{6})-(.+)-([0-9A-Za-z]+)$")
TASK_RE = re.compile(r"^TASK (\S+?): ?(.*)$", re.M)
PROMPT_READ = 262144
_CACHE: dict = {}  # (kind, path) -> (stamp, value): in memory only, never on disk
_CACHE_MAX = 20000
BAD = object()  # a file that exists but can't be read or parsed: "unknown", never "empty"


# ------------------------------------------------------------------ small readers (never raise)
def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def read_bytes(path: Path, limit: int | None = None) -> bytes:
    """Read a file without ever getting in a writer's way. On Windows an ordinary open() denies delete sharing,
    so a conductor's os.replace() of that file during the read would fail; this opens it with FILE_SHARE_DELETE
    (and retries the brief sharing violation of a replace in progress). Raises OSError like open()."""
    path = Path(path)
    for i in range(5):
        try:
            with open_shared(path) as f:
                return f.read() if limit is None else f.read(limit)
        except PermissionError:
            if i == 4:
                raise
            import time
            time.sleep(0.02 * (i + 1))
    raise OSError("unreachable")


def open_shared(path: Path):
    """A binary read handle that lets other processes replace or delete the file while it is open."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)  # GENERIC_READ, share all, OPEN_EXISTING
    if h is None or h == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        if err in (2, 3):
            raise FileNotFoundError(2, "not found", str(path))
        if err in (5, 32):
            raise PermissionError(13, "sharing violation", str(path))
        raise OSError(err, "CreateFileW failed", str(path))
    try:
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        k32.CloseHandle(h)
        raise
    return os.fdopen(fd, "rb")


def read_text(path: Path, limit: int | None = None) -> str:
    return read_bytes(path, limit).decode("utf-8", "replace" if limit else "strict")


def _jread(path: Path, typ=None):
    """The parsed file; None if it doesn't exist; BAD if it exists but can't be read, parsed or is the wrong type."""
    try:
        data = json.loads(read_bytes(path).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return BAD
    return BAD if typ is not None and not isinstance(data, typ) else data


def _json(path: Path, default=None, typ=None):
    data = _jread(path, typ)
    return default if data is None or data is BAD else data


def _num(x) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        x = float(x)
        return x if math.isfinite(x) else None
    return None


def _count(x) -> int | None:
    """A non-negative whole count (tokens), or None when the value is not a usable number."""
    x = _num(x)
    return int(x) if x is not None and 0 <= x < 1e18 else None


def _exists(path: Path) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def _cached(kind: str, path: Path, loader):
    """Parse a file once per (mtime, size); the cache lives in this process only."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key, stamp = (kind, str(path)), (st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        value = loader(Path(path))
    except Exception:  # noqa: BLE001 - a file being written right now is read again next time
        return None
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (stamp, value)
    return value


def fmt_dur(s: float | None) -> str:
    """A short human duration: 45s, 14m, 2h 05m, 3d 4h; 'unknown' for None."""
    if s is None:
        return "unknown"
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


# ------------------------------------------------------------------ run history
def _load_output(path: Path) -> dict:
    data = json.loads(read_bytes(path).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("output.json is not an object")
    inner = data.get("data")
    ntasks = len(inner["tasks"]) if isinstance(inner, dict) and isinstance(inner.get("tasks"), list) else None
    ok = data.get("ok")
    tokens = data.get("tokens")
    return {"ok": ok if isinstance(ok, bool) else None,
            "tokens": _count(tokens),
            "provider": data.get("provider") if isinstance(data.get("provider"), str) else None,
            "ntasks": ntasks}


def _load_task(path: Path) -> tuple:
    head = read_bytes(path, PROMPT_READ).decode("utf-8", "replace")
    m = TASK_RE.search(head)
    return (m.group(1), m.group(2).strip()) if m else (None, None)


def read_runs(runs_dir: Path) -> list[dict]:
    """Every run folder in one lane's runs/, oldest first. A folder whose name isn't a run record is skipped."""
    out = []
    try:
        with os.scandir(runs_dir) as it:
            names = sorted(e.name for e in it if e.is_dir())
    except OSError:
        return out
    for name in names:
        m = RUN_RE.match(name)
        if not m:
            continue
        try:
            start = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        d = Path(runs_dir) / name
        out_path = d / "output.json"
        end = None
        try:
            end = datetime.fromtimestamp(os.stat(out_path).st_mtime, timezone.utc)
        except (OSError, ValueError, OverflowError):
            end = None
        info = _cached("output", out_path, _load_output) if end is not None else None
        task = _cached("prompt", d / "prompt.md", _load_task) or (None, None)
        out.append({"run_id": name, "role": m.group(2), "start": start, "end": end,
                    "ok": (info or {}).get("ok"), "tokens": (info or {}).get("tokens"),
                    "provider": (info or {}).get("provider"), "ntasks": (info or {}).get("ntasks"),
                    "task_id": task[0], "task_title": task[1]})
    out.sort(key=lambda r: (r["start"], r["run_id"]))
    return out


def stage_medians(all_runs: list[list[dict]]) -> dict:
    """{stage: {median_s, n, source}} from every lane's finished runs; defaults where there is no history."""
    samples: dict[str, list[float]] = {s: [] for s in STAGES}
    for runs in all_runs:
        for i, r in enumerate(runs):
            if r["end"] is not None and r["role"] in samples and r["role"] not in ("judge", "merge"):
                d = (r["end"] - r["start"]).total_seconds()
                if d > 0:
                    samples[r["role"]].append(d)
            if r["role"] == "builder" and r["end"] is not None and i + 1 < len(runs):
                gap = (runs[i + 1]["start"] - r["end"]).total_seconds()
                if 0 < gap < JUDGE_GAP_MAX_S:
                    samples["judge"].append(gap)
    out = {}
    for s in STAGES:
        xs = samples[s] if s != "merge" else []
        out[s] = ({"median_s": float(statistics.median(xs)), "n": len(xs), "source": "history"} if xs
                  else {"median_s": float(DEFAULT_S[s]), "n": 0, "source": "default"})
    return out


def builder_attempts(all_runs: list[list[dict]]) -> dict:
    """{median, n, source}: the median number of builder runs per build task in this machine's history (the
    judges and the reviewer send work back, so a task usually takes several attempts); 1 with no history."""
    per: dict = {}
    for runs in all_runs:
        for r in runs:
            if r["role"] == "builder" and r["task_id"]:
                per[r["task_id"]] = per.get(r["task_id"], 0) + 1
    xs = list(per.values())
    return ({"median": float(statistics.median(xs)), "n": len(xs), "source": "history"} if xs
            else {"median": 1.0, "n": 0, "source": "default"})


def tasks_per_plan(all_runs: list[list[dict]]) -> tuple[float, int]:
    """(median task count of finished planner outputs, samples); the default when there are none."""
    xs = [r["ntasks"] for runs in all_runs for r in runs
          if r["role"] == "planner" and r["ok"] is not False and isinstance(r["ntasks"], int) and r["ntasks"] > 0]
    return (float(statistics.median(xs)), len(xs)) if xs else (float(DEFAULT_TASKS_PER_PLAN), 0)


# ------------------------------------------------------------------ lanes
def lane_names(state_root: Path) -> list[str]:
    """main plus every valid name in state/lanes.json, in order (core.lanes.listed, tolerant of a bad file)."""
    try:
        return lanes_mod.listed(state_root)
    except Exception:  # noqa: BLE001
        return [lanes_mod.MAIN]


def conductor(state_root: Path, lane: str, sdir: Path, now: datetime, limits: dict) -> dict:
    """{alive, heartbeat_age_s} (+ private _phase, _last_status) from the service heartbeat, else
    conductor.heartbeat."""
    hb = _json(lanes_mod.service_dir(state_root, lane) / "heartbeat.json", None, dict)
    at = _num(hb.get("at")) if hb else None
    if at is not None:
        age = now.timestamp() - at
        phase = hb.get("phase") if isinstance(hb.get("phase"), str) else None
        last = hb.get("last_status") if isinstance(hb.get("last_status"), str) else None
        return {"alive": age <= HEARTBEAT_STALE_S and phase != "exited", "heartbeat_age_s": max(0.0, age),
                "_phase": phase, "_last_status": last}
    try:
        stamp = _num(float(read_text(sdir / "conductor.heartbeat").split()[1]))
        if stamp is None:
            raise ValueError("not a time")
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return {"alive": False, "heartbeat_age_s": None, "_phase": None, "_last_status": None}
    age = now.timestamp() - stamp
    timeout = _num(limits.get("agent_timeout_s")) or 1800.0
    return {"alive": age <= timeout + 300, "heartbeat_age_s": max(0.0, age), "_phase": None, "_last_status": None}


def ledger_passes(sdir: Path) -> set:
    """Contract ids with a `pass` event in the lane's hash-chained ledger log. Read-only: a torn last line is
    skipped (never repaired); a broken chain or an unreadable log gives no credit."""
    try:
        lines = [x for x in read_text(sdir / "ledger" / "events.jsonl").splitlines() if x.strip()]
    except (OSError, UnicodeDecodeError):
        return set()
    events = []
    for i, line in enumerate(lines):
        try:
            e = json.loads(line)
            if not isinstance(e, dict):
                raise ValueError
        except ValueError:
            if i == len(lines) - 1:
                break  # an append that never completed
            return set()
        events.append(e)
    prev = "genesis"
    for e in events:
        body = {k: v for k, v in e.items() if k != "hash"}
        try:
            if body.get("prev") != prev or _hash(body) != e.get("hash"):
                return set()
        except (TypeError, ValueError):
            return set()
        prev = e["hash"]
    return {e.get("contract_id") for e in events if e.get("action") == "pass"}


def spec_requirements(forge_root: Path, rel) -> dict | None:
    """The layer spec's requirements, or None when there is no usable spec."""
    if not isinstance(rel, str) or not rel or ".." in rel.replace("\\", "/").split("/") \
            or re.match(r"^([A-Za-z]:|[/\\])", rel):
        return None
    try:
        reqs = cov_mod.parse_requirements(read_text(Path(forge_root) / rel))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return reqs or None


def _day_usage(data, day: str) -> dict | None:
    """{provider: tokens} for one day; a provider whose value is unusable is None (unknown), never 0."""
    if not isinstance(data, dict):
        return None
    d = data.get(day, {})
    if not isinstance(d, dict):
        return None
    return {p: _count(d.get(p, 0)) for p in PROVIDERS}


def meter_by_lane(state_root: Path, day: str) -> dict:
    """{lane: {provider: tokens today} or None if unreadable}: state/shared/meter/<lane>.json when that folder has
    files (R60), else state/bootstrap/meter.json as main's."""
    folder = Path(state_root) / "shared" / "meter"
    files = []
    try:
        with os.scandir(folder) as it:
            files = sorted(e.name for e in it if e.is_file() and e.name.endswith(".json")
                           and not e.name.startswith(lanes_mod.TMP_PREFIX))
    except OSError:
        files = []
    if files:
        return {n[:-5]: _day_usage(_jread(folder / n, dict), day) for n in files}
    path = Path(state_root) / "bootstrap" / "meter.json"
    if not _exists(path):
        return {lanes_mod.MAIN: {p: 0 for p in PROVIDERS}}
    return {lanes_mod.MAIN: _day_usage(_jread(path, dict), day)}


def holds(state_root: Path, now: datetime) -> dict:
    """{provider: hold end} for holds that have not ended (every lane's file, the latest wins)."""
    datas = [_json(Path(state_root) / "bootstrap" / "holds.json", {}, dict)]
    folder = Path(state_root) / "shared" / "holds"
    try:
        with os.scandir(folder) as it:
            datas += [_json(Path(e.path), {}, dict) for e in it if e.is_file() and e.name.endswith(".json")]
    except OSError:
        pass
    out: dict = {}
    for data in datas:
        for p, until in (data or {}).items():
            try:
                t = datetime.fromisoformat(str(until))
            except ValueError:
                continue
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if t > now and (p not in out or t > out[p]):
                out[p] = t
    return out


def all_runs_dirs(state_root: Path) -> dict:
    """{lane folder name: runs dir} for every lane, listed or not (R51 counts launches across all lanes)."""
    out = {lanes_mod.MAIN: Path(state_root) / "bootstrap" / "runs"}
    try:
        with os.scandir(Path(state_root) / "lanes") as it:
            for e in sorted(it, key=lambda e: e.name):
                if e.is_dir() and e.name != lanes_mod.MAIN:
                    out[e.name] = Path(e.path) / "runs"
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ estimates
def _stage_txt(s: str, med: dict) -> str:
    m = med[s]
    return f"{s} {fmt_dur(m['median_s'])} ({'n=' + str(m['n']) if m['n'] else 'default'})"


def task_eta(t: dict, current: dict | None, med: dict, attempts: dict | None = None,
             builds_done: int = 0) -> tuple:
    """(stage, eta_s, eta_basis) for one queue task (R64). A build task's builder and judge stages repeat once
    per expected attempt: the median builder runs per task (`attempts`) minus the attempts it already had."""
    status = t.get("status")
    if status == "done":
        return None, 0.0, "done"
    if status == "blocked":
        return None, None, "blocked: waits for Ben"
    plan = t.get("kind") == "plan"
    stages = PLAN_STAGES if plan else BUILD_STAGES
    elapsed = None
    if current and current.get("task_id") == t.get("id") and current.get("role") in stages:
        stage, elapsed = current["role"], float(current.get("elapsed_s") or 0.0)
    elif plan:
        stage = "planner" if status in ("todo", "tests_ok", None) else None
    else:
        stage = FIRST_STAGE.get(status)
    if stage is None:
        return None, None, f"unknown status {status!r}"
    rest = list(stages[stages.index(stage):])
    eta = max(0.0, med[stage]["median_s"] - (elapsed or 0.0)) + sum(med[s]["median_s"] for s in rest[1:])
    parts = [_stage_txt(s, med) for s in rest]
    extra = 0.0
    if not plan and stage in ("test_writer", "builder", "judge") and attempts:
        this = builds_done if stage == "judge" else builds_done + 1  # the attempt under way (or next)
        extra = max(0.0, float(attempts.get("median") or 1.0) - this)
        if extra:
            eta += extra * (med["builder"]["median_s"] + med["judge"]["median_s"])
            parts.append(f"{extra:g} more builder+judge attempt(s) (median {attempts.get('median'):g} per task"
                         f"{', n=' + str(attempts['n']) if attempts.get('n') else ', default'}; "
                         f"{builds_done} so far)")
    tail = f"; {fmt_dur(elapsed)} already spent in {stage}" if elapsed else ""
    return stage, eta, "estimate: " + " + ".join(parts) + tail


def build_task_s(med: dict, attempts: dict) -> float:
    """One whole build task from scratch: test writer, every expected attempt, review and merge."""
    a = max(1.0, float(attempts.get("median") or 1.0))
    return (med["test_writer"]["median_s"] + a * (med["builder"]["median_s"] + med["judge"]["median_s"])
            + med["reviewer"]["median_s"] + med["merge"]["median_s"])


def _current(runs: list[dict], cond: dict, now: datetime, titles: dict, limits: dict | None = None) -> dict | None:
    if not runs or not cond["alive"]:
        return None
    last = runs[-1]
    timeout = _num((limits or {}).get("agent_timeout_s")) or 1800.0
    if last["end"] is None:
        role, start, tid, title = last["role"], last["start"], last["task_id"], last["task_title"]
        if (now - start).total_seconds() > 2 * timeout + 300:  # agents are killed at their timeout: abandoned
            return None
    elif last["role"] == "builder" and cond.get("_phase") != "sleep" \
            and (now - last["end"]).total_seconds() < JUDGE_GAP_MAX_S:
        role, start, tid, title = "judge", last["end"], last["task_id"], last["task_title"]
    else:
        return None
    return {"role": role, "task_id": tid, "task_title": titles.get(tid) or title, "run_id": last["run_id"],
            "started": _iso(start), "elapsed_s": max(0.0, (now - start).total_seconds())}


def _tokens(state_root: Path, now: datetime, limits: dict, all_runs: list[list[dict]]) -> dict:
    day = now.strftime("%Y-%m-%d")
    by_lane = meter_by_lane(state_root, day)
    held = holds(state_root, now)
    hour_ago = now - timedelta(hours=1)
    providers = {}
    for p in PROVIDERS:
        lanes_used = {lane: (u or {}).get(p) if u is not None else None for lane, u in by_lane.items()}
        used = None if any(v is None for v in lanes_used.values()) else sum(lanes_used.values())
        cap = _num(limits.get(f"{p}_daily_token_cap"))
        burn = float(sum(r["tokens"] or 0 for runs in all_runs for r in runs
                         if r["provider"] == p and r["end"] is not None and hour_ago < r["end"] <= now))
        if cap is None or used is None:
            ttc = None
        elif used >= cap:
            ttc = 0.0
        else:
            ttc = (cap - used) / (burn / 3600.0) if burn > 0 else None
        providers[p] = {"used": used, "cap": int(cap) if cap is not None else None,
                        "fraction": (used / cap) if cap and used is not None else None, "by_lane": lanes_used,
                        "burn_per_h": burn, "time_to_cap_s": ttc, "held_until": _iso(held.get(p))}
    prefix = now.strftime("%Y%m%d") + "T"
    runs_today = 0
    for d in all_runs_dirs(state_root).values():
        try:
            with os.scandir(d) as it:
                runs_today += sum(1 for e in it if e.name.startswith(prefix))
        except OSError:
            pass
    rc = limits.get("agent_runs_per_day")
    return {"day": day, "resets_at": _iso(now.replace(hour=0, minute=0, second=0, microsecond=0)
                                          + timedelta(days=1)),
            "providers": providers, "runs_today": runs_today,
            "runs_cap": rc if isinstance(rc, int) and not isinstance(rc, bool) else None}


def _capped(tokens: dict) -> bool:
    for p in tokens["providers"].values():
        at_cap = p["cap"] is not None and p["used"] is not None and p["used"] >= p["cap"]
        if not (at_cap or p["held_until"]):
            return False
    return True


def _lane(forge_root: Path, state_root: Path, name: str, now: datetime, limits: dict, runs: list[dict],
          med: dict, tokens: dict, per_plan: tuple, attempts: dict | None = None) -> dict:
    sdir = lanes_mod.state_dir(state_root, name)
    q = _jread(sdir / "queue.json", dict)
    queue_ok = q is not BAD and (q is None or q.get("tasks") is None or isinstance(q.get("tasks"), list))
    q = q if isinstance(q, dict) else {}  # missing: no queue yet (empty); unreadable: unknown (queue_ok False)
    raw = q.get("tasks") if isinstance(q.get("tasks"), list) else []
    all_tasks = [t for t in raw if isinstance(t, dict) and isinstance(t.get("id"), str)]
    tasks = [t for t in all_tasks if t.get("status") != "superseded"]
    titles = {t["id"]: str(t.get("title", "")) for t in all_tasks}
    cond = conductor(state_root, name, sdir, now, limits)
    service = {"phase": cond.pop("_phase", None), "last_status": cond.pop("_last_status", None)}
    current = _current(runs, dict(cond, _phase=service["phase"]), now, titles, limits)
    if _exists(sdir / "KILL") or _exists(Path(state_root) / "shared" / "KILL") or not cond["alive"]:
        state = "stopped"
    elif _exists(sdir / "PAUSED"):
        state = "paused"
    elif current is not None:
        state = "running"
    elif service["last_status"] == "capped" or _capped(tokens):
        state = "capped"
    else:
        state = "idle"
    qs = _jread(sdir / "questions.json", dict)
    try:
        items = [] if qs is None else _channel_items(qs) if qs is not BAD else None
    except Exception:  # noqa: BLE001
        items = None
    open_q = None if items is None else len(items)
    open_ids = None if items is None else sorted(str(i.get("id")) for i in items)
    attempts = attempts or {"median": 1.0, "n": 0, "source": "default"}
    builds: dict = {}
    for r in runs:
        if r["role"] == "builder" and r["task_id"] and r["end"] is not None:
            builds[r["task_id"]] = builds.get(r["task_id"], 0) + 1
    rows = []
    for t in tasks:
        stage, eta, basis = task_eta(t, current, med, attempts, builds.get(t["id"], 0))
        rows.append({"id": t["id"], "title": str(t.get("title", "")), "kind": t.get("kind", "build"),
                     "status": t.get("status"), "stage": stage, "eta_s": eta, "eta_basis": basis,
                     "covers": cov_mod.claims(t)})
    # checkpoint: the layer's queue and spec coverage
    spec_rel = q.get("spec_file") or limits.get("spec_file") or DEFAULT_SPEC
    reqs = spec_requirements(forge_root, spec_rel)
    cp = {"layer": q.get("layer") if isinstance(q.get("layer"), str) else None,
          "tasks_done": sum(1 for t in tasks if t.get("status") == "done") if queue_ok else None,
          "tasks_total": len(tasks) if queue_ok else None,
          "spec_file": spec_rel if isinstance(spec_rel, str) else None, "covered": None, "partial": None,
          "requirements_total": None, "score": None, "requirements": []}
    if reqs and queue_ok:
        try:
            passed = ledger_passes(sdir)
            verified = {t["id"] for t in tasks if t.get("kind", "build") == "build"
                        and t.get("status") == "done" and t["id"] in passed}
            cov = cov_mod.compute(reqs, tasks, verified)  # a superseded task never blocks a requirement
            cp.update(covered=cov.covered, requirements_total=cov.total, score=float(cov.score),
                      partial=sum(1 for r in reqs if cov.status(r) == "partial"),
                      requirements=[{"id": r, "text": txt, "status": cov.status(r)} for r, txt in reqs.items()])
        except Exception:  # noqa: BLE001 - coverage unknown, never a crash
            pass
    build_s = build_task_s(med, attempts)
    open_rows = [r for r in rows if r["status"] not in ("done", "blocked")]
    known = sum(r["eta_s"] for r in open_rows if r["eta_s"] is not None)
    unknown = sum(1 for r in open_rows if r["eta_s"] is None)
    plans = sum(1 for r in open_rows if r["kind"] == "plan")
    unplanned = plans * per_plan[0] * build_s
    blocked = sum(1 for r in rows if r["status"] == "blocked")
    basis = (f"estimate: {len(open_rows)} open task(s) by stage medians ({fmt_dur(known)})"
             + (f" + {plans} plan(s) x {per_plan[0]:g} tasks each "
                f"({'median of ' + str(per_plan[1]) + ' plans' if per_plan[1] else 'default'})"
                f" x {fmt_dur(build_s)} per build task (with {attempts['median']:g} attempts)" if plans else "")
             + (f"; {blocked} blocked task(s) not counted (they wait for Ben)" if blocked else "")
             + (f"; {unknown} task(s) with an unknown status not counted" if unknown else ""))
    if not queue_ok:
        basis = "unknown: the lane's queue.json can't be read right now"
    cp.update(remaining_work_s=(known + unplanned) if queue_ok else None, eta_s=None, eta_basis=basis)
    return {"name": name, "layer": cp["layer"], "state": state, "conductor": cond, "service": service,
            "current": current, "open_questions": open_q, "open_question_ids": open_ids, "queue_ok": queue_ok,
            "tasks": rows, "checkpoint": cp}


def _channel_items(qs: dict) -> list:
    from core import channel
    return channel.queue_items(qs)


def _timeline(names: list[str], runs_by_lane: dict, lanes_out: list[dict], now: datetime) -> dict:
    start = now - timedelta(hours=TIMELINE_H)
    cur = {lane["name"]: (lane["current"] or {}).get("run_id") for lane in lanes_out
           if lane["current"] and lane["current"].get("role") != "judge"}
    out = {}
    for name in names:
        rows = []
        for r in runs_by_lane.get(name, []):
            running = r["end"] is None and cur.get(name) == r["run_id"]
            if r["start"] > now or (r["end"] or (now if running else r["start"])) <= start:
                continue
            rows.append({"run_id": r["run_id"], "role": r["role"], "task_id": r["task_id"],
                         "start": _iso(r["start"]), "end": _iso(r["end"]), "running": running, "ok": r["ok"],
                         "tokens": r["tokens"], "provider": r["provider"]})
        out[name] = rows
    return {"from": _iso(start), "to": _iso(now), "lanes": out}


def _project(forge_root: Path, lanes_out: list[dict], running: int) -> dict:
    doc = _json(Path(forge_root) / "docs" / "progress.json", {}, dict)
    phases = [{"name": str(p.get("name", "")), "done": bool(p.get("done"))}
              for p in (doc.get("phases") if isinstance(doc.get("phases"), list) else []) if isinstance(p, dict)]
    done = sum(1 for p in phases if p["done"])
    current = next((p["name"] for p in phases if not p["done"]), None)
    main = lanes_out[0]["checkpoint"] if lanes_out else {}
    if current is None:
        frac = 0.0
    elif main.get("requirements_total") and main.get("score") is not None:
        frac = main["score"] / main["requirements_total"]
    elif main.get("tasks_total") is None:
        frac = None  # main's queue can't be read right now: unknown, not 0
    elif main.get("tasks_total"):
        frac = main["tasks_done"] / main["tasks_total"]
    else:
        frac = 0.0
    works = [lane["checkpoint"].get("remaining_work_s") for lane in lanes_out]
    work = None if any(w is None for w in works) else sum(works)
    basis = (f"estimate: the current phase's planned work in {len(lanes_out)} lane(s) ({fmt_dur(work)} of agent "
             f"and judge time) / {running} lane(s) running; later phases are not planned yet, so they are not "
             "included") if work is not None else "unknown: a lane's queue can't be read right now"
    return {"phases": phases, "phases_done": done, "phases_total": len(phases), "current_phase": current,
            "current_fraction": frac,
            "overall": ((done + frac) / len(phases)) if phases and frac is not None else None,
            "eta_s": 0.0 if current is None else (work / running) if work is not None else None,
            "eta_basis": basis}


def snapshot(forge_root, now: datetime | None = None) -> dict:
    """Everything the live dashboard shows (R64). Never writes; never raises on bad state files."""
    forge_root = Path(forge_root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state_root = forge_root / "state"
    limits = _json(forge_root / "charter" / "limits.json", {}, dict)
    names = lane_names(state_root)
    dirs = all_runs_dirs(state_root)
    runs_by_lane = {}
    for name in dict.fromkeys(names + list(dirs)):
        runs_by_lane[name] = read_runs(lanes_mod.state_dir(state_root, name) / "runs")
    all_runs = list(runs_by_lane.values())
    med = stage_medians(all_runs)
    per_plan = tasks_per_plan(all_runs)
    attempts = builder_attempts(all_runs)
    tokens = _tokens(state_root, now, limits, all_runs)
    lanes_out = []
    for name in names:
        try:
            lanes_out.append(_lane(forge_root, state_root, name, now, limits, runs_by_lane.get(name, []),
                                   med, tokens, per_plan, attempts))
        except Exception as e:  # noqa: BLE001 - one bad lane never hides the others
            lanes_out.append({"name": name, "layer": None, "state": "unknown", "error": f"{type(e).__name__}: {e}",
                              "conductor": {"alive": None, "heartbeat_age_s": None},
                              "service": {"phase": None, "last_status": None},
                              "current": None, "open_questions": None, "tasks": [],
                              "checkpoint": {"layer": None, "tasks_done": None, "tasks_total": None,
                                             "spec_file": None, "covered": None, "partial": None,
                                             "requirements_total": None, "score": None, "requirements": [],
                                             "remaining_work_s": None, "eta_s": None, "eta_basis": "unknown"}})
    running = max(1, sum(1 for lane in lanes_out if lane["state"] == "running"))
    for lane in lanes_out:
        cp = lane["checkpoint"]
        if cp.get("remaining_work_s") is None:
            continue
        on_layer = max(1, sum(1 for x in lanes_out if x["state"] == "running" and x["layer"] == lane["layer"]))
        cp["eta_s"] = cp["remaining_work_s"] / on_layer
        if on_layer > 1:
            cp["eta_basis"] += f"; / {on_layer} lanes running on {lane['layer']}"
    return {"generated_at": _iso(now), "lanes": lanes_out, "project": _project(forge_root, lanes_out, running),
            "tokens": tokens, "timeline": _timeline(names, runs_by_lane, lanes_out, now), "stage_medians": med,
            "attempts": attempts}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Forge's live dashboard data (read-only)")
    ap.add_argument("--json", action="store_true", help="print the whole snapshot as JSON")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="the Forge folder")
    a = ap.parse_args(argv)
    snap = snapshot(Path(a.root))
    if a.json:
        print(json.dumps(snap, indent=1))
        return 0
    for lane in snap["lanes"]:
        cur = lane["current"]
        now_txt = f"{cur['role']} on {cur['task_id'] or '-'} for {fmt_dur(cur['elapsed_s'])}" if cur else "nothing"
        cp = lane["checkpoint"]
        print(f"{lane['name']}: {lane['state']}; now {now_txt}; {cp['layer']}: {cp['tasks_done']}/{cp['tasks_total']}"
              f" tasks, coverage {cp['covered']}/{cp['requirements_total']}; checkpoint in {fmt_dur(cp['eta_s'])}"
              " (estimate)")
    for p, v in snap["tokens"]["providers"].items():
        print(f"{p}: {v['used']} of {v['cap']} today, {v['burn_per_h']:.0f}/h")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
