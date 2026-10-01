"""Command-line tool of the entry ledger: report <csv> as text or JSON."""

import argparse
import json
import re
import sys
from datetime import date

from .load import load
from .model import MissingColumnError
from .report import flag_window, select, summarize, to_cents

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# Every character str.splitlines() breaks on, shown escaped so a diagnostic stays one line.
_LINE_BREAKS = {
    ord(c): c.encode("unicode_escape").decode("ascii")
    for c in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
}


def _iso_date(raw):
    if _DATE_RE.fullmatch(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"invalid date {raw!r}, expected YYYY-MM-DD")


def _window_days(raw):
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid day count {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"window days must be >= 0, got {value}")
    return value


def _parser():
    parser = argparse.ArgumentParser(
        prog="entry_ledger", description="Summarize customs entry lines from a CSV file."
    )
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)
    report = commands.add_parser("report", help="summarize duty, optionally with a refund window")
    report.add_argument("csv", help="entry CSV file")
    report.add_argument("--hts", action="extend", nargs="+", default=[], metavar="PREFIX",
                        help="keep entries whose HTS starts with any PREFIX")
    report.add_argument("--from", dest="start", type=_iso_date, metavar="DATE",
                        help="first entry date to keep (YYYY-MM-DD)")
    report.add_argument("--to", dest="end", type=_iso_date, metavar="DATE",
                        help="last entry date to keep (YYYY-MM-DD)")
    report.add_argument("--window-days", type=_window_days, metavar="N",
                        help="flag entries within N days of --as-of")
    report.add_argument("--as-of", type=_iso_date, metavar="DATE",
                        help="reference date for --window-days (YYYY-MM-DD)")
    report.add_argument("--json", action="store_true", help="print one JSON document")
    return parser, report


def _err(text):
    print(text.translate(_LINE_BREAKS), file=sys.stderr)


def _money(amount):
    return f"${to_cents(amount):,.2f}"


def _cents(amount):
    return str(to_cents(amount))


def _iso(day):
    return day.isoformat() if day else None


def _text(summary, flags, args):
    lines = [
        f"Entries: {summary.count}",
        f"Total duty: {_money(summary.total)}",
        f"Earliest: {_iso(summary.earliest) or '-'}",
        f"Latest: {_iso(summary.latest) or '-'}",
        "By HTS prefix:",
        *(f"  {prefix}  {_money(amount)}" for prefix, amount in summary.by_hts.items()),
        "By month:",
        *(f"  {month}  {_money(amount)}" for month, amount in summary.by_month.items()),
    ]
    if flags is not None:
        opened = sum(flag.status == "open" for flag in flags)
        lines.append(f"Window: {args.window_days} days as of {args.as_of.isoformat()}: "
                     f"{opened} open, {len(flags) - opened} closed")
        lines.extend(
            f"  {f.entry.entry_number}  {f.entry.hts}  {f.entry.entry_date.isoformat()}  "
            f"{f.status}  {f.days_left} days left"
            for f in flags
        )
    return "\n".join(lines)


def _json(summary, flags, args, result):
    window = None
    if flags is not None:
        window = {
            "window_days": args.window_days,
            "as_of": args.as_of.isoformat(),
            "entries": [
                {
                    "entry_number": f.entry.entry_number,
                    "hts": f.entry.hts,
                    "entry_date": f.entry.entry_date.isoformat(),
                    "duty": _cents(f.entry.duty),
                    "status": f.status,
                    "days_left": f.days_left,
                }
                for f in flags
            ],
        }
    return json.dumps({
        "count": summary.count,
        "total_duty": _cents(summary.total),
        "by_hts": {prefix: _cents(amount) for prefix, amount in summary.by_hts.items()},
        "by_month": {month: _cents(amount) for month, amount in summary.by_month.items()},
        "earliest": _iso(summary.earliest),
        "latest": _iso(summary.latest),
        "bad_rows": [{"line": line, "reason": reason} for line, reason in result.bad_rows],
        "warnings": list(result.warnings),
        "window": window,
    })


def main(argv=None):
    """Run the CLI and return its exit code; never raises SystemExit."""
    parser, report = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if (args.window_days is None) != (args.as_of is None):
            report.error("--window-days and --as-of must be given together")
    except SystemExit as exc:
        return exc.code

    try:
        result = load(args.csv)
    except MissingColumnError as exc:
        _err(f"error: {exc}")
        return 2
    except (OSError, UnicodeDecodeError) as exc:
        _err(f"error: cannot read {args.csv}: {exc}")
        return 1

    for line, reason in result.bad_rows:
        _err(f"line {line}: {reason}")
    for warning in result.warnings:
        _err(f"warning: {warning}")

    selected = select(result.entries, args.hts, args.start, args.end)
    summary = summarize(selected)
    flags = None
    if args.window_days is not None:
        flags = flag_window(selected, args.window_days, args.as_of)

    render = _json(summary, flags, args, result) if args.json else _text(summary, flags, args)
    print(render, file=sys.stdout)
    return 0
