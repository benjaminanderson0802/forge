"""Contract tests for the deadline-aware mutation runner (T1B1b)."""

import dataclasses
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import mutation


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MutationRunTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(mutation, "run_mutation", None)),
                        "core.mutation.run_mutation must be implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.argv = ["test-command", "--an-argument"]

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.encode("utf-8"))
        return path

    def sites(self, count=1, name="subject.py"):
        source = "".join(f"v{i} = a + b\n" for i in range(count))
        self.write(name, source)
        lines = set(range(1, count + 1))
        mutants = mutation.find_mutants(name, source, lines)
        self.assertEqual(len(mutants), count)
        return {name: lines}, mutants

    def invoke(self, changed, run, **overrides):
        options = dict(mutation_min=0.8, budget_s=100,
                       per_mutant_timeout_s=8, clock=self.clock, run=run)
        options.update(overrides)
        return mutation.run_mutation(self.root, changed, self.argv, **options)

    def assert_result(self, result, total, killed, survivors, not_run, passed):
        self.assertTrue(dataclasses.is_dataclass(result))
        self.assertIsInstance(result, mutation.MutationResult)
        self.assertEqual(result.total, total)
        self.assertEqual(result.killed, killed)
        self.assertEqual(result.survivors, survivors)
        self.assertEqual(result.survivor_ids(), [m.id for m in survivors])
        self.assertEqual(result.not_run, not_run)
        self.assertIs(result.complete, not bool(not_run))
        self.assertIs(result.passed, passed)
        denominator = killed + len(survivors)
        self.assertEqual(result.score, killed / denominator if denominator else 1.0)
        self.assertIsInstance(result.reason, str)
        self.assertTrue(result.reason.strip())
        self.assertEqual(len(result.reason.splitlines()), 1)

    def test_sorted_paths_missing_files_changed_lines_and_independent_sources(self):
        changed, first = self.sites(2, "a.py")
        _, last = self.sites(1, "z.py")
        self.write("ignored.py", "x = a + b\ny = a * b\n")
        originals = {p.name: p.read_bytes() for p in self.root.glob("*.py")}
        expected = first + last
        calls = []

        def run(argv, cwd, timeout, env):
            mutant = expected[len(calls)]
            self.assertEqual(argv, self.argv)
            self.assertEqual(cwd, self.root)
            self.assertEqual(timeout, 8)
            for name, original in originals.items():
                wanted = mutant.source.encode("utf-8") if name == mutant.file else original
                self.assertEqual((self.root / name).read_bytes(), wanted)
            calls.append(mutant.id)
            return 0, False

        result = self.invoke({"z.py": {1}, "missing.py": {1},
                              "ignored.py": {99}, **changed}, run)
        self.assertEqual(calls, [m.id for m in expected])
        self.assert_result(result, 3, 0, expected, [], False)
        for name, original in originals.items():
            self.assertEqual((self.root / name).read_bytes(), original)

    def test_survivor_evidence_is_ordered_json_safe_and_excludes_source(self):
        changed, mutants = self.sites(3)
        verdicts = iter([(0, False), (1, False), (0, False)])
        result = self.invoke(changed, lambda *args: next(verdicts))
        survivors = [mutants[0], mutants[2]]
        self.assert_result(result, 3, 1, survivors, [], False)
        expected = dict(total=3, killed=1, score=1 / 3, complete=True,
                        passed=False, reason=result.reason, not_run=[],
                        survivors=[dict(id=m.id, file=m.file, line=m.line,
                                        original=m.original, replacement=m.replacement)
                                   for m in survivors])
        self.assertEqual(result.as_dict(), expected)
        self.assertEqual(json.loads(json.dumps(result.as_dict())), expected)
        self.assertIn("surviv", result.reason.lower())

    def test_budget_truncates_third_run_and_excludes_it_from_score(self):
        changed, mutants = self.sites(3)
        timeouts = []

        def run(argv, cwd, timeout, env):
            timeouts.append(timeout)
            self.clock.advance(min(4, timeout))
            return (124, True) if timeout < 4 else (1, False)

        result = self.invoke(changed, run, budget_s=10)
        self.assertEqual(timeouts, [8, 6, 2])
        self.assert_result(result, 3, 2, [], [mutants[2].id], False)
        self.assertIn("incomplete", result.reason.lower())
        self.assertIn("budget", result.reason.lower())
        self.assertEqual(result.as_dict()["not_run"], [mutants[2].id])

    def test_nonpositive_budget_never_launches_mutants(self):
        changed, mutants = self.sites(3)
        for budget in (0, -1):
            with self.subTest(budget=budget):
                run = mock.Mock(side_effect=AssertionError("must not launch"))
                result = self.invoke(changed, run, budget_s=budget)
                run.assert_not_called()
                self.assert_result(result, 3, 0, [], [m.id for m in mutants], False)

    def test_deadline_starts_before_mutant_generation(self):
        changed, mutants = self.sites(2)
        find = mutation.find_mutants

        def slow_find(*args):
            self.clock.advance(11)
            return find(*args)

        run = mock.Mock()
        with mock.patch.object(mutation, "find_mutants", side_effect=slow_find):
            result = self.invoke(changed, run, budget_s=10)
        run.assert_not_called()
        self.assert_result(result, 2, 0, [], [m.id for m in mutants], False)

    def test_generation_time_reduces_first_timeout(self):
        changed, mutants = self.sites()
        find = mutation.find_mutants

        def slow_find(*args):
            self.clock.advance(7)
            return find(*args)

        run = mock.Mock(return_value=(1, False))
        with mock.patch.object(mutation, "find_mutants", side_effect=slow_find):
            result = self.invoke(changed, run, budget_s=10)
        self.assertEqual(run.call_args.args[2], 3)
        self.assert_result(result, 1, 1, [], [], True)

    def test_remaining_is_recomputed_after_preparing_mutant(self):
        changed, mutants = self.sites()
        mkdtemp = tempfile.mkdtemp

        def slow_mkdtemp(*args, **kwargs):
            directory = mkdtemp(*args, **kwargs)
            self.clock.advance(7)
            return directory

        run = mock.Mock(return_value=(1, False))
        with mock.patch.object(tempfile, "mkdtemp", side_effect=slow_mkdtemp):
            result = self.invoke(changed, run, budget_s=10)
        self.assertEqual(run.call_args.args[2], 3)
        self.assert_result(result, 1, 1, [], [], True)

    def test_full_per_mutant_timeout_is_killed_with_budget_left(self):
        changed, mutants = self.sites()

        def run(argv, cwd, timeout, env):
            self.assertEqual(timeout, 8)
            self.clock.advance(timeout)
            return 124, True

        result = self.invoke(changed, run, budget_s=10)
        self.assert_result(result, 1, 1, [], [], True)

    def test_shortened_timeout_is_not_a_kill_even_before_clock_reaches_deadline(self):
        changed, mutants = self.sites()
        run = mock.Mock(return_value=(124, True))
        result = self.invoke(changed, run, budget_s=2)
        self.assertEqual(run.call_args.args[2], 2)
        self.assert_result(result, 1, 0, [], [mutants[0].id], False)

    def test_final_run_exhausting_budget_is_incomplete_regardless_of_verdict(self):
        changed, mutants = self.sites()
        for exit_code, timed_out in ((0, False), (1, False), (124, True)):
            for elapsed in (8, 9):
                with self.subTest(exit_code=exit_code, timed_out=timed_out,
                                  elapsed=elapsed):
                    self.clock = FakeClock()

                    def run(argv, cwd, timeout, env):
                        self.assertEqual(timeout, 8)
                        self.clock.advance(elapsed)
                        return exit_code, timed_out

                    result = self.invoke(changed, run, budget_s=8)
                    self.assert_result(result, 1, 0, [], [mutants[0].id], False)

    def test_exhausted_run_and_all_later_mutants_are_not_run(self):
        changed, mutants = self.sites(3)
        calls = []

        def run(*args):
            calls.append(args)
            self.clock.advance(10)
            return 1, False

        result = self.invoke(changed, run, budget_s=10)
        self.assertEqual(len(calls), 1)
        self.assert_result(result, 3, 0, [], [m.id for m in mutants], False)

    def test_threshold_is_inclusive_and_configurable(self):
        changed, mutants = self.sites(5)
        for killed, minimum, passed in ((4, 0.8, True), (3, 0.8, False),
                                        (3, 0.6, True), (4, 0.81, False)):
            with self.subTest(killed=killed, minimum=minimum):
                verdicts = iter([(1, False)] * killed + [(0, False)] * (5 - killed))
                result = self.invoke(changed, lambda *args: next(verdicts),
                                     mutation_min=minimum)
                self.assert_result(result, 5, killed, mutants[killed:], [], passed)

    def test_zero_mutants_pass_even_with_no_time(self):
        self.write("subject.py", "def f(a):\n    return a\n")
        run = mock.Mock()
        result = self.invoke({"subject.py": {1, 2}, "missing.py": {1}}, run,
                             budget_s=0, mutation_min=1.0)
        run.assert_not_called()
        self.assert_result(result, 0, 0, [], [], True)
        self.assertEqual(result.reason, "no mutation sites on changed lines")

    def test_utf8_mutant_written_without_newline_translation_and_bytes_restored(self):
        original = "# café\r\ndef f(a, b):\r\n    return a + b\r\n"
        path = self.write("subject.py", original)
        # Use discovery's exact source as the write contract, independently of how
        # the runner reads CRLF input during generation.
        mutant = mutation.find_mutants("subject.py", original, {3})[0]

        def run(*args):
            self.assertEqual(path.read_bytes(), mutant.source.encode("utf-8"))
            return 1, False

        with mock.patch.object(mutation, "find_mutants", return_value=[mutant]):
            result = self.invoke({"subject.py": {3}}, run)
        self.assert_result(result, 1, 1, [], [], True)
        self.assertEqual(path.read_bytes(), original.encode("utf-8"))

    def test_run_exception_restores_original_bytes_and_removes_cache(self):
        original = "# café\r\ndef f(a, b):\r\n    return a + b\r\n"
        path = self.write("subject.py", original)
        caches = []
        failure = RuntimeError("test command failed to start")

        def run(argv, cwd, timeout, env):
            caches.append(Path(env["PYTHONPYCACHEPREFIX"]))
            self.assertNotEqual(path.read_bytes(), original.encode("utf-8"))
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.invoke({"subject.py": {3}}, run)
        self.assertIs(caught.exception, failure)
        self.assertEqual(path.read_bytes(), original.encode("utf-8"))
        self.assertEqual(len(caches), 1)
        self.assertFalse(caches[0].exists())

    def test_final_audit_detects_corruption_of_previously_restored_file(self):
        self.sites(1, "a.py")
        self.sites(1, "b.py")
        calls = []

        def run(*args):
            if calls:
                (self.root / "a.py").write_bytes(b"corrupted after restoration\n")
            calls.append(args)
            return 1, False

        with self.assertRaises(RuntimeError) as caught:
            self.invoke({"a.py": {1}, "b.py": {1}}, run)
        self.assertEqual(str(caught.exception), "mutation restore failed: a.py")
        self.assertEqual(len(calls), 2)

    def test_each_mutant_gets_fresh_cache_and_environment_copy(self):
        changed, mutants = self.sites(3)
        caches = []
        environments = []
        with mock.patch.dict(os.environ, {"MUTATION_TEST_SENTINEL": "keep-me",
                                          "PYTHONDONTWRITEBYTECODE": "0",
                                          "PYTHONPYCACHEPREFIX": "old-cache"}):
            original_env = dict(os.environ)

            def run(argv, cwd, timeout, env):
                self.assertIsNot(env, os.environ)
                expected_env = dict(original_env, PYTHONDONTWRITEBYTECODE="1",
                                    PYTHONPYCACHEPREFIX=env["PYTHONPYCACHEPREFIX"])
                self.assertEqual(env, expected_env)
                cache = Path(env["PYTHONPYCACHEPREFIX"])
                self.assertTrue(cache.is_dir())
                self.assertNotIn(cache, caches)
                self.assertTrue(all(not previous.exists() for previous in caches))
                (cache / "temporary-cache-entry").write_bytes(b"cache")
                caches.append(cache)
                environments.append(env)
                return 1, False

            result = self.invoke(changed, run)
            self.assertEqual(dict(os.environ), original_env)
        self.assert_result(result, 3, 3, [], [], True)
        self.assertEqual(len({id(env) for env in environments}), 3)
        self.assertTrue(all(not cache.exists() for cache in caches))

    def test_default_runner_invokes_exact_command_and_subprocess_options(self):
        changed, mutants = self.sites()
        with mock.patch.object(subprocess, "run",
                               return_value=subprocess.CompletedProcess(self.argv, 7)) as run:
            result = self.invoke(changed, None)
        self.assert_result(result, 1, 1, [], [], True)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args, (self.argv,))
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertIs(kwargs["capture_output"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 8)
        self.assertEqual(kwargs["creationflags"],
                         subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertFalse(Path(kwargs["env"]["PYTHONPYCACHEPREFIX"]).exists())

    def test_default_runner_maps_timeout_expired_to_timeout_verdict(self):
        changed, mutants = self.sites()
        for budget, killed, not_run in ((20, 1, []), (2, 0, [mutants[0].id])):
            with self.subTest(budget=budget):
                self.clock = FakeClock()

                def timeout(*args, **kwargs):
                    self.clock.advance(kwargs["timeout"])
                    raise subprocess.TimeoutExpired(self.argv, kwargs["timeout"])

                with mock.patch.object(subprocess, "run", side_effect=timeout):
                    result = self.invoke(changed, None, budget_s=budget)
                self.assert_result(result, 1, killed, [], not_run, not bool(not_run))
                self.assertEqual((self.root / "subject.py").read_bytes(), b"v0 = a + b\n")

    def test_real_unittest_kills_addition_but_weak_test_survives(self):
        source = "def f(a, b): return a + b\n"
        path = self.write("subject.py", source)
        mutants = mutation.find_mutants("subject.py", source, {1})
        self.assertEqual(len(mutants), 1)
        self.write("test_subject.py", "import unittest\nfrom subject import f\n"
                   "class Strong(unittest.TestCase):\n"
                   "    def test_sum(self):\n        self.assertEqual(f(2, 3), 5)\n"
                   "class Weak(unittest.TestCase):\n"
                   "    def test_call(self):\n        f(2, 3)\n")
        for case, killed, survivors, passed in (("Strong", 1, [], True),
                                                ("Weak", 0, mutants, False)):
            with self.subTest(case=case):
                result = mutation.run_mutation(
                    self.root, {"subject.py": {1}},
                    [sys.executable, "-m", "unittest", f"test_subject.{case}"],
                    mutation_min=1.0, budget_s=60, per_mutant_timeout_s=30)
                self.assert_result(result, 1, killed, survivors, [], passed)
                self.assertEqual(path.read_bytes(), source.encode("utf-8"))
                self.assertEqual([item["id"] for item in result.as_dict()["survivors"]],
                                 result.survivor_ids())

    def test_real_runner_ignores_valid_same_size_stale_bytecode(self):
        source = "def f(): return 42\n"
        path = self.write("subject.py", source)
        # An unchecked-hash cache remains valid even if the runner changes mtime.
        # Only bytecode isolation guarantees execution of the mutated source.
        py_compile.compile(str(path), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.write("check_value.py", "from subject import f\nassert f() == 42\n")
        result = mutation.run_mutation(
            self.root, {"subject.py": {1}}, [sys.executable, "check_value.py"],
            mutation_min=1.0, budget_s=60, per_mutant_timeout_s=30)
        self.assert_result(result, 1, 1, [], [], True)
        self.assertEqual(path.read_bytes(), source.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
