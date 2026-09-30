"""Behavioural contract for pure entry ledger reporting."""

import dataclasses
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
import unittest

import projects.entry_ledger as ledger
from projects.entry_ledger.model import Entry
from projects.entry_ledger.report import (
    Summary, WindowFlag, flag_window, select, summarize, to_cents,
)


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            Entry("late", date(2024, 3, 1), "9903.01.25", Decimal("1")),
            Entry("early", date(2024, 1, 1), "8471.30.0100", Decimal("2")),
            Entry("middle", date(2024, 2, 1), "9903.01.33", Decimal("3")),
            Entry("other", date(2024, 2, 15), "0099.03", Decimal("4")),
        ]

    def assert_selection(self, expected, **filters):
        before = self.entries.copy()
        result = select(self.entries, **filters)
        self.assertIsInstance(result, list)
        self.assertEqual(result, expected)
        self.assertEqual(self.entries, before)

    def test_no_filters_and_empty_prefixes_keep_input_order(self):
        self.assert_selection(self.entries)
        self.assert_selection(self.entries, hts_prefixes=[])

    def test_single_and_multiple_prefixes_are_an_ordered_union(self):
        self.assert_selection([self.entries[0], self.entries[2]], hts_prefixes=("9903",))
        self.assert_selection(self.entries[:3], hts_prefixes=["8471", "9903"])
        self.assert_selection(
            [self.entries[0], self.entries[2]],
            hts_prefixes=("99", "9903", "9903"),
        )

    def test_prefix_matching_is_not_substring_matching(self):
        self.assert_selection([], hts_prefixes=("03.01",))
        self.assert_selection([], hts_prefixes=("1234",))
        self.assert_selection([self.entries[3]], hts_prefixes=("00",))

    def test_start_is_inclusive(self):
        self.assert_selection(
            [self.entries[0], self.entries[2], self.entries[3]], start=date(2024, 2, 1)
        )
        self.assert_selection(
            [self.entries[0], self.entries[3]], start=date(2024, 2, 2)
        )

    def test_end_is_inclusive(self):
        self.assert_selection(self.entries[1:], end=date(2024, 2, 15))
        self.assert_selection(self.entries[1:3], end=date(2024, 2, 14))

    def test_both_bounds_and_prefix_must_all_pass(self):
        self.assert_selection(
            self.entries[2:], start=date(2024, 2, 1), end=date(2024, 2, 15)
        )
        self.assert_selection(
            [self.entries[2]], hts_prefixes=("9903",),
            start=date(2024, 2, 1), end=date(2024, 2, 15),
        )
        self.assert_selection(
            [self.entries[2]], start=date(2024, 2, 1), end=date(2024, 2, 1)
        )
        self.assert_selection([], start=date(2024, 3, 1), end=date(2024, 1, 1))

    def test_accepts_tuple_and_single_pass_iterable(self):
        for entries in (tuple(self.entries), (entry for entry in self.entries)):
            with self.subTest(iterable=type(entries).__name__):
                result = select(entries, hts_prefixes=["9903"], end=date(2024, 2, 1))
                self.assertIsInstance(result, list)
                self.assertEqual(result, [self.entries[2]])
        self.assertEqual(select(iter(())), [])

    def test_duplicate_entries_are_preserved(self):
        entry = self.entries[0]
        self.assertEqual(select(iter([entry, entry])), [entry, entry])


class SummarizeTests(unittest.TestCase):
    def test_exact_totals_groups_sorted_keys_and_date_extremes(self):
        entries = [
            Entry("a", date(2024, 3, 15), "9903.01.25", Decimal("1234.50")),
            Entry("b", date(2023, 12, 31), "9903.01.33", Decimal("0.25")),
            Entry("c", date(2024, 1, 2), "8471.30.0100", Decimal("2.345")),
            Entry("d", date(2024, 1, 31), "8471.30.0200", Decimal("0.006")),
            Entry("e", date(2024, 3, 1), "12", Decimal("-1.111")),
            Entry("f", date(2024, 2, 29), "0001", Decimal("0")),
        ]
        before = entries.copy()
        result = summarize(entries)
        self.assertIsInstance(result, Summary)
        self.assertIs(type(result.count), int)
        self.assertEqual(result.count, 6)
        self.assertEqual(result.total, Decimal("1235.990"))
        self.assertEqual(result.by_hts, {
            "0001": Decimal("0"), "12": Decimal("-1.111"),
            "8471.30.": Decimal("2.351"), "9903.01.": Decimal("1234.75"),
        })
        self.assertEqual(list(result.by_hts), ["0001", "12", "8471.30.", "9903.01."])
        self.assertEqual(result.by_month, {
            "2023-12": Decimal("0.25"), "2024-01": Decimal("2.351"),
            "2024-02": Decimal("0"), "2024-03": Decimal("1233.389"),
        })
        self.assertEqual(list(result.by_month), ["2023-12", "2024-01", "2024-02", "2024-03"])
        self.assertEqual(result.earliest, date(2023, 12, 31))
        self.assertEqual(result.latest, date(2024, 3, 15))
        self.assertEqual(entries, before)
        self.assert_money_types(result)

    def assert_money_types(self, result):
        self.assertIsInstance(result.total, Decimal)
        self.assertIsInstance(result.by_hts, dict)
        self.assertIsInstance(result.by_month, dict)
        for amount in (*result.by_hts.values(), *result.by_month.values()):
            self.assertIsInstance(amount, Decimal)

    def test_fractional_cents_are_not_rounded_even_in_total(self):
        entries = [
            Entry("a", date(2024, 1, 1), "9903.01.25", Decimal("0.004")),
            Entry("b", date(2024, 1, 2), "9903.01.33", Decimal("0.004")),
        ]
        result = summarize(entries)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.total, Decimal("0.008"))
        self.assertEqual(result.by_hts, {"9903.01.": Decimal("0.008")})
        self.assertEqual(result.by_month, {"2024-01": Decimal("0.008")})
        self.assert_money_types(result)

    def test_empty_summary(self):
        result = summarize([])
        self.assertIsInstance(result, Summary)
        self.assertEqual(result.count, 0)
        self.assertEqual(result.total, Decimal("0"))
        self.assertEqual(result.by_hts, {})
        self.assertEqual(result.by_month, {})
        self.assertIsNone(result.earliest)
        self.assertIsNone(result.latest)
        self.assert_money_types(result)

    def test_repeated_entries_are_counted_and_single_date_is_both_extremes(self):
        entry = Entry("same", date(2024, 2, 29), "12345678X", Decimal("1.005"))
        result = summarize([entry, entry])
        self.assertEqual(result.count, 2)
        self.assertEqual(result.total, Decimal("2.010"))
        self.assertEqual(result.by_hts, {"12345678": Decimal("2.010")})
        self.assertEqual(result.by_month, {"2024-02": Decimal("2.010")})
        self.assertEqual(result.earliest, entry.entry_date)
        self.assertEqual(result.latest, entry.entry_date)


class WindowTests(unittest.TestCase):
    def test_boundaries_old_and_future_entries_keep_input_order(self):
        as_of = date(2024, 7, 1)
        ages = [181, -7, 179, 400, 180]
        entries = [
            Entry(str(age), as_of - timedelta(days=age), "9903", Decimal("1"))
            for age in ages
        ]
        before = entries.copy()
        flags = flag_window(entries, window_days=180, as_of=as_of)
        self.assertIsInstance(flags, list)
        self.assertEqual(len(flags), 5)
        self.assertEqual([flag.entry for flag in flags], entries)
        self.assertEqual(
            [(flag.status, flag.days_left) for flag in flags],
            [("closed", 0), ("open", 187), ("open", 1), ("closed", 0), ("open", 0)],
        )
        for flag in flags:
            self.assertIsInstance(flag, WindowFlag)
            self.assertIs(type(flag.days_left), int)
        self.assertEqual(entries, before)

    def test_zero_and_custom_windows(self):
        as_of = date(2024, 2, 29)
        today = Entry("today", as_of, "12", Decimal("0"))
        yesterday = Entry("yesterday", date(2024, 2, 28), "12", Decimal("0"))
        for window, expected in (
            (0, [("open", 0), ("closed", 0)]),
            (1, [("open", 1), ("open", 0)]),
            (10, [("open", 10), ("open", 9)]),
        ):
            with self.subTest(window=window):
                flags = flag_window([today, yesterday], window, as_of)
                self.assertEqual([(f.status, f.days_left) for f in flags], expected)
        self.assertEqual(flag_window([], 0, as_of), [])

    def test_negative_window_is_rejected_even_for_empty_input(self):
        entry = Entry("a", date(2024, 1, 1), "12", Decimal("1"))
        for entries in ([], [entry]):
            for window in (-1, -180):
                with self.subTest(entries=entries, window=window):
                    with self.assertRaises(ValueError):
                        flag_window(entries, window, date(2024, 1, 1))


class DisplayAndPublicApiTests(unittest.TestCase):
    def test_to_cents_uses_half_up_and_two_decimal_places(self):
        cases = [
            ("2.345", "2.35"), ("-2.345", "-2.35"), ("1", "1.00"),
            ("2.344", "2.34"), ("-2.344", "-2.34"), ("0", "0.00"),
            ("9.999", "10.00"), ("0.005", "0.01"),
        ]
        with localcontext() as context:
            context.rounding = ROUND_DOWN
            for source, expected in cases:
                with self.subTest(source=source):
                    result = to_cents(Decimal(source))
                    self.assertIsInstance(result, Decimal)
                    self.assertEqual(result, Decimal(expected))
                    self.assertEqual(result.as_tuple().exponent, -2)

    def test_summary_and_window_flag_are_frozen_dataclasses(self):
        entry = Entry("a", date(2024, 1, 1), "12", Decimal("1"))
        for result in (summarize([entry]), flag_window([entry], 180, entry.entry_date)[0]):
            with self.subTest(model=type(result).__name__):
                self.assertTrue(dataclasses.is_dataclass(result))
                for field in dataclasses.fields(result):
                    with self.subTest(field=field.name):
                        with self.assertRaises(dataclasses.FrozenInstanceError):
                            setattr(result, field.name, getattr(result, field.name))

    def test_package_exports_reporting_api_and_retains_loader_api(self):
        for item in (Entry, Summary, WindowFlag, select, summarize, flag_window, to_cents):
            with self.subTest(export=item.__name__):
                self.assertIs(getattr(ledger, item.__name__), item)
        for name in ("LoadResult", "MissingColumnError", "REQUIRED_COLUMNS", "load"):
            with self.subTest(export=name):
                self.assertTrue(hasattr(ledger, name), f"Missing public export: {name}")
        self.assertTrue(callable(ledger.load))


if __name__ == "__main__":
    unittest.main()
