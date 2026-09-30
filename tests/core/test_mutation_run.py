"""Contract tests for the deadline-aware mutation runner (T1B1b)."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.mutation import MutationResult, find_mutants, run_mutation


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


class FakeRun:
    """Records every call and what the mutated file looked like during it."""

    def __init__(self, clock, results, watch=None, cost=0.0, hook=None):
        self.clock = clock
        self.results = list(results)
        self.watch = watch
        self.cost = cost
        self.hook = hook
        self.calls = []

    def __call__(self, argv, root, timeout, env):
        seen = None
        if self.watch is not None:
            seen = (root / self.watch).read_bytes()
        pycache = env.get("PYTHONPYCACHEPREFIX")
        self.calls.append({
            "argv": list(argv), "root": root, "timeout": timeout,
            "env": dict(env), "seen": seen,
            "pycache_exists": bool(pycache) and Path(pycache).is_dir(),
        })
        if self.hook is not None:
            self.hook(len(self.calls), root)
        result = self.results.pop(0)
        cost = result[2] if len(result) > 2 else self.cost
        self.clock.now += cost
        return result[0], result[1]


def lines_source(count):
    return "".join(f"v{i} = {i + 10}\n" for i in range(count))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def go(self, changed, run, **kw):
        opts = dict(mutation_min=0.8, budget_s=100.0, per_mutant_timeout_s=10.0)
        opts.update(kw)
        return run_mutation(self.root, changed, ["python", "-m", "unittest", "t"],
                            clock=self.clock, run=run, **opts)


class VerdictTests(Base):
    def test_exit_codes_map_to_killed_and_survivors_and_source_is_written(self):
        src = lines_source(3)
        self.write("pkg/m.py", src)
        expected = find_mutants("pkg/m.py", src, {1, 2, 3})
        self.assertEqual(len(expected), 3)
        run = FakeRun(self.clock, [(1, False), (0, False), (2, False)],
                      watch="pkg/m.py", cost=1.0)
        res = self.go({"pkg/m.py": {1, 2, 3}}, run)
        self.assertIsInstance(res, MutationResult)
        self.assertEqual(res.total, 3)
        self.assertEqual(res.killed, 2)
        self.assertEqual([m.id for m in res.survivors], [expected[1].id])
        self.assertEqual(res.survivor_ids(), [expected[1].id])
        self.assertEqual(res.not_run, [])
        self.assertTrue(res.complete)
        self.assertAlmostEqual(res.score, 2 / 3)
        self.assertFalse(res.passed)
        self.assertIn("killed 2 of 3", res.reason)
        self.assertIn("1 survivor", res.reason)
        # each run saw its own mutant on disk, and the argv/root were passed as-is
        for call, mutant in zip(run.calls, expected):
            self.assertEqual(call["seen"], mutant.source.encode("utf-8"))
            self.assertEqual(call["argv"], ["python", "-m", "unittest", "t"])
            self.assertEqual(call["root"], self.root)
        self.assertEqual((self.root / "pkg/m.py").read_bytes(), src.encode("utf-8"))

    def test_full_per_mutant_timeout_counts_as_killed(self):
        self.write("m.py", lines_source(1))
        run = FakeRun(self.clock, [(124, True, 10.0)])
        res = self.go({"m.py": {1}}, run, budget_s=100.0, per_mutant_timeout_s=10.0)
        self.assertEqual(run.calls[0]["timeout"], 10.0)
        self.assertEqual(res.killed, 1)
        self.assertEqual(res.not_run, [])
        self.assertTrue(res.complete)
        self.assertTrue(res.passed)

    def test_threshold_uses_mutation_min(self):
        self.write("m.py", lines_source(5))
        changed = {"m.py": {1, 2, 3, 4, 5}}
        four = FakeRun(self.clock, [(1, False)] * 4 + [(0, False)], cost=1.0)
        res = self.go(changed, four, mutation_min=0.8)
        self.assertEqual((res.killed, len(res.survivors)), (4, 1))
        self.assertAlmostEqual(res.score, 0.8)
        self.assertTrue(res.passed)
        three = FakeRun(self.clock, [(1, False)] * 3 + [(0, False)] * 2, cost=1.0)
        res = self.go(changed, three, mutation_min=0.8)
        self.assertAlmostEqual(res.score, 0.6)
        self.assertFalse(res.passed)
        self.assertTrue(res.complete)
        self.assertEqual(res.reason, "killed 3 of 5 (0.60 < 0.80); 2 survivors")
        strict = FakeRun(self.clock, [(1, False)] * 4 + [(0, False)], cost=1.0)
        self.assertFalse(self.go(changed, strict, mutation_min=0.9).passed)

    def test_zero_mutants_passes(self):
        self.write("m.py", "def f(a):\n    return a\n")
        run = FakeRun(self.clock, [])
        res = self.go({"m.py": {1, 2}, "missing.py": {1}}, run, mutation_min=1.0)
        self.assertEqual(run.calls, [])
        self.assertEqual((res.total, res.killed, res.survivors, res.not_run),
                         (0, 0, [], []))
        self.assertEqual(res.score, 1.0)
        self.assertTrue(res.complete)
        self.assertTrue(res.passed)
        self.assertEqual(res.reason, "no mutation sites on changed lines")

    def test_paths_sorted_and_missing_files_skipped(self):
        self.write("b.py", "x = 1\n")
        self.write("a.py", "y = 2\n")
        run = FakeRun(self.clock, [(1, False)] * 2, cost=1.0)
        res = self.go({"b.py": {1}, "zz_missing.py": {1}, "a.py": {1}}, run)
        self.assertEqual(res.total, 2)
        self.assertEqual(res.killed, 2)
        # order: a.py before b.py, checked via survivors
        run2 = FakeRun(self.clock, [(0, False)] * 2, cost=1.0)
        res2 = self.go({"b.py": {1}, "a.py": {1}}, run2)
        self.assertEqual([m.file for m in res2.survivors], ["a.py", "b.py"])
        self.assertEqual(res2.survivor_ids(), [m.id for m in res2.survivors])

    def test_crlf_file_restored_byte_for_byte(self):
        raw = b"a = 1\r\nb = 2\r\n"
        (self.root / "m.py").write_bytes(raw)
        run = FakeRun(self.clock, [(1, False)] * 2, cost=1.0, watch="m.py")
        res = self.go({"m.py": {1, 2}}, run)
        self.assertEqual(res.total, 2)
        self.assertEqual((self.root / "m.py").read_bytes(), raw)
        self.assertIn(b"a = 2\r\n", run.calls[0]["seen"])


class BudgetTests(Base):
    def test_third_mutant_cut_down_by_deadline_is_not_run(self):
        src = lines_source(3)
        self.write("m.py", src)
        ids = [m.id for m in find_mutants("m.py", src, {1, 2, 3})]
        run = FakeRun(self.clock, [(1, False, 4.0), (1, False, 4.0), (124, True, 2.0)])
        res = self.go({"m.py": {1, 2, 3}}, run, budget_s=10.0,
                      per_mutant_timeout_s=8.0, mutation_min=0.5)
        self.assertEqual([c["timeout"] for c in run.calls], [8.0, 6.0, 2.0])
        self.assertEqual(res.killed, 2)
        self.assertEqual(res.survivors, [])
        self.assertEqual(res.not_run, [ids[2]])
        self.assertEqual(res.score, 1.0)
        self.assertFalse(res.complete)
        self.assertFalse(res.passed)
        self.assertEqual(res.reason, "incomplete: budget ran out, 1 of 3 mutants not run")
        self.assertEqual((self.root / "m.py").read_bytes(), src.encode("utf-8"))

    def test_no_time_left_means_not_run_and_run_not_called(self):
        src = lines_source(3)
        self.write("m.py", src)
        ids = [m.id for m in find_mutants("m.py", src, {1, 2, 3})]
        # first mutant uses the whole budget (not timed out, but clock hits deadline)
        run = FakeRun(self.clock, [(1, False, 5.0)])
        res = self.go({"m.py": {1, 2, 3}}, run, budget_s=5.0, per_mutant_timeout_s=8.0)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(res.not_run, ids)
        self.assertEqual(res.killed, 0)
        self.assertFalse(res.complete)
        self.assertFalse(res.passed)
        self.assertEqual(res.reason, "incomplete: budget ran out, 3 of 3 mutants not run")

    def test_budget_already_spent_before_first_mutant(self):
        self.write("m.py", lines_source(2))
        run = FakeRun(self.clock, [])
        res = self.go({"m.py": {1, 2}}, run, budget_s=0.0)
        self.assertEqual(run.calls, [])
        self.assertEqual(len(res.not_run), 2)
        self.assertFalse(res.complete)

    def test_deadline_taken_once_at_the_start(self):
        self.write("m.py", lines_source(2))
        times = iter([0.0] + [50.0] * 20)

        def clock():
            return next(times)

        run = FakeRun(self.clock, [])
        res = run_mutation(self.root, {"m.py": {1, 2}}, ["x"], mutation_min=0.8,
                           budget_s=10.0, per_mutant_timeout_s=5.0,
                           clock=clock, run=run)
        self.assertEqual(run.calls, [])
        self.assertEqual(len(res.not_run), 2)
        self.assertFalse(res.passed)

    def test_final_run_overrunning_without_timeout_flag_is_incomplete(self):
        self.write("m.py", lines_source(2))
        # second run reports a clean kill, but the clock shows it ran past the deadline
        run = FakeRun(self.clock, [(1, False, 3.0), (1, False, 9.0)])
        res = self.go({"m.py": {1, 2}}, run, budget_s=10.0, per_mutant_timeout_s=8.0)
        self.assertEqual(res.killed, 1)
        self.assertEqual(len(res.not_run), 1)
        self.assertFalse(res.complete)
        self.assertFalse(res.passed)

    def test_survivor_reported_after_deadline_is_not_run(self):
        self.write("m.py", lines_source(1))
        run = FakeRun(self.clock, [(0, False, 12.0)])
        res = self.go({"m.py": {1}}, run, budget_s=10.0, per_mutant_timeout_s=10.0)
        self.assertEqual(res.survivors, [])
        self.assertEqual(len(res.not_run), 1)
        self.assertFalse(res.complete)

    def test_full_timeout_equal_to_remaining_budget_is_incomplete(self):
        self.write("m.py", lines_source(2))
        run = FakeRun(self.clock, [(1, False, 2.0), (124, True, 8.0)])
        res = self.go({"m.py": {1, 2}}, run, budget_s=10.0, per_mutant_timeout_s=8.0)
        self.assertEqual([c["timeout"] for c in run.calls], [8.0, 8.0])
        self.assertEqual(res.killed, 1)
        self.assertEqual(len(res.not_run), 1)
        self.assertFalse(res.complete)
        self.assertFalse(res.passed)

    def test_every_timeout_is_capped_by_remaining_time(self):
        self.write("m.py", lines_source(4))
        run = FakeRun(self.clock, [(1, False, 1.5)] * 4)
        self.go({"m.py": {1, 2, 3, 4}}, run, budget_s=7.0, per_mutant_timeout_s=5.0)
        self.assertEqual([c["timeout"] for c in run.calls], [5.0, 5.0, 4.0, 2.5])


class SafetyTests(Base):
    def test_file_restored_when_run_raises(self):
        src = lines_source(2)
        path = self.write("m.py", src)

        def boom(argv, root, timeout, env):
            self.assertNotEqual(path.read_bytes(), src.encode("utf-8"))
            raise ValueError("runner exploded")

        with self.assertRaises(ValueError):
            self.go({"m.py": {1, 2}}, boom)
        self.assertEqual(path.read_bytes(), src.encode("utf-8"))

    def test_restore_check_raises_when_a_touched_file_differs(self):
        self.write("a.py", "x = 1\n")
        other = self.write("b.py", "y = 2\n")

        def hook(n, root):
            if n == 2:  # while b.py is mutated, something scribbles on a.py
                (root / "a.py").write_bytes(b"x = 999\n")

        run = FakeRun(self.clock, [(1, False)] * 2, cost=1.0, hook=hook)
        with self.assertRaises(RuntimeError) as ctx:
            self.go({"a.py": {1}, "b.py": {1}}, run)
        self.assertEqual(str(ctx.exception), "mutation restore failed: a.py")
        self.assertEqual(other.read_bytes(), b"y = 2\n")

    def test_env_disables_stale_bytecode_per_mutant(self):
        self.write("m.py", lines_source(2))
        before = dict(os.environ)
        run = FakeRun(self.clock, [(1, False)] * 2, cost=1.0)
        self.go({"m.py": {1, 2}}, run)
        self.assertEqual(dict(os.environ), before)
        prefixes = []
        for call in run.calls:
            env = call["env"]
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertTrue(call["pycache_exists"])
            prefixes.append(env["PYTHONPYCACHEPREFIX"])
            self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
        self.assertEqual(len(set(prefixes)), 2)
        for prefix in prefixes:
            self.assertFalse(Path(prefix).exists())

    def test_as_dict_is_json_safe_without_source(self):
        src = lines_source(3)
        self.write("m.py", src)
        run = FakeRun(self.clock, [(0, False, 1.0), (1, False, 1.0), (124, True, 9.0)])
        res = self.go({"m.py": {1, 2, 3}}, run, budget_s=10.0, per_mutant_timeout_s=8.0)
        data = res.as_dict()
        text = json.dumps(data)
        self.assertEqual(set(data), {"total", "killed", "score", "complete", "passed",
                                     "reason", "not_run", "survivors"})
        self.assertEqual(len(data["survivors"]), 1)
        self.assertEqual(set(data["survivors"][0]),
                         {"id", "file", "line", "original", "replacement"})
        self.assertEqual(data["survivors"][0]["id"], res.survivor_ids()[0])
        self.assertEqual(data["not_run"], res.not_run)
        self.assertNotIn("source", text)
        self.assertNotIn("v1 = 11\\nv2", text)
        self.assertEqual(json.loads(text)["total"], 3)


class RealRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8", newline="")

    def argv(self):
        return [sys.executable, "-m", "unittest", "test_calc"]

    def test_strong_test_kills_plus_mutant(self):
        self.put("calc.py", "def f(a, b): return a + b\n")
        self.put("test_calc.py", "import unittest\nfrom calc import f\n\n"
                 "class T(unittest.TestCase):\n    def test_f(self):\n"
                 "        self.assertEqual(f(2, 3), 5)\n")
        res = run_mutation(self.root, {"calc.py": {1}}, self.argv(), mutation_min=0.8,
                           budget_s=120.0, per_mutant_timeout_s=60.0)
        self.assertEqual(res.total, 1)
        self.assertEqual(res.killed, 1)
        self.assertTrue(res.complete)
        self.assertTrue(res.passed)
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"),
                         "def f(a, b): return a + b\n")

    def test_weak_test_leaves_survivor(self):
        self.put("calc.py", "def f(a, b): return a + b\n")
        self.put("test_calc.py", "import unittest\nfrom calc import f\n\n"
                 "class T(unittest.TestCase):\n    def test_f(self):\n"
                 "        f(2, 3)\n")
        res = run_mutation(self.root, {"calc.py": {1}}, self.argv(), mutation_min=0.8,
                           budget_s=120.0, per_mutant_timeout_s=60.0)
        self.assertEqual(res.killed, 0)
        self.assertEqual(len(res.survivors), 1)
        sid = res.survivors[0].id
        self.assertIn("+->-", sid)
        self.assertEqual(res.survivor_ids(), [sid])
        self.assertEqual([s["id"] for s in res.as_dict()["survivors"]], [sid])
        self.assertTrue(res.complete)
        self.assertFalse(res.passed)

    def test_same_size_mutant_not_masked_by_stale_bytecode(self):
        import subprocess
        self.put("calc.py", "def g():\n    return 42\n")
        self.put("test_calc.py", "import unittest\nfrom calc import g\n\n"
                 "class T(unittest.TestCase):\n    def test_g(self):\n"
                 "        self.assertEqual(g(), 42)\n")
        # warm __pycache__ with the unmutated module (same size as the 43 mutant)
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        subprocess.run(self.argv(), cwd=self.root, capture_output=True, env=env,
                       stdin=subprocess.DEVNULL, timeout=60, check=True)
        self.assertTrue(any((self.root / "__pycache__").glob("calc*.pyc")))
        res = run_mutation(self.root, {"calc.py": {2}}, self.argv(), mutation_min=1.0,
                           budget_s=120.0, per_mutant_timeout_s=60.0)
        self.assertEqual(res.total, 1)
        self.assertEqual(res.killed, 1, res.reason)
        self.assertTrue(res.passed)

    def test_infinite_loop_mutant_killed_by_full_timeout(self):
        self.put("calc.py", "def count():\n    i = 0\n    while i < 3:\n"
                 "        i += 1\n    return i\n")
        self.put("test_calc.py", "import unittest\nfrom calc import count\n\n"
                 "class T(unittest.TestCase):\n    def test_c(self):\n"
                 "        self.assertEqual(count(), 3)\n")
        res = run_mutation(self.root, {"calc.py": {4}}, self.argv(), mutation_min=1.0,
                           budget_s=120.0, per_mutant_timeout_s=4.0)
        kinds = sorted(m.kind for m in find_mutants(
            "calc.py", (self.root / "calc.py").read_text(encoding="utf-8"), {4}))
        self.assertEqual(kinds, ["augassign", "constant"])
        self.assertEqual(res.total, 2)
        self.assertEqual(res.killed, 2, res.reason)
        self.assertTrue(res.complete)
        self.assertTrue(res.passed)


if __name__ == "__main__":
    unittest.main()
