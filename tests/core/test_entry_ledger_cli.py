"""End-to-end CLI contract using real CSV files and captured output."""

from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


HEADER = "entry_number,entry_date,hts,duty_usd\n"
ROOT = Path(__file__).resolve().parents[2]


class EntryLedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv(self, rows, name="entries.csv", bom=False):
        text = HEADER + rows
        data = text.replace("\n", "\r\n").encode("utf-8") if bom else text.encode("utf-8")
        path = self.root / name
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
        return path

    def invoke(self, argv):
        # Import before redirecting: streams must be looked up at call time.
        from projects.entry_ledger.cli import main

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(argv)
            except SystemExit as exc:
                self.fail(f"main must return its exit code, not raise SystemExit({exc.code})")
        self.assertIs(type(code), int)
        self.assertNotIn("Traceback", stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self, path, *options):
        return self.invoke(["report", str(path), *options])

    def json_report(self, path, *options):
        code, out, err = self.report(path, *options, "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)  # Also rejects extra documents and text banners.
        self.assertEqual(set(data), {
            "count", "total_duty", "by_hts", "by_month", "earliest", "latest",
            "bad_rows", "warnings", "window",
        })
        self.assertIs(type(data["count"]), int)
        for amount in [data["total_duty"], *data["by_hts"].values(), *data["by_month"].values()]:
            self.assertIsInstance(amount, str)
        return data, err

    def test_bom_crlf_currency_text_report(self):
        path = self.csv('A,2024-02-29,9903.01.25,"$1,234.50"\n', bom=True)
        code, out, err = self.report(path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "Entries: 1", "Total duty: $1,234.50", "Earliest: 2024-02-29",
            "Latest: 2024-02-29", "By HTS prefix:", "  9903.01.  $1,234.50",
            "By month:", "  2024-02  $1,234.50",
        ])

    def test_text_groups_combine_prefixes_and_months(self):
        path = self.csv(
            "A,2024-02-01,9903.01.25,2.345\n"
            "B,2024-01-31,8471.30.0100,1000\n"
            "C,2024-02-02,9903.01.33,0.004\n"
        )
        code, out, err = self.report(path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "Entries: 3", "Total duty: $1,002.35", "Earliest: 2024-01-31",
            "Latest: 2024-02-02", "By HTS prefix:", "  8471.30.  $1,000.00",
            "  9903.01.  $2.35", "By month:", "  2024-01  $1,000.00",
            "  2024-02  $2.35",
        ])

    def diagnostic_csv(self):
        return self.csv(
            'A,2024-01-01,9903.01.25,"$1,234.50"\n'
            "B,wrong,8471.30.0100,2\n"
            "C,2024-01-02,8471.30.0100,nope\n"
            ",2024-01-02,8471.30.0100,3\n"
            "A,2024-03-01,9903.01.25,999\n"
            "D,2024-02-01,8471.30.0100,0.50\n"
        )

    def test_json_round_trip_and_diagnostics_in_both_modes(self):
        path = self.diagnostic_csv()
        bad_rows = [
            {"line": 3, "reason": "bad entry_date: 'wrong'"},
            {"line": 4, "reason": "bad duty_usd: 'nope'"},
            {"line": 5, "reason": "empty entry_number"},
        ]
        warning = "line 6: duplicate entry A hts 9903.01.25 skipped (first on line 2)"
        expected_stderr = [f"line {row['line']}: {row['reason']}" for row in bad_rows]
        expected_stderr.append("warning: " + warning)
        data, err = self.json_report(path)
        self.assertEqual(data, {
            "count": 2, "total_duty": "1235.00",
            "by_hts": {"9903.01.": "1234.50", "8471.30.": "0.50"},
            "by_month": {"2024-01": "1234.50", "2024-02": "0.50"},
            "earliest": "2024-01-01", "latest": "2024-02-01",
            "bad_rows": bad_rows, "warnings": [warning], "window": None,
        })
        self.assertEqual(Decimal(data["total_duty"]), Decimal("1235.00"))
        self.assertEqual(err.splitlines(), expected_stderr)
        for row in data["bad_rows"]:
            self.assertIs(type(row["line"]), int)
        code, out, err = self.report(path)
        self.assertEqual(code, 0)
        self.assertIn("Entries: 2", out.splitlines())
        self.assertIn("Total duty: $1,235.00", out.splitlines())
        self.assertEqual(err.splitlines(), expected_stderr)
        # Diagnostics concern the input even when all valid entries are filtered out.
        data, err = self.json_report(path, "--hts", "absent")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["bad_rows"], bad_rows)
        self.assertEqual(data["warnings"], [warning])
        self.assertEqual(err.splitlines(), expected_stderr)

    def test_filters_are_inclusive_and_hts_spellings_are_equivalent(self):
        path = self.csv(
            "before,2024-01-31,9903.01.25,1\n"
            "start,2024-02-01,9903.01.25,2\n"
            "end,2024-02-29,8471.30.0100,4\n"
            "other,2024-02-15,1234.56.78,8\n"
            "after,2024-03-01,9903.01.25,16\n"
        )
        cases = [
            ([], 5, "31.00", "2024-01-31", "2024-03-01"),
            (["--hts", "9903"], 3, "19.00", "2024-01-31", "2024-03-01"),
            (["--from", "2024-02-01"], 4, "30.00", "2024-02-01", "2024-03-01"),
            (["--to", "2024-02-29"], 4, "15.00", "2024-01-31", "2024-02-29"),
            (["--from", "2024-02-01", "--to", "2024-02-29"], 3, "14.00", "2024-02-01", "2024-02-29"),
            (["--from", "2024-02-01", "--to", "2024-02-01"], 1, "2.00", "2024-02-01", "2024-02-01"),
        ]
        for options, count, total, earliest, latest in cases:
            with self.subTest(options=options):
                data, err = self.json_report(path, *options)
                self.assertEqual(err, "")
                self.assertEqual((data["count"], data["total_duty"], data["earliest"], data["latest"]),
                                 (count, total, earliest, latest))
        results = []
        for hts in (["--hts", "9903", "8471"], ["--hts", "9903", "--hts", "8471"]):
            data, err = self.json_report(path, *hts, "--from", "2024-02-01", "--to", "2024-02-29")
            self.assertEqual(err, "")
            self.assertEqual((data["count"], data["total_duty"]), (2, "6.00"))
            self.assertEqual(data["by_hts"], {"9903.01.": "2.00", "8471.30.": "4.00"})
            self.assertEqual(data["by_month"], {"2024-02": "6.00"})
            results.append(data)
        self.assertEqual(*results)

    def test_window_json_and_text_include_boundary_and_selected_entries_only(self):
        path = self.csv(
            "closed,2024-01-02,9903.01.25,1\n"
            "boundary,2024-01-03,9903.01.25,2\n"
            "recent,2024-01-04,9903.01.25,3\n"
            "future,2024-07-08,9903.01.25,4\n"
            "excluded,2024-01-03,8471.30.0100,100\n"
        )
        options = ["--hts", "9903", "--window-days", "180", "--as-of", "2024-07-01"]
        expected = [
            {"entry_number": number, "hts": "9903.01.25", "entry_date": day,
             "duty": duty, "status": status, "days_left": left}
            for number, day, duty, status, left in [
                ("closed", "2024-01-02", "1.00", "closed", 0),
                ("boundary", "2024-01-03", "2.00", "open", 0),
                ("recent", "2024-01-04", "3.00", "open", 1),
                ("future", "2024-07-08", "4.00", "open", 187),
            ]
        ]
        data, err = self.json_report(path, *options)
        self.assertEqual(err, "")
        self.assertEqual((data["count"], data["total_duty"]), (4, "10.00"))
        self.assertEqual(data["window"], {"window_days": 180, "as_of": "2024-07-01", "entries": expected})
        self.assertIs(type(data["window"]["window_days"]), int)
        for entry in data["window"]["entries"]:
            self.assertIs(type(entry["days_left"]), int)
            self.assertIsInstance(entry["duty"], str)
        code, out, err = self.report(path, *options)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-5:], [
            "Window: 180 days as of 2024-07-01: 3 open, 1 closed",
            "  closed  9903.01.25  2024-01-02  closed  0 days left",
            "  boundary  9903.01.25  2024-01-03  open  0 days left",
            "  recent  9903.01.25  2024-01-04  open  1 days left",
            "  future  9903.01.25  2024-07-08  open  187 days left",
        ])

    def test_fractional_cent_json_rounds_only_after_summing(self):
        path = self.csv(
            "A,2024-01-01,9903.01.25,0.004\n"
            "B,2024-01-01,9903.01.33,0.004\n"
            "C,2024-02-01,8471.30.0100,2.345\n"
            "D,2024-03-01,1234,-1.005\n"
        )
        data, err = self.json_report(path, "--window-days", "0", "--as-of", "2024-03-01")
        self.assertEqual(err, "")
        self.assertEqual(data["total_duty"], "1.35")
        self.assertEqual(data["by_hts"], {"9903.01.": "0.01", "8471.30.": "2.35", "1234": "-1.01"})
        self.assertEqual(data["by_month"], {"2024-01": "0.01", "2024-02": "2.35", "2024-03": "-1.01"})
        self.assertEqual(data["window"]["window_days"], 0)
        self.assertEqual([e["duty"] for e in data["window"]["entries"]], ["0.00", "0.00", "2.35", "-1.01"])
        self.assertEqual([(e["status"], e["days_left"]) for e in data["window"]["entries"]],
                         [("closed", 0), ("closed", 0), ("closed", 0), ("open", 0)])

    def test_empty_input_and_empty_selection(self):
        for rows, filters in [("", []), ("A,2024-01-01,9903,12\n", ["--hts", "absent"])]:
            for window in ([], ["--window-days", "180", "--as-of", "2024-07-01"]):
                with self.subTest(rows=rows, window=window):
                    path = self.csv(rows)
                    data, err = self.json_report(path, *filters, *window)
                    self.assertEqual(err, "")
                    self.assertEqual(data, {
                        "count": 0, "total_duty": "0.00", "by_hts": {}, "by_month": {},
                        "earliest": None, "latest": None, "bad_rows": [], "warnings": [],
                        "window": {"window_days": 180, "as_of": "2024-07-01", "entries": []} if window else None,
                    })
                    code, out, err = self.report(path, *filters, *window)
                    self.assertEqual((code, err), (0, ""))
                    expected = ["Entries: 0", "Total duty: $0.00", "Earliest: -", "Latest: -",
                                "By HTS prefix:", "By month:"]
                    if window:
                        expected.append("Window: 180 days as of 2024-07-01: 0 open, 0 closed")
                    self.assertEqual(out.splitlines(), expected)

    def test_argument_errors_return_two_without_system_exit(self):
        path = self.csv("A,2024-01-01,9903,1\n")
        base = ["report", str(path)]
        cases = [[], ["unknown"], ["report"], base + ["--unknown"], base + ["--hts"],
                 base + ["--window-days", "180"], base + ["--as-of", "2024-07-01"]]
        for value in ("-1", "1.5", "nope"):
            cases.append(base + ["--window-days", value, "--as-of", "2024-07-01"])
        for option in ("--from", "--to", "--as-of"):
            for value in ("2024-02-30", "not-a-date", "20240101", "2024-2-01"):
                args = base + [option, value]
                if option == "--as-of":
                    args += ["--window-days", "180"]
                cases.append(args)
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error:", err.lower())

    def test_missing_column_returns_two_and_names_column(self):
        path = self.root / "missing-column.csv"
        path.write_text("entry_number,entry_date,hts\nA,2024-01-01,9903\n", encoding="utf-8")
        for options in ([], ["--json"]):
            with self.subTest(options=options):
                code, out, err = self.report(path, *options)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("duty_usd", err)

    def test_unreadable_files_return_one_with_single_line_error(self):
        invalid = self.root / "invalid-utf8.csv"
        invalid.write_bytes(HEADER.encode("ascii") + b"A,2024-01-01,9903,1,\xff\n")
        for path in (self.root / "nonexistent.csv", self.root, invalid):
            for options in ([], ["--json"]):
                with self.subTest(path=path, options=options):
                    code, out, err = self.report(path, *options)
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertEqual(len(err.splitlines()), 1)
                    self.assertTrue(err.startswith("error: "), err)
                    self.assertTrue(err[len("error: "):].strip())

    def test_help_returns_zero_without_system_exit(self):
        for argv in (["--help"], ["report", "--help"]):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(argv)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("usage:", out.lower())
                self.assertIn("report", out)

    def test_main_defaults_to_sys_argv(self):
        path = self.csv("A,2024-01-01,9903,7\n")
        with patch.object(sys, "argv", ["entry_ledger", "report", str(path), "--json"]):
            code, out, err = self.invoke(None)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["total_duty"], "7.00")

    def test_python_module_entry_point(self):
        path = self.csv('A,2024-01-01,9903.01.25,"$1,234.50"\n', bom=True)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "projects.entry_ledger", "report", str(path)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), [
            "Entries: 1", "Total duty: $1,234.50", "Earliest: 2024-01-01",
            "Latest: 2024-01-01", "By HTS prefix:", "  9903.01.  $1,234.50",
            "By month:", "  2024-01  $1,234.50",
        ])


if __name__ == "__main__":
    unittest.main()
