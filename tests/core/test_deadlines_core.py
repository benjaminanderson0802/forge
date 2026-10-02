"""Behavioural contract for deadline CSV loading and classification."""

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from projects.deadlines import classify, load
from projects.deadlines import core


HEADER = "item,venture,due,status\n"


def row(item, due, venture="Venture", status="open"):
    return {"item": item, "venture": venture, "due": due, "status": status}


def entry(item, due, days, venture="Venture"):
    return {"item": item, "venture": venture, "due": due, "days": days}


class DeadlineLoadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_csv(self, text, encoding="utf-8"):
        path = self.root / "deadlines.csv"
        path.write_bytes(text.encode(encoding))
        return path

    def assert_loaded(self, result, rows, errors):
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], list)
        self.assertIsInstance(result[1], list)
        self.assertEqual(result, (rows, errors))
        for loaded in result[0]:
            self.assertEqual(set(loaded), {"item", "venture", "due", "status"})
            self.assertIs(type(loaded["due"]), date)

    def test_reordered_normalized_headers_extra_columns_and_stripped_values(self):
        path = self.write_csv(
            " Extra , STATUS , Due , VENTURE , Item \n"
            "ignored, Open , 2026-10-02 , Alpha , Launch \n"
            "ignored,DONE,2024-02-29, Beta , Review \n"
        )
        expected = [
            row("Launch", date(2026, 10, 2), "Alpha"),
            row("Review", date(2024, 2, 29), "Beta", "done"),
        ]
        for supplied_path in (path, str(path)):
            with self.subTest(path_type=type(supplied_path).__name__):
                self.assert_loaded(load(supplied_path), expected, [])

    def test_missing_columns_are_named_in_required_order(self):
        for text, missing in (
            ("item,venture,status\n", ["due"]),
            ("venture,item\n", ["due", "status"]),
            ("status,due\n", ["item", "venture"]),
            ("", ["item", "venture", "due", "status"]),
            ("\n" + HEADER, ["item", "venture", "due", "status"]),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    load(self.write_csv(text))
                message = str(caught.exception).lower()
                for name in missing:
                    self.assertIn(name, message)
                positions = [message.index(name) for name in missing]
                self.assertEqual(positions, sorted(positions))

    def test_bad_rows_are_reported_in_order_and_good_neighbours_survive(self):
        path = self.write_csv(
            HEADER
            + "Before,A,2026-10-01,open\n"
            + "\n"
            + "Impossible,A,2026-02-30,open\n"
            + "Slash,A,2026/10/02,open\n"
            + "Empty,A,,open\n"
            + "Unknown,A,2026-10-02,pending\n"
            + "Both,A,20261002,pending\n"
            + "After,B,2026-10-03,done\n"
        )
        self.assert_loaded(load(path), [
            row("Before", date(2026, 10, 1), "A"),
            row("After", date(2026, 10, 3), "B", "done"),
        ], [
            {"line": 4, "reason": "bad date: '2026-02-30'"},
            {"line": 5, "reason": "bad date: '2026/10/02'"},
            {"line": 6, "reason": "bad date: ''"},
            {"line": 7, "reason": "unknown status: 'pending'"},
            {"line": 8, "reason": "bad date: '20261002'"},
        ])

    def test_date_requires_exact_iso_shape_and_a_real_calendar_date(self):
        for raw in (
            "2026-2-03", "2026-10-2", "20261002", "2026-W40-5",
            "2026-10-02T00:00:00", "2026-13-01", "2026-00-01",
            "2025-02-29", "0000-01-01", "not-a-date",
        ):
            with self.subTest(raw=raw):
                self.assert_loaded(
                    load(self.write_csv(HEADER + f"Bad,V,{raw},open\n")),
                    [], [{"line": 2, "reason": f"bad date: '{raw}'"}],
                )

    def test_physical_line_numbers_and_embedded_crlf_are_preserved(self):
        path = self.write_csv(
            "item,venture,due,status\r\n"
            '"First\r\nSecond",V,2026-10-02,open\r\n'
            "\r\n"
            '"Bad\r\nitem",V,wrong,open\r\n'
            "Unknown,V,2026-10-02,pending\r\n"
        )
        self.assert_loaded(load(path), [
            row("First\r\nSecond", date(2026, 10, 2), "V"),
        ], [
            {"line": 6, "reason": "bad date: 'wrong'"},
            {"line": 7, "reason": "unknown status: 'pending'"},
        ])

    def test_short_rows_supply_empty_fields_and_only_empty_lines_are_skipped(self):
        path = self.write_csv(
            "due,status,item,venture,extra\n"
            "2026-10-02,open\n"
            "2026-10-03,done,Finished\n"
            "2026-10-04\n"
            "\n"
            ",,,,\n"
        )
        self.assert_loaded(load(path), [
            row("", date(2026, 10, 2), ""),
            row("Finished", date(2026, 10, 3), "", "done"),
        ], [
            {"line": 4, "reason": "unknown status: ''"},
            {"line": 6, "reason": "bad date: ''"},
        ])

    def test_bom_non_ascii_and_quoted_commas(self):
        path = self.write_csv(
            HEADER + '"Caf\u00e9 launch, phase 1",M\u00fcnchen,2026-10-02,open\n',
            encoding="utf-8-sig",
        )
        self.assert_loaded(load(path), [
            row("Caf\u00e9 launch, phase 1", date(2026, 10, 2), "M\u00fcnchen"),
        ], [])

    def test_header_only_and_blank_lines_return_two_empty_lists(self):
        self.assert_loaded(load(self.write_csv(HEADER + "\n\n")), [], [])

    def test_missing_file_propagates_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load(self.root / "absent.csv")


class DeadlineClassifyTests(unittest.TestCase):
    today = date(2026, 10, 2)

    def test_boundaries_done_filtering_sort_order_and_exact_output(self):
        rows = [
            row("Z", date(2026, 10, 1), "A"),
            row("End", date(2026, 10, 5)),
            row("A", date(2026, 10, 1), "B"),
            row("Z", self.today, "A"),
            row("A", date(2026, 10, 1), "A"),
            row("Oldest", date(2026, 9, 30), "Z"),
            row("A", self.today, "B"),
            row("A", self.today, "A"),
            row("Next", date(2026, 10, 6)),
            row("Far", date(2027, 1, 1)),
        ]
        rows.extend(row("Done", due, status="done") for due in (
            date(2026, 10, 1), self.today, date(2026, 10, 5), date(2026, 10, 6),
        ))
        result = classify(rows, self.today, days=3)
        self.assertEqual(result, {
            "overdue": [
                entry("Oldest", "2026-09-30", -2, "Z"),
                entry("A", "2026-10-01", -1, "A"),
                entry("Z", "2026-10-01", -1, "A"),
                entry("A", "2026-10-01", -1, "B"),
            ],
            "due_soon": [
                entry("A", "2026-10-02", 0, "A"),
                entry("Z", "2026-10-02", 0, "A"),
                entry("A", "2026-10-02", 0, "B"),
                entry("End", "2026-10-05", 3),
            ],
            "later": 2,
        })
        self.assertIs(type(result["later"]), int)
        for group in (result["overdue"], result["due_soon"]):
            for classified in group:
                self.assertIs(type(classified["days"]), int)

    def test_zero_day_window_includes_only_today(self):
        rows = [row(str(offset), self.today + timedelta(days=offset))
                for offset in (-1, 0, 1)]
        self.assertEqual(classify(rows, self.today, days=0), {
            "overdue": [entry("-1", "2026-10-01", -1)],
            "due_soon": [entry("0", "2026-10-02", 0)],
            "later": 1,
        })

    def test_default_window_is_fourteen_days(self):
        rows = [row("Edge", date(2026, 10, 16)),
                row("Outside", date(2026, 10, 17))]
        self.assertEqual(classify(rows, self.today), {
            "overdue": [],
            "due_soon": [entry("Edge", "2026-10-16", 14)],
            "later": 1,
        })

    def test_negative_days_raise_even_when_no_rows_are_open(self):
        for rows in ([], [row("Open", self.today)],
                     [row("Done", self.today, status="done")]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    classify(rows, self.today, days=-1)

    def test_empty_and_all_done_inputs_have_empty_groups(self):
        for rows in ([], [row("Done", self.today, status="done")]):
            with self.subTest(rows=rows):
                result = classify(rows, self.today)
                self.assertEqual(result, {"overdue": [], "due_soon": [], "later": 0})
                self.assertIs(type(result["later"]), int)

    def test_classification_does_not_mutate_list_or_original_dicts(self):
        rows = [row("Later", date(2026, 11, 1)),
                row("Today", self.today), row("Past", date(2026, 9, 1)),
                row("Done", self.today, status="done")]
        before = deepcopy(rows)
        originals = list(rows)
        classify(rows, self.today)
        self.assertEqual(rows, before)
        for actual, original, expected in zip(rows, originals, before):
            self.assertIs(actual, original)
            self.assertEqual(original, expected)


class DeadlinePublicApiTests(unittest.TestCase):
    def test_package_reexports_core_functions(self):
        self.assertIs(load, core.load)
        self.assertIs(classify, core.classify)


if __name__ == "__main__":
    unittest.main()
