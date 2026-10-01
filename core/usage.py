"""Forge-wide daily token meter per provider (UTC days). Written only by plain code.

R48/R50 (Layer 1D): a provider can also be on hold until a plan limit window resets (holds.json), and
limits["agent_runs_per_day"] caps every provider by the number of agent launches today, counted from the
conductor's run records (state/runs/<YYYYMMDDTHHMMSS>-<role>-<id>)."""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


class Meter:
    """R58 (lanes): with `lock` (a core.lanes.SharedLock) the meter and holds are shared by several conductor
    processes: every read-modify-write happens under that lock, and `runs_dirs` lists every lane's runs/ folder so
    agent_runs_per_day counts launches across all lanes. Without them it is the single-conductor meter."""

    def __init__(self, state_dir: Path, clock: Callable[[], datetime] | None = None, *,
                 lock=None, runs_dirs: Callable[[], list[Path]] | None = None):
        self.path = Path(state_dir) / "meter.json"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock, self.runs_dirs = lock, runs_dirs

    def _locked(self):
        return self.lock if self.lock is not None else nullcontext()

    def _replace(self, data: dict, target: Path) -> None:
        if self.lock is not None:
            from core.lanes import write_json_atomic
            write_json_atomic(target, data)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, target)

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _read(self) -> dict:
        with self._locked():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}

    def used_today(self, provider: str) -> int:
        return int(self._read().get(self._day(), {}).get(provider, 0))

    def add(self, provider: str, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        with self._locked():  # R58: one read-modify-write at a time across every lane
            data = self._read()
            day = data.setdefault(self._day(), {})
            day[provider] = int(day.get(provider, 0)) + tokens
            self._replace(data, self.path)

    def over(self, provider: str, limits: dict) -> bool:
        cap = limits.get(f"{provider}_daily_token_cap")
        if cap is not None and self.used_today(provider) >= cap:
            return True
        runs_cap = limits.get("agent_runs_per_day")  # T1D3: a hard bound on launches, whatever tokens say
        if isinstance(runs_cap, int) and not isinstance(runs_cap, bool) and self.runs_today() >= runs_cap:
            return True
        return self.held(provider) is not None

    # ------------------------------------------------------------------ T1D3
    @property
    def _holds_path(self) -> Path:
        return self.path.parent / "holds.json"

    def _holds(self) -> dict:
        with self._locked():
            try:
                data = json.loads(self._holds_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}

    def held(self, provider: str) -> datetime | None:
        """The time a hold on this provider ends, or None if it isn't held now."""
        try:
            until = datetime.fromisoformat(str(self._holds()[provider]))
        except (KeyError, ValueError, TypeError):
            return None
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until if until > self.clock() else None

    def hold(self, provider: str, until: datetime) -> None:
        """Hold a provider until a limit window resets. A later hold extends an earlier one, never shortens it."""
        with self._locked():
            data = self._holds()
            cur = self.held(provider)
            if cur is not None and cur >= until:
                return
            data[provider] = until.astimezone(timezone.utc).isoformat()
            self._replace(data, self._holds_path)

    def runs_today(self) -> int:
        prefix = self.clock().astimezone(timezone.utc).strftime("%Y%m%d") + "T"
        n = 0
        for d in (self.runs_dirs() if self.runs_dirs is not None else [self.path.parent / "runs"]):
            try:
                with os.scandir(d) as it:
                    n += sum(1 for e in it if e.name.startswith(prefix))
            except OSError:
                continue
        return n


# R48/R50: one detector for a provider's own usage limit (main's R48 wording and Layer 1D's plan-window wording).
_LIMIT = re.compile(r"hit your (?:\w+ )?limit|(?:session|usage|rate|weekly)[ _-]?limit|limit (?:reached|exceeded)"
                    r"|quota exceeded|too many requests", re.I)
_RESET = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b\s*(?:\(([^)]+)\))?", re.I)
LIMIT_FALLBACK = timedelta(minutes=30)  # R48's hold length when no reset time can be read


def limit_hold_until(text: str | None, now: datetime) -> datetime | None:
    """R48/R50: if an agent's error says its provider's usage limit is used up, when to try again; otherwise None.
    Reads "resets 6am (America/Chicago)" when it can (zoneinfo needs a tz database, which Windows may lack);
    otherwise 30 minutes (R48). Always between 5 minutes and 24 hours from now."""
    if not text or not _LIMIT.search(text):
        return None
    until = now + LIMIT_FALLBACK
    m = _RESET.search(text)
    if m and m.group(4):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group(4).strip())
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            local = now.astimezone(tz)
            t = local.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
            if t <= local:
                t = (t + timedelta(days=1)).replace(tzinfo=tz)
            until = t.astimezone(timezone.utc)
        except Exception:  # noqa: BLE001 - unknown zone or no tz database: keep the fallback
            until = now + LIMIT_FALLBACK
    return min(max(until, now + timedelta(minutes=5)), now + timedelta(hours=24))
