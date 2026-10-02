"""Command line for the deadline tracker: text or JSON report with exit codes.

Usage: python -m projects.deadlines <csv> [--today YYYY-MM-DD] [--days N] [--json]

Exit codes: 0 report printed with no skipped rows, 1 report printed with some
rows skipped, 2 nothing printed because of a bad argument or unreadable file.
"""

import argparse
import json
import re
import sys
from datetime import date

from projects.deadlines.core import classify, load

PROG = "deadlines"
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class _UsageError(Exception):
    """An argument problem, reported as one stderr line with exit code 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse would print the multiline usage and exit; raise instead so
        # main can write a single line and return 2.
        raise _UsageError(message)


def _today_arg(value):
    if not _DATE_SHAPE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"--today must be YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--today is not a real date: {value!r}") from None


def _days_arg(value):
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--days must be an integer, got {value!r}") from None
    if days < 0:
        raise argparse.ArgumentTypeError(f"--days must not be negative, got {days}")
    return days


def _parser():
    parser = _Parser(prog=PROG, description="Report overdue and upcoming deadlines from a CSV.")
    parser.add_argument("csv", help="CSV file with item, venture, due and status columns")
    parser.add_argument("--today", type=_today_arg, default=None,
                        help="report date as YYYY-MM-DD (default: today's local date)")
    parser.add_argument("--days", type=_days_arg, default=14,
                        help="size of the due-soon window in days (default: 14)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def _fail(message):
    print(f"{PROG}: error: " + " ".join(str(message).split()), file=sys.stderr)
    return 2


def _text(result, errors, days):
    lines = [f"Overdue ({len(result['overdue'])})"]
    for e in result["overdue"]:
        lines.append(f"  {e['due']}  {e['venture']}: {e['item']}  ({-e['days']} days late)")
    lines.append(f"Due in the next {days} days ({len(result['due_soon'])})")
    for e in result["due_soon"]:
        when = "today" if e["days"] == 0 else f"in {e['days']} days"
        lines.append(f"  {e['due']}  {e['venture']}: {e['item']}  ({when})")
    lines.append(f"Later: {result['later']}")
    if errors:
        lines.append(f"Skipped rows: {len(errors)}")
        for error in errors:
            lines.append(f"  line {error['line']}: {error['reason']}")
    return "".join(line + "\n" for line in lines)


def main(argv=None):
    """Run the command line and return its exit code."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = _parser().parse_args(argv)
    except _UsageError as exc:
        return _fail(exc)
    today = args.today if args.today is not None else date.today()

    try:
        rows, errors = load(args.csv)
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        return _fail(f"cannot read {args.csv}: {reason}")
    except ValueError as exc:
        return _fail(f"{args.csv}: {exc}")

    result = classify(rows, today, args.days)
    if args.json:
        report = {"today": today.isoformat(), "days": args.days,
                  "overdue": result["overdue"], "due_soon": result["due_soon"],
                  "later": result["later"], "errors": errors}
        sys.stdout.write(json.dumps(report) + "\n")
    else:
        sys.stdout.write(_text(result, errors, args.days))
    return 1 if errors else 0
