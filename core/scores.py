"""Scores per agent role (Phase 2 design section 5, proposed D-041).

Plain code, standard library only. The ledger is the source of truth (D-001):
every count comes from ledger events; the easy_outs.jsonl, challenges.jsonl and
audits.jsonl sidecars are never read.

  * runs: one per "agent_run" event (the conductor records each completed run)
  * builder: false claims, easy-outs, overturned blockers, escaped defects
  * troubleshooter: overturned dead ends and overturned blocked verdicts
  * reviewer: confirmed defects in work that passed
  * auditor: unconfirmed findings, dropped findings, invalid runs
  * challenger: overturns it could not verify, stands without evidence

Scoring is defensive: malformed events or data are skipped, never raised on.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ledger import Ledger

ROLE_COUNTS = {
    "builder": ("false_claims", "easy_outs", "overturned", "escaped_defects"),
    "troubleshooter": ("overturned_dead_ends", "overturned_blocked"),
    "reviewer": ("missed_defects",),
    "auditor": ("unconfirmed_findings", "dropped_findings", "invalid_runs"),
    "challenger": ("unverified_overturns", "stands_without_evidence"),
}
WINDOWS = {"last_7_days": 7 * 24, "last_24_hours": 24}   # hours
SCORES_FILE = "scores.json"

# challenge target -> (role, count) when the verdict is "overturned"
_OVERTURNED = {
    "blocker": ("builder", "overturned"),
    "dead_end": ("troubleshooter", "overturned_dead_ends"),
    "blocked": ("troubleshooter", "overturned_blocked"),
}
# challenge outcome -> challenger count
_OUTCOMES = {
    "unverified_overturn": "unverified_overturns",
    "stands_no_evidence": "stands_without_evidence",
}


def parse_at(value) -> datetime | None:
    """An ISO 8601 text (trailing Z accepted) as an aware datetime; naive means UTC; else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(value) -> bool:
    return isinstance(value, str) and bool(value)


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _valid(events) -> list:
    """Only dict events with a dict payload; everything else is skipped."""
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict) and isinstance(e.get("payload"), dict)]


def _score_window(events: list, passed: set) -> dict:
    roles = {role: {"runs": 0, "counts": dict.fromkeys(names, 0)} for role, names in ROLE_COUNTS.items()}
    extra = {}
    confirmed, unconfirmed = set(), set()
    for e in events:
        action, payload = e.get("action"), e["payload"]
        if e.get("note") == "false_claim":
            roles["builder"]["counts"]["false_claims"] += 1
        if action == "agent_run":
            role = payload.get("role")
            if _text(role):
                row = roles.get(role) or extra.setdefault(role, {"runs": 0, "counts": {}})
                row["runs"] += 1
        elif action == "run_report":
            if isinstance(payload.get("easy_out"), dict):
                roles["builder"]["counts"]["easy_outs"] += 1
        elif action == "challenge":
            target = payload.get("target")
            if payload.get("verdict") == "overturned" and isinstance(target, str) and target in _OVERTURNED:
                role, name = _OVERTURNED[target]
                roles[role]["counts"][name] += 1
            outcomes = payload.get("outcomes")
            if isinstance(outcomes, list):
                for item in outcomes:
                    if isinstance(item, str) and item in _OUTCOMES:
                        roles["challenger"]["counts"][_OUTCOMES[item]] += 1
        elif action == "audit":
            kind = payload.get("kind")
            if kind in ("confirmed", "unconfirmed"):
                of, finding = payload.get("of"), payload.get("finding")
                if _text(of) and _text(finding):  # validated before use as a hashable pair
                    (confirmed if kind == "confirmed" else unconfirmed).add((of, finding))
            elif kind == "report":
                dropped = payload.get("dropped")
                if _int(dropped) > 0:
                    roles["auditor"]["counts"]["dropped_findings"] += dropped
            elif kind == "invalid":
                roles["auditor"]["counts"]["invalid_runs"] += 1
    roles["builder"]["counts"]["escaped_defects"] = len(confirmed)
    roles["reviewer"]["counts"]["missed_defects"] = sum(1 for of, _ in confirmed if of in passed)
    roles["auditor"]["counts"]["unconfirmed_findings"] = len(unconfirmed)
    roles.update(extra)
    for row in roles.values():
        runs = row["runs"]
        row["rates"] = {name: (round(n / runs, 4) if runs > 0 else None) for name, n in row["counts"].items()}
    return {"roles": roles}


def score_events(events: list, now: datetime) -> dict:
    """Pure: role scores over all events and over each window ending at now."""
    events = _valid(events)
    passed = {e["contract_id"] for e in events if e.get("action") == "pass" and _text(e.get("contract_id"))}
    data = {"generated_at": now.isoformat(), "total": _score_window(events, passed)}
    stamped = [(parse_at(e["payload"].get("at")), e) for e in events]
    for window, hours in WINDOWS.items():
        start = now - timedelta(hours=hours)
        data[window] = _score_window([e for at, e in stamped if at is not None and at >= start], passed)
    return data


def scores(ledger=None, state=None, now=None) -> dict:
    """Role scores read from the ledger's event log."""
    if ledger is None:
        ledger = Ledger(state)
    if now is None:
        now = datetime.now(timezone.utc)
    return score_events(ledger.events(), now)


def write_scores(state, data) -> Path:
    """Write <state>/scores.json atomically (temp file, flush, fsync, replace)."""
    folder = Path(state)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SCORES_FILE
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-scores-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _roles(data, window) -> dict:
    block = data.get(window) if isinstance(data, dict) else None
    roles = block.get("roles") if isinstance(block, dict) else None
    return roles if isinstance(roles, dict) else {}


def _counts(row) -> dict:
    counts = row.get("counts") if isinstance(row, dict) else None
    return counts if isinstance(counts, dict) else {}


def record_line(data, role) -> str:
    """The one-line 7-day record shown to an agent of this role."""
    try:
        roles = _roles(data, "last_7_days")
        counts = _counts(roles.get(role)) if isinstance(role, str) else {}
    except Exception:
        counts = {}
    overturned = sum(_int(counts.get(k)) for k in ("overturned", "overturned_dead_ends", "overturned_blocked"))
    return (f"YOUR RECORD (last 7 days): false claims {_int(counts.get('false_claims'))}, "
            f"easy-outs {_int(counts.get('easy_outs'))}, overturned {overturned}")


def alarm_counts(data) -> dict[str, int]:
    """false claims + easy-outs per role over the last 24 hours."""
    result = {}
    for role, row in _roles(data, "last_24_hours").items():
        if isinstance(role, str):
            counts = _counts(row)
            result[role] = _int(counts.get("false_claims")) + _int(counts.get("easy_outs"))
    return result


def digest_lines(data) -> list[str]:
    """The Scores section of the digest, over the last 7 days."""
    roles = _roles(data, "last_7_days")
    order = [r for r in ROLE_COUNTS if r in roles]
    order += sorted(r for r in roles if isinstance(r, str) and r not in ROLE_COUNTS)
    lines = []
    for role in order:
        row = roles[role]
        if not isinstance(row, dict):
            continue
        runs, counts = _int(row.get("runs")), _counts(row)
        rates = row.get("rates") if isinstance(row.get("rates"), dict) else {}
        names = ROLE_COUNTS.get(role) or sorted(k for k in counts if isinstance(k, str))
        if not runs and not any(_int(counts.get(n)) for n in names):
            continue
        line = f"- {role}: {runs} runs"
        for name in names:
            line += f"; {name.replace('_', ' ')} {_int(counts.get(name))}"
            rate = rates.get(name)
            if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                line += f" ({rate:.2f} per run)"
        lines.append(line)
    if not lines:
        return ["Scores (last 7 days): no agent runs or verdicts recorded yet."]
    return ["Scores (last 7 days):", *lines]
