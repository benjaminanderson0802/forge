"""T1C2: durable drift state, merge recording (each merge once, gain per merge), stall thresholds, active time."""
import json
import math
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from core import drift


def score_of(credit):
    """A score function from a per-task credit table (tasks not listed add nothing)."""
    return lambda done: sum((Fraction(credit.get(t, 0)) for t in done), Fraction(0))


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_corrupt_file_loads_as_none(self):
        self.assertIsNone(drift.load(self.state))
        (self.state / drift.DRIFT_FILE).write_text("{not json", encoding="utf-8")
        self.assertIsNone(drift.load(self.state))
        (self.state / drift.DRIFT_FILE).write_text("[]", encoding="utf-8")
        self.assertIsNone(drift.load(self.state))

    def test_save_is_atomic_json_and_round_trips(self):
        d = drift.adopt(["T1"], {"T1"}, drift_due=False, active_s=5.0)
        drift.save(self.state, d)
        self.assertEqual(drift.load(self.state), d)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [drift.DRIFT_FILE])
        raw = (self.state / drift.DRIFT_FILE).read_bytes()
        self.assertNotIn(b"\r\n", raw)


class AdoptTests(unittest.TestCase):
    def test_existing_work_is_the_baseline(self):
        d = drift.adopt(["T1", "T2"], {"T1", "T2", "T0"}, drift_due=False, active_s=42.0)
        self.assertEqual(sorted(d["counted"]), ["T0", "T1", "T2"])
        self.assertEqual(d["no_gain"], 0)
        self.assertEqual(d["active_mark"], 42.0)
        self.assertIsNone(d["stall"])
        self.assertIsNone(d["replan"])

    def test_pending_drift_leaves_the_newest_mark_uncounted(self):
        d = drift.adopt(["T1", "T2"], {"T1", "T2"}, drift_due=True, active_s=0.0)
        self.assertEqual(d["counted"], ["T1"])

    def test_first_new_merge_gets_no_credit_for_history(self):
        """Adopted with T1 and T2 done (each worth coverage); T3 adds nothing: no gain for T3."""
        d = drift.adopt(["T1", "T2"], {"T1", "T2"}, drift_due=False, active_s=0.0)
        ev = drift.record_merges(d, ["T1", "T2", "T3"], {"T1", "T2", "T3"}, 10.0,
                                 score_of({"T1": 1, "T2": 1, "T3": 0}))
        self.assertEqual([(e["tid"], e["gain"]) for e in ev], [("T3", False)])
        self.assertEqual(d["no_gain"], 1)


class RecordMergesTests(unittest.TestCase):
    def fresh(self, active=0.0):
        return drift.adopt([], set(), drift_due=False, active_s=active)

    def test_each_merge_is_counted_once(self):
        d = self.fresh()
        s = score_of({"T1": 1})
        self.assertEqual(len(drift.record_merges(d, ["T1"], {"T1"}, 1.0, s)), 1)
        self.assertEqual(drift.record_merges(d, ["T1"], {"T1"}, 2.0, s), [])
        self.assertEqual(d["counted"], ["T1"])
        self.assertEqual(d["active_mark"], 1.0)  # a re-run does not move the window

    def test_gain_resets_and_no_gain_counts(self):
        d = self.fresh()
        s = score_of({"A": 1, "D": Fraction(1, 4)})
        ev = drift.record_merges(d, ["A", "B", "C"], {"A", "B", "C"}, 0.0, s)
        self.assertEqual([e["gain"] for e in ev], [True, False, False])
        self.assertEqual(d["no_gain"], 2)
        drift.record_merges(d, ["A", "B", "C", "D"], {"A", "B", "C", "D"}, 0.0, s)
        self.assertEqual(d["no_gain"], 0)

    def test_gain_is_measured_per_merge_even_in_one_batch(self):
        """Two merges recorded together: each is credited only with its own rise."""
        d = self.fresh()
        ev = drift.record_merges(d, ["A", "B"], {"A", "B"}, 0.0, score_of({"A": 1}))
        self.assertEqual([(e["tid"], e["gain"]) for e in ev], [("A", True), ("B", False)])

    def test_unverified_merge_counts_but_gains_nothing(self):
        d = self.fresh()
        ev = drift.record_merges(d, ["A"], set(), 0.0, score_of({"A": 1}))
        self.assertEqual(ev[0]["gain"], False)
        self.assertEqual(d["no_gain"], 1)

    def test_deferred_merges_wait(self):
        d = self.fresh()
        ev = drift.record_merges(d, ["A", "B"], {"B"}, 3.0, score_of({"A": 1, "B": 1}), deferred={"A"})
        self.assertEqual([e["tid"] for e in ev], ["B"])
        self.assertNotIn("A", d["counted"])

    def test_without_a_spec_the_window_resets_but_no_gain_is_unknown(self):
        d = self.fresh()
        ev = drift.record_merges(d, ["A", "B", "C"], {"A"}, 9.0, None)
        self.assertEqual([e["gain"] for e in ev], [None, None, None])
        self.assertEqual(d["no_gain"], 0)
        self.assertEqual(d["active_mark"], 9.0)

    def test_gain_clears_the_auto_replan_count(self):
        d = self.fresh()
        d["auto_replans"] = 2
        drift.record_merges(d, ["A"], {"A"}, 0.0, score_of({"A": 1}))
        self.assertEqual(d["auto_replans"], 0)

    def test_history_is_bounded(self):
        d = self.fresh()
        marks = [f"T{i}" for i in range(drift.HISTORY_KEEP + 20)]
        drift.record_merges(d, marks, set(), 0.0, score_of({}))
        self.assertEqual(len(d["history"]), drift.HISTORY_KEEP)
        self.assertEqual(len(d["counted"]), len(marks))


class ThresholdTests(unittest.TestCase):
    def test_no_gain_due_at_the_limit(self):
        d = drift.adopt([], set(), drift_due=False, active_s=0.0)
        d["no_gain"] = 2
        self.assertFalse(drift.no_gain_due(d, 3))
        d["no_gain"] = 3
        self.assertTrue(drift.no_gain_due(d, 3))

    def test_idle_due_counts_active_seconds_since_the_last_merge(self):
        d = drift.adopt([], set(), drift_due=False, active_s=100.0)
        self.assertFalse(drift.idle_due(d, 100.0 + 7199, 7200))
        self.assertTrue(drift.idle_due(d, 100.0 + 7200, 7200))

    def test_a_merge_recorded_first_resets_the_window(self):
        """Boundary: the attempt that crosses 2 active hours merges; recording it first means no stall."""
        d = drift.adopt([], set(), drift_due=False, active_s=0.0)
        active = 7300.0
        drift.record_merges(d, ["T1"], {"T1"}, active, score_of({"T1": 1}))
        self.assertFalse(drift.idle_due(d, active, 7200))

    def test_restart_window(self):
        d = drift.adopt([], set(), drift_due=False, active_s=0.0)
        drift.restart_window(d, 500.0)
        self.assertEqual((d["active_mark"], d["no_gain"]), (500.0, 0))


class ReplanIdTests(unittest.TestCase):
    def test_ids_are_unique_and_durable(self):
        d = drift.adopt([], set(), drift_due=False, active_s=0.0)
        a = drift.new_replan(d, ["drifting"], "keeper")
        self.assertEqual(d["replan"]["id"], a)
        self.assertEqual(d["replan"]["attempts"], 0)
        b = drift.new_replan(d, ["again"], "no gain")
        self.assertNotEqual(a, b)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accumulates_and_persists(self):
        a = drift.Activity(self.state)
        self.assertEqual(a.total(), 0.0)
        a.add(1.5)
        a.add(2.5)
        self.assertEqual(drift.Activity(self.state).total(), 4.0)

    def test_ignores_negative_nan_and_inf(self):
        a = drift.Activity(self.state)
        for bad in (-5.0, math.nan, math.inf, "x", None):
            a.add(bad)
        self.assertEqual(a.total(), 0.0)
        self.assertFalse((self.state / drift.ACTIVITY_FILE).exists())

    def test_corrupt_file_restarts_from_zero(self):
        (self.state / drift.ACTIVITY_FILE).write_text("garbage", encoding="utf-8")
        a = drift.Activity(self.state)
        self.assertEqual(a.total(), 0.0)
        a.add(3.0)
        self.assertEqual(json.loads((self.state / drift.ACTIVITY_FILE).read_text())["active_s"], 3.0)


if __name__ == "__main__":
    unittest.main()
