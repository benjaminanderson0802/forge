# Phase 1 gate: `entry_ledger` implementation plan

**Task:** P1G. **Spec:** `docs/specs/gate-project-entry-ledger.md` (approved under D-037, 2026-09-30). This plan implements exactly that spec and nothing more.

**Goal:** Forge builds a small, real, pure-Python library and CLI with no human help. It reads a CSV export of import entries, totals the duties paid (grouped and filtered), and flags entries against a user-supplied refund window. It gives no legal advice and decides no eligibility.

**Architecture:** a package `projects/entry_ledger/` with `__init__.py`, `model.py` (data types and errors), `load.py` (CSV reader), `report.py` (select, summarize, flag_window, cents helper), `cli.py` (`main(argv) -> int`) and `__main__.py` (so `python -m projects.entry_ledger` works). `projects/` itself has no `__init__.py`; it is an implicit namespace package, so every file stays under `projects/entry_ledger/`. Standard library only. Money is `decimal.Decimal`, never float.

**Tests:** one test file per task under `tests/core/`, named `test_entry_ledger_*.py`, written first (by the test writer) and read-only for the builder. Tests write real files into a `tempfile.TemporaryDirectory` as raw bytes so CRLF and UTF-8 BOM cases are exact. Tests must assert exact values (exact Decimals, exact dates, exact reasons, exact exit codes, exact boundary results) so the mutation gate passes.

**Dependency order:** T1Ga → T1Gb → T1Gc. A fourth hardening task is not needed: the edge cases the spec names (CRLF, BOM, `$1,234.50`, duplicates, bad rows, boundary dates, JSON round trip, exit codes) are each assigned to the task that owns the code.

---

## T1Ga: model and CSV loader

- **Files in scope:** `projects/entry_ledger/__init__.py`, `projects/entry_ledger/model.py`, `projects/entry_ledger/load.py`
- **Test file:** `tests/core/test_entry_ledger_load.py`
- **Test command:** `python -m unittest tests/core/test_entry_ledger_load.py`

Build the data model and the CSV loader of the `entry_ledger` package (spec `docs/specs/gate-project-entry-ledger.md`). Standard library only. No other task has run yet; create the package directory. Do not create `projects/__init__.py` (namespace package).

**`projects/entry_ledger/model.py`:**
- `Entry`: `@dataclass(frozen=True)` with fields in this order: `entry_number: str`, `entry_date: datetime.date`, `hts: str`, `duty: decimal.Decimal`, `importer: Optional[str] = None`. Assigning to a field raises `dataclasses.FrozenInstanceError`.
- `LoadResult`: `@dataclass(frozen=True)` with `entries: tuple` (of `Entry`, file order), `bad_rows: tuple` (of `(line_number: int, reason: str)` tuples, file order), `warnings: tuple` (of `str`, file order).
- `MissingColumnError(ValueError)`: has attribute `columns` (tuple of the missing required column names, in the order `entry_number, entry_date, hts, duty_usd`), and its `str()` names every missing column, e.g. `missing required column(s): duty_usd`.
- `REQUIRED_COLUMNS = ("entry_number", "entry_date", "hts", "duty_usd")`.

**`projects/entry_ledger/load.py`:** `load(path) -> LoadResult`. `path` is a `str` or `pathlib.Path`.
- Open with `open(path, newline="", encoding="utf-8-sig")` and read with `csv.reader`, so CRLF and LF files and files with or without a UTF-8 BOM load identically. `OSError` (missing file, directory) and `UnicodeDecodeError` propagate unchanged (the CLI maps them to exit 1).
- The first row is the header. Header names are stripped of whitespace and matched case-insensitively, in any order (`Entry_Number`, ` DUTY_USD ` are fine). An empty file, or a header lacking any required column, raises `MissingColumnError` listing all missing ones. `importer` is optional; unknown extra columns are ignored.
- Line numbers: the header is line 1; each record's `line_number` is `reader.line_num` after reading it (the physical file line). Rows that are completely empty (no fields, or all fields blank) are skipped silently: not bad rows, not entries.
- Every cell value is stripped of surrounding whitespace. A short row is padded with empty values.
- Validation, in this order, the first failure making the row a bad row `(line_number, reason)` and skipping it (never raising):
  1. empty `entry_number` → reason exactly `empty entry_number`;
  2. `entry_date` must match `^\d{4}-\d{2}-\d{2}$` and be a real date (`date.fromisoformat`), else reason `bad entry_date: '<raw value>'` (e.g. `bad entry_date: '2024-02-30'`, `bad entry_date: '01/02/2024'`);
  3. `duty_usd`: remove every `$`, `,` and space, then `Decimal(...)`; `InvalidOperation`, an empty value, or a non-finite result (`NaN`, `Infinity`) → reason `bad duty_usd: '<raw value>'`. `"$1,234.50"` → `Decimal("1234.50")`; `"12"` → `Decimal("12")`; negative values such as `"-5.00"` are accepted.
- `hts` is kept as text exactly as given (after stripping); it is never converted to a number, so `8471.30.0100` keeps its trailing zeros. `importer` is `None` when the column is absent or the cell is empty.
- Duplicates: a valid row whose `(entry_number, hts)` pair was already accepted is skipped and adds the warning `line <n>: duplicate entry <entry_number> hts <hts> skipped (first on line <m>)`. The first occurrence is kept. Bad rows never count as a first occurrence.
- Money is never converted to float anywhere.

**`projects/entry_ledger/__init__.py`:** re-exports `Entry`, `LoadResult`, `MissingColumnError`, `REQUIRED_COLUMNS` and `load`.

**Acceptance (tests must check, using real temp files written as bytes):**
- a clean LF file loads into exact `Entry` values (date objects, exact Decimals, `importer` None when absent and set when present);
- the same content as CRLF with a UTF-8 BOM yields an equal `LoadResult`;
- mixed-case, reordered, space-padded headers work;
- `"$1,234.50"` parses to `Decimal("1234.50")` and `isinstance(duty, Decimal)`;
- each bad-row kind gives the exact `(line_number, reason)` and the good rows around it still load; blank lines are skipped and do not shift reported line numbers off the physical lines;
- a duplicate pair keeps the first row's values and adds exactly one warning naming both line numbers; the same entry_number with a different hts is not a duplicate;
- a missing `duty_usd` (and a missing pair of columns) raises `MissingColumnError` whose `columns` and message name them; an empty file raises it too;
- a nonexistent path raises `FileNotFoundError`;
- `Entry` is frozen.

---

## T1Gb: select, summarize and flag_window

- **Files in scope:** `projects/entry_ledger/report.py`, `projects/entry_ledger/__init__.py`
- **Test file:** `tests/core/test_entry_ledger_report.py`
- **Test command:** `python -m unittest tests/core/test_entry_ledger_report.py`

Build the pure reporting functions of `entry_ledger` in `projects/entry_ledger/report.py`. Depends on T1Ga, already merged: `projects/entry_ledger/model.py` provides `Entry` (frozen dataclass: `entry_number: str`, `entry_date: date`, `hts: str`, `duty: Decimal`, `importer: Optional[str] = None`). Do not modify `model.py` or `load.py`. No file I/O in this module; tests build `Entry` objects directly. Standard library only; Decimal only, never float.

**`select(entries, hts_prefixes=(), start=None, end=None) -> list[Entry]`**
- Returns the entries, in input order, whose `hts` starts with any of `hts_prefixes` (a tuple/list of strings; if empty, every entry matches the HTS test) and with `start <= entry_date` when `start` is not None and `entry_date <= end` when `end` is not None. Both bounds are inclusive. Does not mutate its input. Accepts any iterable of entries.

**`Summary`**: `@dataclass(frozen=True)` with `count: int`, `total: Decimal`, `by_hts: dict` (str → Decimal), `by_month: dict` (str → Decimal), `earliest: Optional[date]`, `latest: Optional[date]`.

**`summarize(entries) -> Summary`**
- `count` = number of entries; `total` = exact Decimal sum of `duty` (unrounded; `Decimal("0")` when empty).
- `by_hts`: totals grouped by `hts[:8]` (the first 8 characters; shorter codes use the whole code), e.g. `9903.01.25` and `9903.01.33` both go to `9903.01.`, and `8471.30.0100` goes to `8471.30.`.
- `by_month`: totals grouped by `entry_date.strftime("%Y-%m")`.
- Both dicts have keys inserted in sorted ascending order.
- `earliest`/`latest` = min/max `entry_date`, or `None` when there are no entries.

**`WindowFlag`**: `@dataclass(frozen=True)` with `entry: Entry`, `status: str` (`"open"` or `"closed"`), `days_left: int`.

**`flag_window(entries, window_days, as_of) -> list[WindowFlag]`**
- One flag per entry, in input order. `age = (as_of - entry.entry_date).days`. `status = "open"` if `age <= window_days`, else `"closed"`. `days_left = max(0, window_days - age)`: never negative. An entry dated after `as_of` is `open` with `days_left = window_days - age` (more than `window_days`).
- `window_days` must be an `int >= 0`; a negative value raises `ValueError`.

**`to_cents(amount: Decimal) -> Decimal`**: `amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`; used only for display (`Decimal("2.345")` → `Decimal("2.35")`, `Decimal("-2.345")` → `Decimal("-2.35")`, `Decimal("1")` → `Decimal("1.00")`).

**`__init__.py`:** keep the T1Ga exports and add `select`, `summarize`, `Summary`, `flag_window`, `WindowFlag`, `to_cents`.

**Acceptance (tests must check exact values):**
- `select` with no filters returns all entries in order; a single prefix; several prefixes (union, order kept); prefix that matches nothing gives `[]`; `start` equal to an entry's date includes it and the day after excludes it; `end` equal to an entry's date includes it and the day before excludes it; both bounds together; the input list is unchanged.
- `summarize` on known entries gives the exact count, exact Decimal total (e.g. `Decimal("1234.50") + Decimal("0.25") == Decimal("1234.75")`), exact `by_hts` and `by_month` dicts including sorted key order, and exact earliest/latest; on `[]` gives `count 0`, `total Decimal("0")`, `{}`, `{}`, `None`, `None`; totals are `Decimal`, never float.
- `flag_window` boundaries with `window_days=180`: age 179 → open, days_left 1; age 180 → open, days_left 0; age 181 → closed, days_left 0; age 400 → closed, days_left 0 (never negative); a future-dated entry → open with days_left greater than 180; `window_days=0` with age 0 → open, 0; negative `window_days` raises `ValueError`.
- `to_cents` rounds half up as above.

---

## T1Gc: CLI and JSON output

- **Files in scope:** `projects/entry_ledger/cli.py`, `projects/entry_ledger/__main__.py`
- **Test file:** `tests/core/test_entry_ledger_cli.py`
- **Test command:** `python -m unittest tests/core/test_entry_ledger_cli.py`

Build the command-line tool of `entry_ledger`. Depends on T1Ga and T1Gb, already merged; do not modify their files. Available: `projects.entry_ledger.load.load(path) -> LoadResult(entries, bad_rows, warnings)` (raises `projects.entry_ledger.model.MissingColumnError` (a `ValueError`) for a missing required column, and lets `OSError` / `UnicodeDecodeError` propagate for an unreadable file); `projects.entry_ledger.report` provides `select(entries, hts_prefixes=(), start=None, end=None)`, `summarize(entries) -> Summary(count, total, by_hts, by_month, earliest, latest)`, `flag_window(entries, window_days, as_of) -> list[WindowFlag(entry, status, days_left)]` and `to_cents(Decimal) -> Decimal` (ROUND_HALF_UP to cents). Standard library only.

**Command:** `python -m projects.entry_ledger report <csv> [--hts PREFIX ...] [--from DATE] [--to DATE] [--window-days N --as-of DATE] [--json]`

**`projects/entry_ledger/cli.py`: `main(argv=None) -> int`** (argv defaults to `sys.argv[1:]`). It returns the exit code and never lets `SystemExit` escape: argparse errors and `--help` are caught and their code returned (errors → 2, help → 0). Writes via `sys.stdout` / `sys.stderr` looked up at call time (so `contextlib.redirect_stdout/redirect_stderr` capture it).
- Subcommand `report` (required; missing or unknown subcommand → 2). `--hts` uses `action="extend", nargs="+"`, so `--hts 9903 8471` and `--hts 9903 --hts 8471` are equivalent. `--from`/`--to`/`--as-of` are `YYYY-MM-DD`; an invalid date → 2. `--window-days` is an int `>= 0`; negative or non-integer → 2. `--window-days` and `--as-of` must be given together; only one → 2.
- Flow: `load(csv)`; `select(entries, hts, from, to)`; `summarize(selected)`; `flag_window(selected, N, as_of)` when the window options are given.
- Exit codes: 0 success (also when there were bad rows or warnings, and when the selection is empty); 2 bad arguments or `MissingColumnError` (its message on stderr, naming the column); 1 unreadable file (`OSError` including missing file or a directory, or `UnicodeDecodeError`), with a one-line `error: ...` message on stderr. No traceback is ever printed.
- stderr always gets one line per bad row, `line <n>: <reason>`, and one line per warning, `warning: <text>`, in both output modes.
- **Text output (default)** on stdout, amounts shown as `$` + `f"{to_cents(x):,.2f}"`:
  ```
  Entries: <count>
  Total duty: $<total>
  Earliest: <YYYY-MM-DD or ->
  Latest: <YYYY-MM-DD or ->
  By HTS prefix:
    <prefix>  $<amount>
  By month:
    <YYYY-MM>  $<amount>
  ```
  and, with the window options, `Window: <N> days as of <DATE>: <k> open, <m> closed` followed by one line per entry `  <entry_number>  <hts>  <entry_date>  <open|closed>  <days_left> days left`. So `$1,234.50` in the CSV shows as `Total duty: $1,234.50`.
- **`--json`**: stdout holds exactly one JSON document (nothing else) with keys `count` (int), `total_duty`, `by_hts` ({prefix: amount}), `by_month` ({month: amount}), `earliest`, `latest` (ISO date string or null), `bad_rows` (list of `{"line": int, "reason": str}`), `warnings` (list of str), `window` (null, or `{"window_days": int, "as_of": "YYYY-MM-DD", "entries": [{"entry_number", "hts", "entry_date", "duty", "status", "days_left"}]}`). Every Decimal is emitted as a string of `to_cents(value)` (e.g. `"1234.50"`), never a JSON number.

**`projects/entry_ledger/__main__.py`:** `from .cli import main` and `raise SystemExit(main())` under `if __name__ == "__main__":`.

**Acceptance (tests call `main([...])` with real temp CSV files, capture stdout/stderr, check exact codes and content):**
- text report on a CRLF + BOM file with `$1,234.50` amounts: exit 0, exact `Entries:` and `Total duty:` lines and the prefix/month lines;
- `--json` round trip: `json.loads(stdout)` works; `Decimal(data["total_duty"])` equals the expected total; amounts are `str`; `bad_rows` and `warnings` match the file's bad rows and duplicate; `window` is null without window options;
- `--hts` (both spellings), `--from` and `--to` with boundary dates are applied (count and totals change accordingly);
- `--window-days 180 --as-of DATE --json` gives the exact status and days_left per entry, including the 180-day boundary entry as `open` with `0`;
- bad rows and duplicate warnings appear on stderr as `line <n>: <reason>` / `warning: ...` and the exit code is still 0;
- exit 2 for: missing `duty_usd` column (stderr names it), invalid `--from` date, `--window-days` without `--as-of`, negative `--window-days`, missing subcommand; exit 1 for a nonexistent file and for a directory path; `main(["--help"])` returns 0; no case raises `SystemExit` out of `main`;
- `python -m projects.entry_ledger report <csv>` run via `subprocess` from the repo root exits 0.

## Reviewer notes

- T1Gb: Include fractional-cent duties in summarize tests to verify totals remain unrounded until display.
- T1Gc: Include invalid UTF-8 input, empty selections, text-mode window output, and fractional-cent JSON amounts to verify the specified error handling and display rounding.
