"""Behavioural contract for the entry ledger model and CSV loader."""

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from projects.entry_ledger import (
    Entry, LoadResult, MissingColumnError, REQUIRED_COLUMNS, load,
)


HEADER = "entry_number,entry_date,hts,duty_usd\n"


class EntryLedgerLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_csv(self, data, name="entries.csv"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def load_text(self, text):
        return load(self.write_csv(text.encode("utf-8")))

    def assert_result(self, result, entries=(), bad_rows=(), warnings=()):
        self.assertIsInstance(result, LoadResult)
        self.assertIsInstance(result.entries, tuple)
        self.assertIsInstance(result.bad_rows, tuple)
        self.assertIsInstance(result.warnings, tuple)
        self.assertEqual(result.entries, entries)
        self.assertEqual(result.bad_rows, bad_rows)
        self.assertEqual(result.warnings, warnings)
        for entry in result.entries:
            self.assertIsInstance(entry, Entry)
            self.assertIs(type(entry.entry_date), date)
            self.assertIsInstance(entry.duty, Decimal)
        for number, reason in result.bad_rows:
            self.assertIs(type(number), int)
            self.assertIsInstance(reason, str)

    def test_models_are_frozen_dataclasses_with_positional_fields(self):
        entry = Entry("0007", date(2024, 2, 29), "8471.30.0100", Decimal("12"))
        self.assertTrue(dataclasses.is_dataclass(entry))
        self.assertEqual(
            dataclasses.astuple(entry),
            ("0007", date(2024, 2, 29), "8471.30.0100", Decimal("12"), None),
        )
        for field in dataclasses.fields(entry):
            with self.subTest(field=field.name):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(entry, field.name, getattr(entry, field.name))
        result = LoadResult((entry,), ((3, "empty entry_number"),), ("warning",))
        self.assertTrue(dataclasses.is_dataclass(result))
        self.assertEqual(dataclasses.astuple(result)[1:], (((3, "empty entry_number"),), ("warning",)))
        for field in dataclasses.fields(result):
            with self.subTest(field=field.name):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(result, field.name, ())

    def test_clean_lf_and_bom_crlf_are_equal_and_accept_both_path_types(self):
        content = HEADER + '0007,2024-02-29,8471.30.0100,"$1,234.50"\n0008,2023-12-01,9903.01.25,12\n'
        lf = load(self.write_csv(content.encode("utf-8"), "lf.csv"))
        crlf = load(str(self.write_csv(b"\xef\xbb\xbf" + content.replace("\n", "\r\n").encode("utf-8"), "crlf.csv")))
        self.assert_result(lf, (
            Entry("0007", date(2024, 2, 29), "8471.30.0100", Decimal("1234.50")),
            Entry("0008", date(2023, 12, 1), "9903.01.25", Decimal("12")),
        ))
        self.assertEqual(crlf, lf)

    def test_reordered_headers_whitespace_importer_and_unknown_columns(self):
        result = self.load_text(
            ' EXTRA , DuTy_UsD , HTS , IMPORTER , ENTRY_DATE , Entry_Number \n'
            'ignored," $1,234.50 ", 0012.00.0000 , Acme Café , 2024-01-02 , 0001 \n'
            'ignored, -5.00 , 9903.01.25 ,   , 2024-01-03 , 0002 \n'
        )
        self.assert_result(result, (
            Entry("0001", date(2024, 1, 2), "0012.00.0000", Decimal("1234.50"), "Acme Café"),
            Entry("0002", date(2024, 1, 3), "9903.01.25", Decimal("-5.00"), None),
        ))

    def test_money_removes_every_currency_separator_and_preserves_precision(self):
        values = (
            ('" $ 1,$2 3,4.50$ "', "1234.50"),
            ("-5.00", "-5.00"),
            ("0.10000000000000000000000000001", "0.10000000000000000000000000001"),
            ("9007199254740993.01", "9007199254740993.01"),
            ("1E+3", "1E+3"),
            ("0", "0"),
        )
        result = self.load_text(HEADER + "".join(
            f"E{i},2024-01-01,8471.30.0100,{raw}\n" for i, (raw, _) in enumerate(values)
        ))
        self.assert_result(result, tuple(
            Entry(f"E{i}", date(2024, 1, 1), "8471.30.0100", Decimal(expected))
            for i, (_, expected) in enumerate(values)
        ))

    def test_each_bad_value_is_reported_exactly_and_neighbours_survive(self):
        cases = [(" ,nonsense,hts,nope", "empty entry_number")]
        for raw in ("", "2024-02-30", "2023-02-29", "2024-13-01", "0000-01-01",
                    "2024-2-01", "2024-01-1", "20240101", "2024-W01-1", "2024-01-01T00:00:00"):
            cases.append((f"bad, {raw} ,hts,nope", f"bad entry_date: '{raw}'"))
        for raw in ("", "not-money", "$ , $", "NaN", "sNaN", "Infinity", "-Infinity", "12.3.4"):
            # Quoting allows commas within the raw amount.
            cases.append((f'bad,2024-01-02,hts," {raw} "', f"bad duty_usd: '{raw}'"))
        for row, reason in cases:
            with self.subTest(row=row):
                result = self.load_text(
                    HEADER + "before,2024-01-01,hts,1\n" + row + "\nafter,2024-01-03,hts,2\n"
                )
                self.assert_result(result, (
                    Entry("before", date(2024, 1, 1), "hts", Decimal("1")),
                    Entry("after", date(2024, 1, 3), "hts", Decimal("2")),
                ), ((3, reason),))

    def test_blank_rows_short_rows_and_physical_multiline_numbers(self):
        result = self.load_text(
            HEADER.rstrip("\n") + ",importer\n"
            "\n"
            " , , , , \n"
            'A,2024-01-01,hts,1,"First\nSecond"\n'
            "\n"
            "B\n"
            "C,2024-01-01\n"
            "D,2024-01-02,,0\n"
            'E,wrong,hts,1,"two\nlines"\n'
            "A,2024-02-01,hts,99,later\n"
        )
        self.assert_result(result, (
            Entry("A", date(2024, 1, 1), "hts", Decimal("1"), "First\nSecond"),
            Entry("D", date(2024, 1, 2), "", Decimal("0")),
        ), (
            (7, "bad entry_date: ''"),
            (8, "bad duty_usd: ''"),
            (11, "bad entry_date: 'wrong'"),
        ), ("line 12: duplicate entry A hts hts skipped (first on line 5)",))

    def test_duplicate_pair_keeps_first_values_and_different_hts_is_distinct(self):
        result = self.load_text(
            HEADER.rstrip("\n") + ",importer\n"
            "A,2024-01-01,001,1,First\n"
            "A,2024-02-02,001,99,Second\n"
            "A,2024-03-03,002,3,Third\n"
            "B,2024-04-04,001,4,Fourth\n"
        )
        self.assert_result(result, (
            Entry("A", date(2024, 1, 1), "001", Decimal("1"), "First"),
            Entry("A", date(2024, 3, 3), "002", Decimal("3"), "Third"),
            Entry("B", date(2024, 4, 4), "001", Decimal("4"), "Fourth"),
        ), warnings=("line 3: duplicate entry A hts 001 skipped (first on line 2)",))

    def test_invalid_rows_do_not_claim_pairs_and_validation_precedes_duplicates(self):
        result = self.load_text(
            HEADER + "A,bad,001,1\nA,2024-01-01,001,bad\n"
            " A ,2024-01-02, 001 ,2\nA,bad,001,3\n"
            "A,2024-01-03,001,4\nA,2024-01-04,001,5\n"
        )
        self.assert_result(result, (Entry("A", date(2024, 1, 2), "001", Decimal("2")),), (
            (2, "bad entry_date: 'bad'"), (3, "bad duty_usd: 'bad'"), (5, "bad entry_date: 'bad'"),
        ), (
            "line 6: duplicate entry A hts 001 skipped (first on line 4)",
            "line 7: duplicate entry A hts 001 skipped (first on line 4)",
        ))

    def test_missing_columns_include_all_names_in_required_order(self):
        self.assertEqual(REQUIRED_COLUMNS, ("entry_number", "entry_date", "hts", "duty_usd"))
        for text, missing in (
            ("entry_number,entry_date,hts\n", ("duty_usd",)),
            ("duty_usd,entry_date\n", ("entry_number", "hts")),
            ("", REQUIRED_COLUMNS),
            ("\n" + HEADER, REQUIRED_COLUMNS),
        ):
            with self.subTest(text=text):
                with self.assertRaises(MissingColumnError) as caught:
                    self.load_text(text)
                self.assertIsInstance(caught.exception, ValueError)
                self.assertEqual(caught.exception.columns, missing)
                for name in missing:
                    self.assertIn(name, str(caught.exception))

    def test_header_only_returns_empty_tuples(self):
        self.assert_result(self.load_text(HEADER))

    def test_filesystem_and_decoding_errors_propagate(self):
        with self.assertRaises(FileNotFoundError):
            load(self.root / "does-not-exist.csv")
        with self.assertRaises(OSError):
            load(self.root)
        with self.assertRaises(UnicodeDecodeError):
            load(self.write_csv(HEADER.encode("ascii") + b"A,2024-01-01,hts,1,\xff\n"))


if __name__ == "__main__":
    unittest.main()
