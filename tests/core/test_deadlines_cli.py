"""Behavioural contract for the deadline command line and module entry point."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from importlib import import_module
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
HEADER = "item,venture,due,status\n"
ROWS = (
    "Boundary,Beta,2026-10-16,open\n"
    "Zulu,Alpha,2026-10-01,open\n"
    "Today,Beta,2026-10-02,open\n"
    "Later,Alpha,2026-10-17,open\n"
    "Review,Beta,2026-10-05,open\n"
    "Oldest,Zeta,2026-09-29,open\n"
    "Alpha,Alpha,2026-10-01,open\n"
    "First,Beta,2026-10-01,open\n"
    "Zulu,Alpha,2026-10-05,open\n"
    "Alpha,Alpha,2026-10-05,open\n"
    "Tomorrow,Alpha,2026-10-03,open\n"
    "Outside short window,Alpha,2026-10-06,open\n"
    "Done overdue,Alpha,2026-09-01,done\n"
    "Done today,Alpha,2026-10-02,done\n"
    "Done later,Alpha,2026-11-01,done\n"
)
OVERDUE_TEXT = (
    "Overdue (4)\n"
    "  2026-09-29  Zeta: Oldest  (3 days late)\n"
    "  2026-10-01  Alpha: Alpha  (1 days late)\n"
    "  2026-10-01  Alpha: Zulu  (1 days late)\n"
    "  2026-10-01  Beta: First  (1 days late)\n"
)
SHORT_ENTRIES = (
    "  2026-10-02  Beta: Today  (today)\n"
    "  2026-10-03  Alpha: Tomorrow  (in 1 days)\n"
    "  2026-10-05  Alpha: Alpha  (in 3 days)\n"
    "  2026-10-05  Alpha: Zulu  (in 3 days)\n"
    "  2026-10-05  Beta: Review  (in 3 days)\n"
)
EXPECTED_TEXT = (
    OVERDUE_TEXT + "Due in the next 14 days (7)\n" + SHORT_ENTRIES
    + "  2026-10-06  Alpha: Outside short window  (in 4 days)\n"
    "  2026-10-16  Beta: Boundary  (in 14 days)\n"
    "Later: 1\n"
)


def entry(item, venture, due, days):
    return {"item": item, "venture": venture, "due": due, "days": days}


class DeadlineCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def csv(self, rows=ROWS, header=HEADER):
        path = self.root / "deadline input.csv"
        path.write_text(header + rows, encoding="utf-8")
        return path

    def invoke(self, argv):
        # Import before redirecting: main must look up the streams at call time.
        main = import_module("projects.deadlines.cli").main
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(argv)
            except SystemExit as exc:
                self.fail(f"main must return, not raise SystemExit({exc.code})")
        self.assertIs(type(code), int)
        self.assertNotIn("Traceback", stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self, path, *options):
        return self.invoke([str(path), "--today", "2026-10-02", *options])

    def assert_error(self, result):
        code, out, err = result
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())
        self.assertEqual(len(err.splitlines()), 1, repr(err))
        self.assertNotIn("Traceback", err)
        return err

    def assert_json(self, out, expected):
        data = json.loads(out)
        self.assertEqual(list(data), ["today", "days", "overdue", "due_soon", "later", "errors"])
        self.assertEqual(data, expected)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.endswith("\n"))
        self.assertIs(type(data["days"]), int)
        self.assertIs(type(data["later"]), int)
        for row in data["overdue"] + data["due_soon"]:
            self.assertIs(type(row["days"]), int)
        for error in data["errors"]:
            self.assertIs(type(error["line"]), int)

    def test_exact_text_report_sorted_and_done_rows_excluded(self):
        self.assertEqual(self.report(self.csv()), (0, EXPECTED_TEXT, ""))

    def test_three_day_window_includes_boundary(self):
        expected = OVERDUE_TEXT + "Due in the next 3 days (5)\n" + SHORT_ENTRIES + "Later: 3\n"
        self.assertEqual(self.report(self.csv(), "--days", "3"), (0, expected, ""))

    def test_zero_day_window_includes_only_today(self):
        expected = (OVERDUE_TEXT + "Due in the next 0 days (1)\n"
                    "  2026-10-02  Beta: Today  (today)\nLater: 7\n")
        self.assertEqual(self.report(self.csv(), "--days", "0"), (0, expected, ""))

    def test_empty_sections_are_always_printed(self):
        self.assertEqual(self.report(self.csv("")),
                         (0, "Overdue (0)\nDue in the next 14 days (0)\nLater: 0\n", ""))

    def test_bad_rows_reported_in_file_order_in_text_and_json(self):
        path = self.csv(
            "Bad date,Alpha,tomorrow,open\n"
            "Today,Beta,2026-10-02,open\n"
            "Bad status,Alpha,2026-10-03,waiting\n"
            "Impossible,Alpha,2026-02-30,open\n"
        )
        errors = [
            {"line": 2, "reason": "bad date: 'tomorrow'"},
            {"line": 4, "reason": "unknown status: 'waiting'"},
            {"line": 5, "reason": "bad date: '2026-02-30'"},
        ]
        expected = (
            "Overdue (0)\nDue in the next 14 days (1)\n"
            "  2026-10-02  Beta: Today  (today)\nLater: 0\nSkipped rows: 3\n"
            "  line 2: bad date: 'tomorrow'\n"
            "  line 4: unknown status: 'waiting'\n"
            "  line 5: bad date: '2026-02-30'\n"
        )
        self.assertEqual(self.report(path), (1, expected, ""))
        code, out, err = self.report(path, "--json")
        self.assertEqual((code, err), (1, ""))
        self.assert_json(out, {
            "today": "2026-10-02", "days": 14, "overdue": [],
            "due_soon": [entry("Today", "Beta", "2026-10-02", 0)],
            "later": 0, "errors": errors,
        })

    def test_json_report_values_order_and_custom_window(self):
        path = self.csv()
        overdue = [entry("Oldest", "Zeta", "2026-09-29", -3),
                   entry("Alpha", "Alpha", "2026-10-01", -1),
                   entry("Zulu", "Alpha", "2026-10-01", -1),
                   entry("First", "Beta", "2026-10-01", -1)]
        soon = [entry("Today", "Beta", "2026-10-02", 0),
                entry("Tomorrow", "Alpha", "2026-10-03", 1),
                entry("Alpha", "Alpha", "2026-10-05", 3),
                entry("Zulu", "Alpha", "2026-10-05", 3),
                entry("Review", "Beta", "2026-10-05", 3)]
        for days, options, due_soon, later in [
            (14, [], soon + [entry("Outside short window", "Alpha", "2026-10-06", 4),
                            entry("Boundary", "Beta", "2026-10-16", 14)], 1),
            (3, ["--days", "3"], soon, 3),
        ]:
            with self.subTest(days=days):
                code, out, err = self.report(path, *options, "--json")
                self.assertEqual((code, err), (0, ""))
                self.assert_json(out, {"today": "2026-10-02", "days": days,
                                       "overdue": overdue, "due_soon": due_soon,
                                       "later": later, "errors": []})

    def test_argument_errors_return_two_and_exactly_one_stderr_line(self):
        base = [str(self.csv())]
        cases = [[], base + ["--unknown"], base + ["--today"], base + ["--days"]]
        for value in ("2026-13-01", "tomorrow", "2026-02-29", "2026-2-01", "20261002"):
            cases.append(base + ["--today", value])
        for value in ("-1", "x", "1.5"):
            cases.append(base + ["--days", value])
        for argv in cases:
            with self.subTest(argv=argv):
                self.assert_error(self.invoke(argv))

    def test_missing_unreadable_and_invalid_encoding_files_name_path(self):
        invalid = self.root / "invalid.csv"
        invalid.write_bytes(HEADER.encode("ascii") + b"Broken,Alpha,2026-10-02,open\xff\n")
        # Opening a directory fails with OSError on both Windows and POSIX.
        for path in (self.root / "missing.csv", self.root, invalid):
            for options in ([], ["--json"]):
                with self.subTest(path=path, options=options):
                    err = self.assert_error(self.report(path, *options))
                    self.assertIn(str(path), err)

    def test_missing_columns_preserve_loader_error_text(self):
        from projects.deadlines.core import load
        path = self.csv("Launch,Alpha\n", header="item,venture\n")
        with self.assertRaises(ValueError) as caught:
            load(path)
        message = str(caught.exception)
        self.assertIn("due", message)
        self.assertIn("status", message)
        for options in ([], ["--json"]):
            with self.subTest(options=options):
                self.assertIn(message, self.assert_error(self.report(path, *options)))

    def test_today_defaults_to_local_date(self):
        path = self.csv("")
        before = date.today().isoformat()
        code, out, err = self.invoke([str(path), "--json"])
        after = date.today().isoformat()
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertIn(data["today"], (before, after))
        self.assert_json(out, {"today": data["today"], "days": 14,
                               "overdue": [], "due_soon": [], "later": 0, "errors": []})

    def test_main_defaults_to_sys_argv(self):
        path = self.csv()
        with patch.object(sys, "argv", ["deadlines", str(path), "--today", "2026-10-02"]):
            self.assertEqual(self.invoke(None), (0, EXPECTED_TEXT, ""))

    def test_module_entry_point_matches_main(self):
        path = self.csv()
        result = subprocess.run(
            [sys.executable, "-m", "projects.deadlines", str(path), "--today", "2026-10-02"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, EXPECTED_TEXT, ""))
        self.assertEqual(self.report(path), (result.returncode, result.stdout, result.stderr))


if __name__ == "__main__":
    unittest.main()
