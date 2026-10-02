# Demo: venture deadline tracker (DEMO1)

Request from Ben, 2026-10-02: a deadline tracker for his venture timelines.

**Goal:** a small, self-contained, standard-library-only Python package `projects/deadlines/` that reads a CSV of venture deadlines, sorts open items into overdue / due soon / later, and reports them on the command line as text or JSON.

**Architecture:** two layers. `core.py` holds the pure logic (`load`, `classify`), re-exported from `__init__.py`. `cli.py` holds `main(argv)`, which parses arguments, calls the core and prints; `__main__.py` only runs `main`. `projects/` has no `__init__.py` (namespace package, like `projects/entry_ledger`), so `python -m projects.deadlines` works from the repo root.

**Tests:** `tests/core/test_deadlines_core.py` (task D1) and `tests/core/test_deadlines_cli.py` (task D2). Tests build their CSV files in a temporary directory; nothing is read from or written into the repo.

---

## Task D1: load and classify

**Files in scope:** `projects/deadlines/__init__.py`, `projects/deadlines/core.py`
**Tests:** `tests/core/test_deadlines_core.py`
**Run:** `python -m unittest tests/core/test_deadlines_core.py`
**Depends on:** nothing.

Build the package `projects/deadlines/` (standard library only, no third-party imports). `projects/deadlines/core.py` defines `load` and `classify`; `projects/deadlines/__init__.py` re-exports them so `from projects.deadlines import load, classify` works. Do not create `projects/__init__.py`.

### `load(path) -> (rows, errors)`

- `path` is a `str` or `os.PathLike`. Open with `encoding="utf-8-sig"` (plain UTF-8, a leading BOM is tolerated) and `newline=""`, and read with `csv.reader`.
- The first row is the header. Header names are matched after stripping surrounding spaces, case-insensitively. Required columns: `item`, `venture`, `due`, `status`. Extra columns are ignored; column order is free.
- If any required column is missing (including an empty file with no header), raise `ValueError` whose message names every missing column, in the order `item, venture, due, status`, e.g. `missing columns: due, status`.
- A file that does not exist raises the normal `FileNotFoundError` (do not catch it).
- Each data row's line number is the 1-based physical line number in the file, header = 1, taken from `reader.line_num` right after the row is read. Completely empty lines are skipped (not rows, not errors) but still count toward later line numbers.
- Every field value is stripped of surrounding spaces. A row shorter than the header gives `""` for its missing fields.
- `due` must match `YYYY-MM-DD` exactly (regex `^\d{4}-\d{2}-\d{2}$`) and be a real date (`datetime.date.fromisoformat`). Otherwise the row is skipped and `{"line": L, "reason": "bad date: '<raw value>'"}` is appended to `errors` (e.g. `2026-02-30`, `2026/10/02`, `20261002`, empty).
- `status`, stripped and lower-cased, must be `open` or `done`. Otherwise the row is skipped with `{"line": L, "reason": "unknown status: '<raw value>'"}`.
- A row with both a bad date and an unknown status gets one error only: the date error.
- Good rows become `{"item": str, "venture": str, "due": datetime.date, "status": "open" | "done"}` (exactly these four keys, values stripped, status lower-cased), in file order. Errors are in file order.
- Bad rows are never fatal; `load` returns `(rows, errors)` as two lists.

### `classify(rows, today, days=14) -> dict`

- `rows` is a list of row dicts as returned by `load`; `today` is a `datetime.date`; `days` is an `int`.
- If `days < 0`, raise `ValueError`.
- Only rows with `status == "open"` count; `done` rows are ignored entirely.
- `overdue`: open rows with `due < today`. `due_soon`: open rows with `today <= due <= today + timedelta(days=days)` (both ends inclusive; with `days=0` only items due today). `later`: the count (int) of open rows with `due > today + days`.
- `overdue` and `due_soon` are lists sorted by `(due, venture, item)`. Each entry is `{"item": str, "venture": str, "due": "YYYY-MM-DD", "days": (due - today).days}` (`days` negative for overdue, 0 for due today).
- Returns exactly `{"overdue": [...], "due_soon": [...], "later": int}`. The input list and its dicts are not modified.

### Acceptance criteria (tests must check)

1. A valid CSV with columns in a different order plus an extra column loads; rows have exactly the four keys, `due` is a `datetime.date`, values are stripped, ` Open `/`DONE` become `open`/`done`.
2. Header names with surrounding spaces or other case (` Due `, `STATUS`) are accepted.
3. Missing columns raise `ValueError` whose message contains each missing name (test one and two missing; an empty file names all four).
4. A bad date (`2026-02-30`, `2026/10/02`, empty) and an unknown status (`pending`) are skipped and reported with the correct 1-based line numbers (header = 1; a blank line before a bad row shifts its number) and reasons starting `bad date:` / `unknown status:`; good rows around them still load.
5. A UTF-8 file with a BOM and non-ASCII text (e.g. `Café launch`) loads correctly.
6. `classify` ignores `done` rows; splits overdue / due soon / later correctly at the boundaries (due = today − 1, today, today + days, today + days + 1); sorts by due, then venture, then item; formats `due` as `YYYY-MM-DD`; computes `days` (negative for overdue); `later` is an int count.
7. `classify(..., days=0)` puts only today's items in `due_soon`; `days=-1` raises `ValueError`; the default is 14.
8. `classify` does not mutate its input.

---

## Task D2: command line

**Files in scope:** `projects/deadlines/__main__.py`, `projects/deadlines/cli.py`
**Tests:** `tests/core/test_deadlines_cli.py`
**Run:** `python -m unittest tests/core/test_deadlines_cli.py`
**Depends on:** D1 (uses `load` and `classify` from `projects/deadlines/core.py`; do not change that file).

### Interface

- `projects/deadlines/cli.py` defines `main(argv=None) -> int` (argv defaults to `sys.argv[1:]`). It writes the report to `sys.stdout` and error messages to `sys.stderr` (looked up at call time, so tests can redirect them), and returns the exit code. It never lets a traceback escape for the error cases below.
- `projects/deadlines/__main__.py` runs `sys.exit(main())`, so `python -m projects.deadlines <csv> [--today YYYY-MM-DD] [--days N] [--json]` works from the repo root.
- Use `argparse`. `--today` defaults to `datetime.date.today()` (local date) and must match `YYYY-MM-DD` and be a real date. `--days` defaults to 14 and must be an integer >= 0. `--json` is a flag.

### Behaviour

`rows, errors = load(csv)`, then `result = classify(rows, today, days)`.

Text output (default), each line ending in `\n`, two-space indents and two spaces between fields exactly as shown:

```
Overdue (K)
  YYYY-MM-DD  <venture>: <item>  (N days late)
Due in the next N days (K)
  YYYY-MM-DD  <venture>: <item>  (in N days)
Later: K
Skipped rows: K
  line L: reason
```

- `Overdue (K)`: K = number of overdue entries, then one line per entry in `classify` order; `N days late` uses N = `-days` (always the word `days`).
- `Due in the next N days (K)`: N = the `--days` value, K = number of due-soon entries; each line ends `(in N days)` with N = the entry's `days`, or `(today)` when it is 0.
- Section headers are printed even when a section is empty (`Overdue (0)`).
- `Later: K` with K = `result["later"]`.
- Only when `errors` is non-empty: `Skipped rows: K`, then one `  line L: reason` per error in file order.

With `--json`: print exactly one JSON object (`json.dumps`, then a newline) with keys in this order: `{"today": "YYYY-MM-DD", "days": N, "overdue": [...], "due_soon": [...], "later": K, "errors": [...]}`, where `overdue`/`due_soon` are the `classify` entries, and `errors` the `load` errors (`{"line", "reason"}`); `errors` is `[]` when none. Nothing else goes to stdout.

### Exit codes

- `0`: report printed, no rows skipped.
- `1`: report printed, some rows skipped.
- `2`, with a one-line message on stderr, nothing on stdout, no traceback: the CSV file does not exist or cannot be read (`OSError`, `UnicodeDecodeError`) — message includes the path; required columns missing — message includes the `ValueError` text naming them; bad arguments — no CSV argument, unknown option, `--today` not a valid `YYYY-MM-DD` date, `--days` not an integer or negative. For argument errors `main` returns 2 (catch argparse's `SystemExit` so `main` returns rather than exits; `--help` may exit 0 as usual).

### Acceptance criteria (tests must check)

Tests write CSVs to a temporary directory, call `main([...])` with `sys.stdout`/`sys.stderr` redirected (`contextlib.redirect_stdout/redirect_stderr`), and run at least one real `subprocess.run([sys.executable, "-m", "projects.deadlines", ...], cwd=<repo root>)`.

1. With `--today 2026-10-02` and a CSV holding overdue, due-today, due-soon, later and done rows, the text output equals the expected lines exactly, including `(N days late)`, `(today)`, `(in N days)`, `Due in the next 14 days (K)`, `Later: K`, and the sort order; exit 0; `done` rows do not appear.
2. `--days 3` changes both the header (`Due in the next 3 days`) and which items are due soon vs later.
3. A CSV with bad rows: report still printed, followed by `Skipped rows: K` and `  line L: reason` lines; exit 1. Without bad rows there is no `Skipped rows` line.
4. `--json` prints one parseable JSON object with exactly the keys `today, days, overdue, due_soon, later, errors` and the right values (including `errors` with line numbers); exit 0 or 1 by the same rule.
5. Missing file, missing columns, `--today 2026-13-01`, `--today tomorrow`, `--days -1`, `--days x`, and no arguments each return 2 with a non-empty stderr message, empty stdout, and no `Traceback` in stderr.
6. Without `--today`, the report uses `datetime.date.today()` (check via `--json` `today` field).
7. The subprocess run of `python -m projects.deadlines <csv> --today 2026-10-02` succeeds with the same output as `main`.

## Reviewer notes

- D2: Catching argparse's SystemExit alone still emits its default multiline usage output. Customize argument-error handling to satisfy the planned one-line stderr message, and verify unknown-option handling.
