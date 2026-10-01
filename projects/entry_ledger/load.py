"""CSV loader for the entry ledger. Standard library only; money stays Decimal."""

import csv
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .model import REQUIRED_COLUMNS, Entry, LoadResult, MissingColumnError

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_MONEY_JUNK = str.maketrans("", "", "$, ")


def _parse_date(raw):
    if not _DATE_RE.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_duty(raw):
    cleaned = raw.translate(_MONEY_JUNK)
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def load(path):
    """Load an entry CSV into a LoadResult. Bad rows are reported, not raised."""
    entries = []
    bad_rows = []
    warnings = []
    first_seen = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        index = {}
        for position, name in enumerate(header):
            index.setdefault(name.strip().lower(), position)
        missing = [name for name in REQUIRED_COLUMNS if name not in index]
        if missing:
            raise MissingColumnError(missing)
        importer_at = index.get("importer")
        width = max(index.values()) + 1

        for row in reader:
            line = reader.line_num
            cells = [cell.strip() for cell in row]
            if not any(cells):
                continue
            if len(cells) < width:
                cells.extend([""] * (width - len(cells)))

            number = cells[index["entry_number"]]
            raw_date = cells[index["entry_date"]]
            hts = cells[index["hts"]]
            raw_duty = cells[index["duty_usd"]]

            if not number:
                bad_rows.append((line, "empty entry_number"))
                continue
            entry_date = _parse_date(raw_date)
            if entry_date is None:
                bad_rows.append((line, f"bad entry_date: '{raw_date}'"))
                continue
            duty = _parse_duty(raw_duty)
            if duty is None:
                bad_rows.append((line, f"bad duty_usd: '{raw_duty}'"))
                continue

            key = (number, hts)
            if key in first_seen:
                warnings.append(
                    f"line {line}: duplicate entry {number} hts {hts} skipped "
                    f"(first on line {first_seen[key]})"
                )
                continue
            first_seen[key] = line
            importer = cells[importer_at] if importer_at is not None else ""
            entries.append(Entry(number, entry_date, hts, duty, importer or None))

    return LoadResult(tuple(entries), tuple(bad_rows), tuple(warnings))
