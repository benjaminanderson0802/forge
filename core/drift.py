"""Drift keeper bookkeeping and the D-026 stall rules. Plain code, no AI, no imports from core.bootstrap.

State lives in `state/drift.json` (durable writes):
- `counted`: merged task ids already recorded, so a merge is counted exactly once, across restarts.
- `no_gain`: consecutive recorded merges that did not raise spec coverage.
- `active_mark`: the active-time total at the last recorded merge (or the last re-plan); the "no merge in
  2 active hours" window is measured from here.
- `stall`: a pending stall trigger that must bring in the Drift keeper, or None.
- `replan`: a pending re-plan for the Manager ({id, trigger, reasons, attempts}), or None.
- `auto_replans`: re-plans accepted without Ben since coverage last rose.

Active time (`state/activity.json`) is the sum of seconds spent doing work: agent runs and judges. Waiting
(pauses, cap waits, idle sleeps, restarts) never adds to it.
"""
from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.finalize import _atomic_write

DRIFT_FILE = "drift.json"
ACTIVITY_FILE = "activity.json"
HISTORY_KEEP = 50


def load(state: Path) -> Optional[dict]:
    try:
        d = json.loads((Path(state) / DRIFT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("counted"), list) else None


def save(state: Path, d: dict) -> None:
    d["history"] = list(d.get("history") or [])[-HISTORY_KEEP:]
    _atomic_write(Path(state) / DRIFT_FILE, d)


def adopt(marks: list[str], done_ids: Iterable[str], drift_due: bool, active_s: float) -> dict:
    """First use: everything already merged or done is the baseline, never credited to a new merge. When a
    drift check is pending, the newest mark is the merge it is for, so it stays uncounted."""
    pending = marks[-1] if drift_due and marks else None
    counted: list[str] = []
    for tid in list(marks) + sorted(set(done_ids)):
        if tid != pending and tid not in counted:
            counted.append(tid)
    return {"counted": counted, "no_gain": 0, "active_mark": float(active_s), "stall": None, "replan": None,
            "auto_replans": 0, "replan_seq": 0, "score": None, "history": []}


def record_merges(d: dict, marks: list[str], verified: set[str], active_s: float,
                  score: Optional[Callable[[set[str]], Fraction]], deferred: Iterable[str] = ()) -> list[dict]:
    """Record every marked merge not yet counted (skipping `deferred` ones). Each merge's gain is measured on the
    same task set: score(counted ∪ {tid}) > score(counted), counting only verified tasks. With no score function
    (no spec) the gain is unknown (None): the window still resets but no_gain does not move. Mutates d."""
    deferred = set(deferred)
    events = []
    for tid in marks:
        if tid in d["counted"] or tid in deferred:
            continue
        gain = None
        if score is not None:
            base = set(d["counted"]) & verified
            before, after = score(base), score(base | ({tid} & verified))
            gain = after > before
            d["score"] = str(after)
        d["counted"].append(tid)
        if gain is True:
            d["no_gain"] = 0
            d["auto_replans"] = 0
        elif gain is False:
            d["no_gain"] = int(d.get("no_gain", 0)) + 1
        d["active_mark"] = float(active_s)
        ev = {"tid": tid, "gain": gain, "score": d.get("score")}
        d.setdefault("history", []).append(ev)
        events.append(ev)
    d["history"] = d["history"][-HISTORY_KEEP:]
    return events


def no_gain_due(d: dict, limit: int) -> bool:
    return int(d.get("no_gain", 0)) >= int(limit)


def idle_due(d: dict, active_s: float, limit_s: float) -> bool:
    return float(active_s) - float(d.get("active_mark", 0.0)) >= float(limit_s)


def restart_window(d: dict, active_s: float) -> None:
    """After a stall has been acted on: fresh counters, so the same stall does not fire again at once."""
    d["no_gain"] = 0
    d["active_mark"] = float(active_s)


def new_replan(d: dict, reasons: list[str], trigger: str) -> str:
    d["replan_seq"] = int(d.get("replan_seq", 0)) + 1
    rid = f"R{d['replan_seq']}"
    d["replan"] = {"id": rid, "trigger": str(trigger)[:500], "reasons": [str(x)[:2000] for x in reasons][:20],
                   "attempts": 0, "notes": []}
    return rid


class Activity:
    """Accumulated active seconds, persisted in state/activity.json."""

    def __init__(self, state: Path):
        self.path = Path(state) / ACTIVITY_FILE

    def total(self) -> float:
        try:
            v = json.loads(self.path.read_text(encoding="utf-8")).get("active_s", 0.0)
        except (OSError, ValueError, AttributeError):
            return 0.0
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 else 0.0

    def add(self, seconds) -> None:
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) \
                or seconds <= 0:
            return
        _atomic_write(self.path, {"active_s": self.total() + float(seconds)})
