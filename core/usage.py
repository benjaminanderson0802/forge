"""Forge-wide daily token meter per provider (UTC days). Written only by plain code."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class Meter:
    def __init__(self, state_dir: Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(state_dir) / "meter.json"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _read(self) -> dict:
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
        data = self._read()
        day = data.setdefault(self._day(), {})
        day[provider] = int(day.get(provider, 0)) + tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def over(self, provider: str, limits: dict) -> bool:
        cap = limits.get(f"{provider}_daily_token_cap")
        return cap is not None and self.used_today(provider) >= cap
