# Phase 1 gate project: `entry_ledger`

**Purpose.** This is the Layer 1 gate (layer-1-design.md §7): Forge builds a small, real project from this approved spec with no human help. It is useful groundwork for venture 1 (tariff refunds). Approved under D-037, 2026-09-30.

**What it is.** A pure-Python library and command-line tool. It reads a CSV export of import entries and reports the duties paid, grouped and filtered, so a person can see quickly which entries may fall in a refund window. It gives no legal advice and does not decide eligibility. It only totals and flags, using rules the user passes in.

## Location

- **Code:** `projects/entry_ledger/` (a package: `__init__.py`, `model.py`, `load.py`, `report.py`, `cli.py`).
- **Tests:** `tests/core/test_entry_ledger_*.py`.
- **Dependencies:** the standard library only.

## Input CSV

- **Header row required.** Columns are matched case-insensitively and in any order: `entry_number`, `entry_date` (YYYY-MM-DD), `hts` (the HTS or chapter-99 code as text, for example `9903.01.25` or `8471.30.0100`), `duty_usd` (a decimal, which may contain `$` and `,`), and `importer` (optional).
- **Missing required column:** a clear error naming it, with exit code 2 from the CLI.
- **Bad rows:** an unparseable date or amount, or an empty `entry_number`, is collected as `(line_number, reason)` and skipped. It never crashes the run.
- **Duplicates:** a repeated (entry_number, hts) pair is kept once, the first occurrence, and reported as a warning.
- **Money:** amounts use `decimal.Decimal`, never float. Totals are rounded to cents with ROUND_HALF_UP only for display.

## Library API

- `load(path) -> LoadResult(entries, bad_rows, warnings)`.
- `Entry` is a frozen dataclass: `entry_number`, `entry_date` (a `date`), `hts`, `duty` (a `Decimal`) and `importer` (a `str`, or None).
- `select(entries, hts_prefixes=(), start=None, end=None)`: the entries whose `hts` starts with any of the given prefixes (all entries if none are given), with `start <= entry_date <= end` where each bound is given (both inclusive).
- `summarize(entries) -> Summary`: the count, the total duty, totals by HTS prefix group (the first 8 characters of `hts`), totals by month (`YYYY-MM`), and the earliest and latest date.
- `flag_window(entries, window_days, as_of)`: each entry is marked `open` if `as_of - entry_date <= window_days`, else `closed`, and gets its `days_left` (never negative).

## CLI

`python -m projects.entry_ledger report <csv> [--hts PREFIX ...] [--from DATE] [--to DATE] [--window-days N --as-of DATE] [--json]`

- **Output:** a plain-text summary table by default. `--json` gives machine-readable output with every Decimal as a string.
- **Exit codes:** 0 on success, 2 for bad arguments or a missing column, 1 for an unreadable file.
- **Bad rows:** reported on stderr.

## Acceptance

- **Tests:** real files in a temp dir, including CRLF and a UTF-8 BOM, `$1,234.50` amounts, duplicates, bad rows, boundary dates in `select` and `flag_window`, and a JSON round trip. The CLI is tested through `main(argv)`, which returns the exit code.
- **Standards:** it works on Windows and Linux (pathlib, `newline=""` for csv, `encoding="utf-8-sig"`), and the mutation gate passes.
