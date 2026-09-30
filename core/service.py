"""The always-on service around the conductor (design §5; D-007, D-018, D-019, D-024). Plan: 2026-09-30-layer-1d.md.

Plain code, standard library only. Everything here lives in state/service/, a sibling of the conductor's
state/bootstrap/, so the heartbeat thread can write while an agent runs without tripping the tamper check (R14).

    python -m core.service status      # what the status page and digest read
    python -m core.service stop        # the kill switch (same as the Stop Forge shortcut)
    python -m core.service wake        # end the service's sleep now
    python -m core.service watchdog    # run every 5 minutes by the "Forge watchdog" task
"""
from __future__ import annotations

import ctypes
import os

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
