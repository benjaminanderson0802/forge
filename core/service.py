"""The always-on service around the conductor (design §5; D-007, D-018, D-019, D-024). Plan: 2026-09-30-layer-1d.md.

Plain code, standard library only. Everything here lives in state/service/, a sibling of the conductor's
state/bootstrap/, so the heartbeat thread can write while an agent runs without tripping the tamper check (R14).

    python -m core.service status      # what the status page and digest read
    python -m core.service stop        # the kill switch (same as the Stop Forge shortcut)
    python -m core.service wake        # end the service's sleep now
    python -m core.service watchdog    # run every 5 minutes by the "Forge watchdog" task
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

IS_WIN = os.name == "nt"

DEFAULTS = {
    "idle_after_s": 600,        # §5: idle once Ben has been away from mouse and keyboard for 10 minutes
    "active_step_gap_s": 30,    # D-019 "lighter while Ben is active": pause between work steps
    "idle_poll_s": 60,          # longest sleep between steps; each step reads Ben's email
    "tick_s": 2,                # how quickly a sleep notices KILL or WAKE
    "beat_s": 30,               # heartbeat interval
    "step_stall_s": 14400,      # one step running longer than this is a stall: exit, let the watchdog restart
    "heartbeat_stale_s": 600,   # the watchdog treats an older heartbeat as a frozen or dead service
}


def setting(limits: dict, key: str) -> float:
    v = limits.get(key, DEFAULTS[key])
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else float(DEFAULTS[key])


# ------------------------------------------------------------------ activity (T1D1)
def _user32():
    return ctypes.windll.user32  # type: ignore[attr-defined]


def _kernel32():
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _idle_from_ticks(now_tick: int, last_input_tick: int) -> float:
    """GetTickCount is 32-bit and wraps about every 49.7 days; unsigned modular difference, in seconds."""
    return ((int(now_tick) - int(last_input_tick)) & 0xFFFFFFFF) / 1000.0


def idle_seconds() -> float | None:
    """Seconds since Ben last touched the mouse or keyboard (Windows GetLastInputInfo), or None if unknown.
    Read-only: it never touches input, the screen or any window (D-019)."""
    if not IS_WIN:
        return None
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not _user32().GetLastInputInfo(ctypes.byref(info)):
            return None
        return _idle_from_ticks(_kernel32().GetTickCount(), info.dwTime)
    except Exception:  # noqa: BLE001 - unknown means "assume Ben is here"
        return None


def mode(idle_s: float | None, limits: dict) -> dict:
    """§5: while Ben has been active in the last 10 minutes (or we can't tell): one agent at a time, no browsers,
    and a pause between work steps. Once he's been idle 10+ minutes: up to max_parallel_idle agents."""
    if idle_s is not None and idle_s >= setting(limits, "idle_after_s"):
        try:
            n = max(1, int(limits.get("max_parallel_idle", 1)))
        except (TypeError, ValueError):
            n = 1
        return {"state": "idle", "idle_s": idle_s, "max_agents": n, "browsers": True, "step_gap_s": 0}
    return {"state": "unknown" if idle_s is None else "active", "idle_s": idle_s, "max_agents": 1,
            "browsers": False, "step_gap_s": setting(limits, "active_step_gap_s")}


# ------------------------------------------------------------------ files (T1D2)
def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(json.dumps(data, indent=2, sort_keys=True, default=str).encode("utf-8"))
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _log(root: Path, msg: str) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "service.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass


def wake(root: Path) -> None:
    """End the service's current sleep (status page, spec approvals, anything that means "look now")."""
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "WAKE").write_bytes(b"")


def request_stop(bootstrap_state: Path, reason: str) -> None:
    """D-024: the kill switch. Same file the Stop Forge shortcut and an email STOP write. An existing stop and its
    reason are kept; only Ben clears it (Start Forge)."""
    kill = Path(bootstrap_state) / "KILL"
    if kill.exists():
        return
    Path(bootstrap_state).mkdir(parents=True, exist_ok=True)
    tmp = kill.with_name("KILL.tmp")
    tmp.write_bytes((reason.strip()[:300] + "\n").encode("utf-8"))
    os.replace(tmp, kill)


# ------------------------------------------------------------------ health (T1D2)
def _exit_on_stall(msg: str) -> None:
    """A hung step can't be unwound from inside: kill our whole process tree and exit. The 5-minute task trigger
    starts a fresh conductor, and crash recovery (ledger, merge journal, worktrees) picks up the half-done step."""
    try:
        from core import agents
        agents.kill_live()
    except Exception:  # noqa: BLE001
        pass
    if IS_WIN:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(os.getpid())], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            pass
    os._exit(3)


class Health:
    """Heartbeat from its own thread (so it keeps beating while an agent runs) plus stall detection."""

    def __init__(self, root: Path, limits: dict, *, clock: Callable[[], float] = time.time,
                 on_stall: Callable[[str], None] | None = None):
        self.root, self.limits, self.clock = Path(root), limits, clock
        self.on_stall = on_stall or _exit_on_stall
        self.started_at = clock()
        self.phase, self.since = "startup", self.started_at
        self.last_status: str | None = None
        self.mode: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_phase(self, phase: str) -> None:
        self.phase, self.since = phase, self.clock()

    def _write(self, now: float) -> None:
        _write_json(self.root / "heartbeat.json", {
            "pid": os.getpid(), "at": now, "phase": self.phase, "since": self.since,
            "started_at": self.started_at, "last_status": self.last_status, "mode": self.mode})

    def beat(self) -> bool:
        """One heartbeat. Returns True if a stall was found (and on_stall was called)."""
        now = self.clock()
        try:
            self._write(now)
        except OSError as e:  # R12: a failed heartbeat is logged, never fatal
            _log(self.root, f"heartbeat failed: {e!r}")
        limit = setting(self.limits, "step_stall_s")
        if self.phase == "step" and now - self.since > limit:
            msg = f"stall: one step has run {int(now - self.since)} s (limit {int(limit)} s); exiting for a restart"
            _log(self.root, msg)
            self.on_stall(msg)
            return True
        return False

    def start(self) -> "Health":
        def loop() -> None:
            while True:
                try:
                    self.beat()
                except Exception as e:  # noqa: BLE001 - the heartbeat thread never dies
                    _log(self.root, f"heartbeat error: {e!r}")
                if self._stop.wait(setting(self.limits, "beat_s")):
                    return

        self._thread = threading.Thread(target=loop, name="forge-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.phase = "exited"
        try:
            self._write(self.clock())
        except OSError:
            pass


# ------------------------------------------------------------------ the loop (T1D2)
class Service:
    """Runs the conductor's own loop (errors, backoff, KILL) with a kill-aware, wakeable sleep and
    activity-aware pacing, and publishes status.json after every step."""

    def __init__(self, conductor, root: Path, limits: dict, *, health: Health | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 idle: Callable[[], float | None] = idle_seconds):
        self.c, self.root, self.limits = conductor, Path(root), limits
        self.health = health or Health(self.root, limits, clock=clock)
        self._sleep, self.clock, self.idle = sleep, clock, idle
        self.mode = mode(None, limits)

    def _killed(self) -> bool:
        return (Path(self.c.state) / "KILL").exists()

    def nap(self, seconds: float) -> None:
        """Sleep until the time is up, KILL appears, or someone wakes us. Checks every tick_s."""
        self.health.set_phase("sleep")
        try:
            end = self.clock() + float(seconds)
            tick = setting(self.limits, "tick_s")
            while not self._killed():
                w = self.root / "WAKE"
                if w.exists():
                    try:
                        w.unlink()
                    except OSError:
                        pass
                    return
                left = end - self.clock()
                if left <= 0:
                    return
                self._sleep(min(tick, left))
        finally:
            self.health.set_phase("step")

    def on_step(self, status: str) -> None:
        self.health.last_status = status
        self.mode = mode(self.idle(), self.limits)
        self.health.mode = self.mode["state"]
        try:
            snap = snapshot(Path(self.c.state), self.root, self.limits)
            snap["last_status"], snap["mode"] = status, self.mode
            _write_json(self.root / "status.json", snap)
        except Exception as e:  # noqa: BLE001 - status is for Ben's eyes; it never stops the work
            _log(self.root, f"status write failed: {e!r}")
        if status == "worked" and self.mode["step_gap_s"] > 0 and not self._killed():
            self.nap(self.mode["step_gap_s"])  # D-019: lighter while Ben is active
        self.health.set_phase("step")

    def serve(self) -> str:
        self.health.set_phase("step")
        return self.c.run(idle_sleep_s=int(setting(self.limits, "idle_poll_s")),
                          heartbeat=Path(self.c.state) / "conductor.heartbeat", sleep=self.nap, on_step=self.on_step)


# ------------------------------------------------------------------ status data (T1D2)
def _next_utc_midnight(now: datetime) -> datetime:
    d = now.astimezone(timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)


def snapshot(bootstrap_state: Path, root: Path, limits: dict, now: datetime | None = None) -> dict:
    """Everything the status page and the daily digest (1E) need, read-only."""
    from core.usage import Meter
    st, root = Path(bootstrap_state), Path(root)
    now = now or datetime.now(timezone.utc)

    def flag(name: str) -> str | None:
        try:
            return (st / name).read_text(encoding="utf-8", errors="replace").strip()[:300] or name
        except OSError:
            return None

    hb = _read_json(root / "heartbeat.json", {})
    hb = hb if isinstance(hb, dict) else {}
    at = hb.get("at")
    fresh = isinstance(at, (int, float)) and hb.get("phase") != "exited" and \
        0 <= now.timestamp() - at <= setting(limits, "heartbeat_stale_s")
    q = _read_json(st / "queue.json", {})
    tasks = q.get("tasks", []) if isinstance(q, dict) else []
    counts: dict[str, int] = {}
    for t in tasks:
        if isinstance(t, dict):
            counts[str(t.get("status"))] = counts.get(str(t.get("status")), 0) + 1
    current = next((t.get("id") for t in tasks if isinstance(t, dict) and t.get("status") == "tests_ok"),
                   next((t.get("id") for t in tasks if isinstance(t, dict) and t.get("status") == "todo"), None))
    qs = _read_json(st / "questions.json", {})
    open_q = sum(1 for v in (qs.values() if isinstance(qs, dict) else [])
                 if isinstance(v, dict) and v.get("status") == "open")
    meter = Meter(st, clock=lambda: now)
    caps = {}
    for key in sorted(limits):
        if key.endswith("_daily_token_cap"):
            p = key[: -len("_daily_token_cap")]
            caps[p] = {"used": meter.used_today(p), "cap": limits[key], "over": meter.over(p, limits)}
            held = getattr(meter, "held", None)
            if held:
                h = held(p)
                caps[p]["held_until"] = h.isoformat() if h else None
    try:
        with (st / "errors.log").open(encoding="utf-8", errors="replace") as f:
            errors = [ln.rstrip("\n") for ln in f.readlines()[-5:]]
    except OSError:
        errors = []
    snap = {
        "at": now.isoformat(), "running": fresh, "pid": hb.get("pid"), "phase": hb.get("phase"),
        "phase_for_s": (now.timestamp() - hb["since"]) if isinstance(hb.get("since"), (int, float)) else None,
        "last_status": hb.get("last_status"), "mode": hb.get("mode"),
        "kill": flag("KILL"), "paused": flag("PAUSED"),
        "caps": caps, "cap_resets_at": _next_utc_midnight(now).isoformat(),
        "tasks": counts, "current": current, "open_questions": open_q, "recent_errors": errors,
    }
    runs = getattr(meter, "runs_today", None)
    if runs:
        snap["runs_today"], snap["runs_cap"] = runs(), limits.get("agent_runs_per_day")
    return snap


# ------------------------------------------------------------------ command line
def main(argv: list[str], forge: Path | None = None) -> int:
    forge = Path(forge) if forge else Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(prog="python -m core.service")
    ap.add_argument("cmd", choices=["status", "stop", "wake"])
    ap.add_argument("--reason", default="stopped from the command line")
    a = ap.parse_args(argv)
    st, root = forge / "state" / "bootstrap", forge / "state" / "service"
    try:
        from core.agents import load_limits
        limits = load_limits(forge)
    except (OSError, ValueError):
        limits = {}
    if a.cmd == "stop":
        request_stop(st, a.reason)
        print("Forge is stopped. Nothing new will start. Run Start Forge to resume.")
    elif a.cmd == "wake":
        wake(root)
    else:
        print(json.dumps(snapshot(st, root, limits), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
