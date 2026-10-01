"""R67: deterministic, bounded mutation samples and conductor limit wiring."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.bootstrap import Conductor
from core.mutation import MutationResult, find_mutants, run_mutation


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class MutationSamplingTests(unittest.TestCase):
    # Thirteen constants, one per line. Lexical ID order starts at line 10,
    # not line 1. For max=5, floor(i * 13 / 5) selects indices 0, 2, 5, 7, 10.
    SAMPLE_IDS = [
        "m.py:10:4:constant:10->11",
        "m.py:12:4:constant:12->13",
        "m.py:2:4:constant:2->3",
        "m.py:4:4:constant:4->5",
        "m.py:7:4:constant:7->8",
    ]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def exercise(self, count, maximum, killed_ids=None, mutation_min=0.8):
        source = "".join(f"x = {n}\n" for n in range(1, count + 1))
        path = self.root / "m.py"
        path.write_bytes(source.encode("utf-8"))
        changed = {"m.py": set(range(1, count + 1))}
        mutants = find_mutants("m.py", source, changed["m.py"])
        self.assertEqual(len(mutants), count)
        by_source = {m.source: m.id for m in mutants}
        clock = FakeClock()
        seen = []

        def fake_run(argv, root, timeout, env):
            mutant_id = by_source[(root / "m.py").read_text(encoding="utf-8")]
            seen.append(mutant_id)
            clock.now += 1.0
            killed = killed_ids is None or mutant_id in killed_ids
            return (1 if killed else 0), False

        result = run_mutation(
            self.root, changed, ["python", "-m", "unittest", "t"],
            mutation_min=mutation_min, budget_s=100.0,
            per_mutant_timeout_s=10.0, max_mutants=maximum,
            run=fake_run, clock=clock,
        )
        self.assertIsInstance(result, MutationResult)
        self.assertEqual(path.read_bytes(), source.encode("utf-8"))
        return result, seen, [m.id for m in mutants]

    def test_large_set_selects_exact_evenly_spaced_ids_deterministically(self):
        first, first_ids, _ = self.exercise(13, 5)
        second, second_ids, _ = self.exercise(13, 5)
        self.assertEqual(first_ids, self.SAMPLE_IDS)
        self.assertEqual(second_ids, first_ids)
        for result, seen in ((first, first_ids), (second, second_ids)):
            self.assertEqual(len(seen), 5)
            self.assertEqual(result.total, 13)
            self.assertEqual(result.sampled, 5)
            self.assertEqual(result.as_dict()["sampled"], 5)
            self.assertIn("sampled 5 of 13", result.reason)

    def test_at_or_below_limit_runs_every_mutant(self):
        for count in (0, 1, 4, 5):
            with self.subTest(total=count):
                result, seen, generated = self.exercise(count, 5)
                self.assertCountEqual(seen, generated)
                self.assertEqual(len(seen), count)
                self.assertEqual(result.total, count)
                self.assertEqual(result.sampled, count)
                self.assertEqual(result.killed, count)
                self.assertEqual(result.score, 1.0)
                self.assertTrue(result.complete)
                self.assertTrue(result.passed)
                self.assertEqual(result.not_run, [])
                self.assertEqual(result.survivor_ids(), [])

    def test_small_set_keeps_survivor_score_and_threshold(self):
        killed = {"m.py:1:4:constant:1->2", "m.py:2:4:constant:2->3"}
        result, seen, generated = self.exercise(3, 5, killed_ids=killed)
        self.assertCountEqual(seen, generated)
        self.assertEqual((result.total, result.sampled, result.killed), (3, 3, 2))
        self.assertAlmostEqual(result.score, 2 / 3)
        self.assertFalse(result.passed)
        self.assertTrue(result.complete)
        self.assertEqual(result.not_run, [])
        self.assertEqual(result.survivor_ids(), ["m.py:3:4:constant:3->4"])

    def test_all_sampled_killed_passes_even_if_unsampled_would_survive(self):
        # The runner returns success (survival) for every ID outside the sample.
        result, seen, generated = self.exercise(
            13, 5, killed_ids=set(self.SAMPLE_IDS), mutation_min=1.0,
        )
        self.assertEqual(len(set(generated) - set(self.SAMPLE_IDS)), 8)
        self.assertEqual(seen, self.SAMPLE_IDS)
        self.assertEqual((result.total, result.sampled, result.killed), (13, 5, 5))
        self.assertEqual(result.score, 1.0)
        self.assertTrue(result.passed)
        self.assertTrue(result.complete)
        self.assertEqual(result.not_run, [])
        self.assertEqual(result.survivor_ids(), [])

    def test_sample_survivors_and_threshold_use_only_sample(self):
        for threshold, passed in ((0.6, True), (0.8, False)):
            with self.subTest(mutation_min=threshold):
                result, seen, _ = self.exercise(
                    13, 5, killed_ids=set(self.SAMPLE_IDS[:3]),
                    mutation_min=threshold,
                )
                self.assertEqual(seen, self.SAMPLE_IDS)
                self.assertEqual(result.killed, 3)
                self.assertAlmostEqual(result.score, 3 / 5)
                self.assertEqual(result.passed, passed)
                self.assertTrue(result.complete)
                self.assertEqual(result.not_run, [])
                self.assertEqual(result.survivor_ids(), self.SAMPLE_IDS[3:])


class ConductorMutationSamplingTests(unittest.TestCase):
    def check_limit(self, limits, expected):
        # This adapter only needs limits; avoid constructor side effects and git.
        conductor = Conductor.__new__(Conductor)
        conductor.limits = limits
        task = {
            "test_cmd": "python -m unittest tests/core/test_example.py",
            "test_files": ["tests/core/test_example.py"],
        }
        result = MutationResult(total=0, killed=0)
        with patch("core.bootstrap.run_mutation", return_value=result) as runner:
            actual = conductor._run_mutation(
                Path("unused"), {"m.py": {1}}, task, timeout=30.0, baseline=2.0,
            )
        self.assertIs(actual, result)
        runner.assert_called_once()
        self.assertIn("max_mutants", runner.call_args.kwargs)
        self.assertEqual(runner.call_args.kwargs["max_mutants"], expected)

    def test_conductor_passes_configured_max_mutants(self):
        self.check_limit({"mutation_max_mutants": 7}, 7)

    def test_conductor_defaults_max_mutants_to_25(self):
        self.check_limit({}, 25)


if __name__ == "__main__":
    unittest.main()
