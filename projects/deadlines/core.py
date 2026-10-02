"""Load deadline rows from CSV and classify open items by due date."""

import csv
import re
from datetime import date, timedelta

REQUIRED_COLUMNS = ("item", "venture", "due", "status")
STATUSES = ("open", "done")
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _parse_due(raw):
    """Return the date for an exact YYYY-MM-DD string, or None."""
    if not _DATE_SHAPE.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def load(path):
    """Read a deadline CSV and return (rows, errors).

    Raises ValueError naming every missing required column, and lets
    FileNotFoundError propagate for a missing file.
    """
    rows = []
    errors = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        positions = {}
        for index, name in enumerate(header):
            positions.setdefault(name.strip().lower(), index)
        missing = [name for name in REQUIRED_COLUMNS if name not in positions]
        if missing:
            raise ValueError("missing columns: " + ", ".join(missing))

        for record in reader:
            line = reader.line_num
            if not record:
                continue
            fields = {}
            for name in REQUIRED_COLUMNS:
                index = positions[name]
                fields[name] = record[index].strip() if index < len(record) else ""

            due = _parse_due(fields["due"])
            if due is None:
                errors.append({"line": line, "reason": f"bad date: '{fields['due']}'"})
                continue
            status = fields["status"].lower()
            if status not in STATUSES:
                errors.append({"line": line,
                               "reason": f"unknown status: '{fields['status']}'"})
                continue
            rows.append({"item": fields["item"], "venture": fields["venture"],
                         "due": due, "status": status})
    return rows, errors


def classify(rows, today, days=14):
    """Split open rows into overdue and due-soon lists plus a count of later ones."""
    if days < 0:
        raise ValueError(f"days must not be negative, got {days}")
    horizon = today + timedelta(days=days)
    overdue = []
    due_soon = []
    later = 0
    for row in rows:
        if row["status"] != "open":
            continue
        due = row["due"]
        if due > horizon:
            later += 1
        elif due < today:
            overdue.append(row)
        else:
            due_soon.append(row)

    def entries(group):
        ordered = sorted(group, key=lambda r: (r["due"], r["venture"], r["item"]))
        return [{"item": r["item"], "venture": r["venture"],
                 "due": r["due"].isoformat(), "days": (r["due"] - today).days}
                for r in ordered]

    return {"overdue": entries(overdue), "due_soon": entries(due_soon), "later": later}
