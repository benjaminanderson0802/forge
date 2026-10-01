"""Pure reporting over entry ledger records: select, summarize, flag_window, to_cents."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .model import Entry

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Summary:
    count: int
    total: Decimal
    by_hts: dict
    by_month: dict
    earliest: Optional[date]
    latest: Optional[date]


@dataclass(frozen=True)
class WindowFlag:
    entry: Entry
    status: str
    days_left: int


def select(entries, hts_prefixes=(), start=None, end=None):
    """Entries in input order matching any HTS prefix and the inclusive date bounds."""
    prefixes = tuple(hts_prefixes)
    return [
        e for e in entries
        if (not prefixes or e.hts.startswith(prefixes))
        and (start is None or start <= e.entry_date)
        and (end is None or e.entry_date <= end)
    ]


def summarize(entries):
    """Exact, unrounded Decimal totals grouped by HTS heading and by month."""
    entries = list(entries)
    total = Decimal("0")
    by_hts = {}
    by_month = {}
    for e in entries:
        total += e.duty
        hts_key = e.hts[:8]
        by_hts[hts_key] = by_hts.get(hts_key, Decimal("0")) + e.duty
        month_key = e.entry_date.strftime("%Y-%m")
        by_month[month_key] = by_month.get(month_key, Decimal("0")) + e.duty
    dates = [e.entry_date for e in entries]
    return Summary(
        count=len(entries),
        total=total,
        by_hts=dict(sorted(by_hts.items())),
        by_month=dict(sorted(by_month.items())),
        earliest=min(dates) if dates else None,
        latest=max(dates) if dates else None,
    )


def flag_window(entries, window_days, as_of):
    """One WindowFlag per entry, in input order, against a window of whole days."""
    if isinstance(window_days, bool) or not isinstance(window_days, int):
        raise TypeError("window_days must be an int, got %r" % (window_days,))
    if window_days < 0:
        raise ValueError("window_days must be >= 0, got %d" % window_days)
    flags = []
    for e in entries:
        age = (as_of - e.entry_date).days
        status = "open" if age <= window_days else "closed"
        flags.append(WindowFlag(entry=e, status=status, days_left=max(0, window_days - age)))
    return flags


def to_cents(amount):
    """Round a Decimal to cents, half up, for display only."""
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)
