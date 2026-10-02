"""R68: the mutation judge stops each mutant at its first failing test (-f) and gives a slow-test task enough
total budget to finish its sampled mutants."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from core import bootstrap
from core.bootstrap import MUTATION_BUDGET_MAX_S, Conductor
from core.mutation import MutationResult, run_mutation

TASK = {"test_cmd": "python -m unittest tests/core/test_example.py", "test_files": ["tests/core/test_example.py"]}


def conductor(limits):
    c = Conductor.__new__(Conductor)  # only limits are needed; avoid constructor side effects and git
    c.limits = limits
    return c


def call(limits, baseline=2.0):
    with patch("core.bootstrap.run_mutation", return_value=MutationResult(total=0, killed=0)) as runner:
        conductor(limits)._run_mutation(Path("unused"), {"m.py": {1}}, TASK, timeout=30.0, baseline=baseline)
    return runner.call_args


class MutationBudgetTests(unittest.TestCase):
    def test_default_budget_is_the_test_timeout_for_fast_tests(self):
        self.assertEqual(call({}, baseline=2.0).kwargs["budget_s"], 600.0)

    def test_default_budget_grows_for_slow_tests(self):
        # 0.6 * 25 mutants * 60 s = 900 s, above the 600 s floor
        self.assertEqual(call({}, baseline=60.0).kwargs["budget_s"], 900.0)

    def test_default_budget_is_capped(self):
        self.assertEqual(call({}, baseline=10_000.0).kwargs["budget_s"], float(MUTATION_BUDGET_MAX_S))

    def test_budget_follows_the_sample_size(self):
        self.assertEqual(call({"mutation_max_mutants": 10}, baseline=100.0).kwargs["budget_s"], 600.0)

    def test_explicit_budget_is_used_as_is(self):
        self.assertEqual(call({"mutation_budget_s": 100}, baseline=500.0).kwargs["budget_s"], 100.0)
        self.assertEqual(call({"mutation_budget_s": 5000}, baseline=1.0).kwargs["budget_s"], 5000.0)

    def test_floor_is_the_configured_test_timeout(self):
        self.assertEqual(call({"test_timeout_s": 900}, baseline=1.0).kwargs["budget_s"], 900.0)


class FailfastTests(unittest.TestCase):
    def test_mutant_tests_run_with_failfast(self):
        argv = call({}).args[2]
        self.assertEqual(argv[1:], ["-m", "unittest", "-f", "tests/core/test_example.py"])


class FailfastVerdictTests(unittest.TestCase):
    """A real run: failfast kills the same mutants as a full run, and survivors are still survivors."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef neg(a):\n    return -a\n", encoding="utf-8")
        (self.root / "test_calc.py").write_text(textwrap.dedent('''
            import unittest
            import calc

            class T(unittest.TestCase):
                def test_a_add(self):
                    self.assertEqual(calc.add(1, 2), 3)

                def test_b_add_again(self):
                    self.assertEqual(calc.add(2, 2), 4)

                def test_c_neg(self):
                    self.assertEqual(calc.neg(0), 0)  # does not distinguish -a from a

            if __name__ == "__main__":
                unittest.main()
        '''), encoding="utf-8")

    def run_it(self, *flags):
        return run_mutation(self.root, {"calc.py": {2, 5}},
                            [sys.executable, "-m", "unittest", *flags, "test_calc"],
                            mutation_min=0.0, budget_s=120.0, per_mutant_timeout_s=60.0)

    def test_failfast_gives_the_same_verdict_as_a_full_run(self):
        full = self.run_it()
        fast = self.run_it("-f")
        self.assertEqual(fast.killed, full.killed)
        self.assertEqual(fast.survivor_ids(), full.survivor_ids())
        self.assertEqual((fast.total, fast.complete), (full.total, full.complete))
        self.assertGreaterEqual(fast.killed, 1)
        self.assertGreaterEqual(len(fast.survivors), 1)  # the neg mutant survives either way


if __name__ == "__main__":
    unittest.main()
