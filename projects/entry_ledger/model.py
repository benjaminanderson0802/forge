"""Data model of the entry ledger: one customs entry line and a load result."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

REQUIRED_COLUMNS = ("entry_number", "entry_date", "hts", "duty_usd")


@dataclass(frozen=True)
class Entry:
    entry_number: str
    entry_date: date
    hts: str
    duty: Decimal
    importer: Optional[str] = None


@dataclass(frozen=True)
class LoadResult:
    entries: tuple
    bad_rows: tuple
    warnings: tuple


class MissingColumnError(ValueError):
    """The CSV header lacks one or more required columns."""

    def __init__(self, columns):
        self.columns = tuple(columns)
        super().__init__("missing required column(s): " + ", ".join(self.columns))
