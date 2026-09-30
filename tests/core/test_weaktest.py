"""Tests for core.weaktest: empty implementation, R4 real failing run, stub targets."""

import ast
import asyncio
from pathlib import Path
import sys
import textwrap
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.weaktest import empty_implementation, real_failing_run, stub_targets


SAMPLE = textwrap.dedent(
    '''\
    """Module docstring."""
    import functools
    from os import path as _path

    LIMIT = 42  # the limit
    NAMES = ["a", "b"]


    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs) + 1
        return wrapper


    class Thing:
        """A thing."""

        size = 3

        def method(self, x):
            """Doubles x."""
            y = x * 2
            return y

        class Inner:
            flag = True

            def inner_method(self):
                return "inner"


    @deco
    def decorated(a, b=LIMIT):
        total = a + b
        for i in range(3):
            total += i
        return total


    async def fetch(n):
        await asyncio.sleep(0)
        return n


    def one_line(): return 1


    def outer(v):
        def nested_helper(w):
            return w * 10
        return nested_helper(v)


    def gen():
        yield 1
        yield 2


    def only_doc():
        """Only a docstring."""


    print_me = LIMIT + 1
    '''
)


def load(source, name="stubbed"):
    code = compile(source, name, "exec")
    module = types.ModuleType(name)
    module.__dict__["asyncio"] = asyncio
    exec(code, module.__dict__)
    return module


def function_nodes(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


class EmptyImplementationTests(unittest.TestCase):
    def setUp(self):
        self.original = load(SAMPLE, "original")
        self.result = empty_implementation(SAMPLE)
        self.stubbed = load(self.result)

    def test_sanity_original_behaves(self):
        self.assertEqual(self.original.one_line(), 1)
        self.assertEqual(self.original.decorated(1, 2), 7)

    def test_result_compiles_and_every_function_returns_none(self):
        compile(self.result, "stubbed", "exec")
        m = self.stubbed
        self.assertIsNone(m.one_line())
        self.assertIsNone(m.outer(3))
        self.assertIsNone(m.gen())
        self.assertIsNone(m.only_doc())
        self.assertIsNone(m.Thing().method(5))
        self.assertIsNone(m.Thing.Inner().inner_method())
        self.assertIsNone(asyncio.run(m.fetch(9)))
        # deco is stubbed too, so @deco turns `decorated` into None.
        self.assertIsNone(m.deco(lambda: 1))
        self.assertIsNone(m.decorated)

    def test_everything_outside_bodies_unchanged(self):
        for line in [
            '"""Module docstring."""',
            "import functools",
            "from os import path as _path",
            "LIMIT = 42  # the limit",
            'NAMES = ["a", "b"]',
            "class Thing:",
            '    """A thing."""',
            "    size = 3",
            "    class Inner:",
            "        flag = True",
            "@deco",
            "def decorated(a, b=LIMIT):",
            "async def fetch(n):",
            "def outer(v):",
            "print_me = LIMIT + 1",
        ]:
            self.assertIn(line + "\n", self.result, line)
        m = self.stubbed
        self.assertEqual(m.LIMIT, 42)
        self.assertEqual(m.NAMES, ["a", "b"])
        self.assertEqual(m.Thing.size, 3)
        self.assertTrue(m.Thing.Inner.flag)
        self.assertEqual(m.print_me, 43)
        self.assertEqual(m.Thing.__doc__, "A thing.")
        self.assertEqual(m.__doc__, "Module docstring.")

    def test_docstrings_kept(self):
        self.assertEqual(self.stubbed.Thing.method.__doc__, "Doubles x.")
        self.assertEqual(self.stubbed.only_doc.__doc__, "Only a docstring.")
        self.assertIn('        """Doubles x."""\n        return None\n', self.result)

    def test_bodies_are_exactly_return_none(self):
        tree = ast.parse(self.result)
        names = sorted(f.name for f in function_nodes(tree))
        self.assertEqual(
            names,
            sorted(["deco", "method", "inner_method", "decorated", "fetch",
                    "one_line", "outer", "gen", "only_doc"]),
        )
        for fn in function_nodes(tree):
            body = fn.body
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            self.assertEqual(len(body), 1, fn.name)
            self.assertIsInstance(body[0], ast.Return, fn.name)
            self.assertIsInstance(body[0].value, ast.Constant, fn.name)
            self.assertIsNone(body[0].value.value, fn.name)

    def test_nested_def_gone_and_decorator_kept(self):
        self.assertNotIn("nested_helper", self.result)
        self.assertNotIn("wrapper", self.result)
        self.assertNotIn("functools.wraps", self.result)
        self.assertIn("@deco\ndef decorated(a, b=LIMIT):\n    return None\n", self.result)
        self.assertIn("def outer(v):\n    return None\n", self.result)

    def test_one_line_def(self):
        self.assertIn("def one_line(): return None\n", self.result)

    def test_indentation_of_method_body(self):
        self.assertIn("    def method(self, x):\n", self.result)
        self.assertIn("            return None\n", self.result)  # Inner.inner_method

    def test_non_ascii_before_body(self):
        src = (
            'def greet(name="héllo wörld ✓"): return "ünïcode " + name\n'
            "\n"
            "def other(x):\n"
            '    s = "日本語テキスト"; return s * x\n'
            "\n"
            'TAIL = "ç"\n'
        )
        out = empty_implementation(src)
        self.assertEqual(
            out,
            'def greet(name="héllo wörld ✓"): return None\n'
            "\n"
            "def other(x):\n"
            "    return None\n"
            "\n"
            'TAIL = "ç"\n',
        )
        m = load(out)
        self.assertIsNone(m.greet())
        self.assertEqual(m.TAIL, "ç")

    def test_non_ascii_docstring_then_code_on_same_line(self):
        src = 'def f():\n    "dôc ✓"; x = "é"; return x\n'
        out = empty_implementation(src)
        self.assertEqual(out, 'def f():\n    "dôc ✓"; return None\n')
        self.assertEqual(load(out).f.__doc__, "dôc ✓")

    def test_crlf_and_multiline_signature(self):
        src = "def f(\r\n    a,\r\n    b,\r\n):\r\n    return a + b\r\nX = 1\r\n"
        out = empty_implementation(src)
        self.assertEqual(out, "def f(\r\n    a,\r\n    b,\r\n):\r\n    return None\r\nX = 1\r\n")

    def test_function_inside_module_level_if(self):
        src = "import sys\nif sys:\n    def f():\n        return 5\nelse:\n    def f():\n        return 6\n"
        out = empty_implementation(src)
        self.assertEqual(out.count("return None"), 2)
        self.assertIsNone(load(out).f())

    def test_no_functions_unchanged(self):
        src = "X = 1\nclass A:\n    y = 2\n"
        self.assertEqual(empty_implementation(src), src)

    def test_syntax_error(self):
        with self.assertRaises(SyntaxError):
            empty_implementation("def broken(:\n    pass\n")


class RealFailingRunTests(unittest.TestCase):
    def test_real_failing_run(self):
        self.assertIsNone(real_failing_run(1, "Ran 2 tests\nFAILED", False))
        self.assertIsNone(real_failing_run(1, "....\nRan 1 test in 0.01s\n\nFAILED (errors=1)", False))
        self.assertIsNone(real_failing_run(2, "Ran 10 tests", False))

    def test_passed(self):
        self.assertEqual(real_failing_run(0, "Ran 1 test\nOK", False), "passed")

    def test_no_tests_ran(self):
        self.assertEqual(real_failing_run(1, "Ran 0 tests", False), "no tests ran")
        self.assertEqual(real_failing_run(1, "ImportError", False), "no tests ran")
        self.assertEqual(real_failing_run(0, "", False), "no tests ran")

    def test_timed_out_takes_priority(self):
        self.assertEqual(real_failing_run(124, "Ran 3 tests", True), "timed out")
        self.assertEqual(real_failing_run(0, "", True), "timed out")


class StubTargetsTests(unittest.TestCase):
    def test_mixed_entries(self):
        files_in_scope = [
            "core/pkg/*.py",          # glob
            "core/weaktest.py",       # literal, tracked
            "core/newmod.py",         # literal, untracked -> create
            "docs/notes.md",          # non-.py entry
            "tests/core/test_x.py",   # in scope but a test file
            "core/gen_*.py",          # glob, never created
        ]
        tracked = [
            "core/pkg/a.py",
            "core/pkg/b.py",
            "core/pkg/data.json",
            "core/weaktest.py",
            "core/other.py",
            "docs/notes.md",
            "tests/core/test_x.py",
        ]
        test_files = ["tests/core/test_x.py"]
        to_stub, to_create = stub_targets(files_in_scope, tracked, test_files)
        self.assertEqual(to_stub, ["core/pkg/a.py", "core/pkg/b.py", "core/weaktest.py"])
        self.assertEqual(to_create, ["core/newmod.py"])

    def test_path_normalisation(self):
        to_stub, to_create = stub_targets(
            [".\\core\\x.py", "./core/new.py", "core\\t.py", "./core/fresh_test.py"],
            ["core\\x.py", "./core/t.py"],
            ["core/t.py", ".\\core\\fresh_test.py"],
        )
        self.assertEqual(to_stub, ["core/x.py"])
        self.assertEqual(to_create, ["core/new.py"])

    def test_sorted_and_deduplicated(self):
        to_stub, to_create = stub_targets(
            ["z/*.py", "z/b.py", "y/new2.py", "y/new1.py", "y/new1.py"],
            ["z/b.py", "z/a.py"],
            [],
        )
        self.assertEqual(to_stub, ["z/a.py", "z/b.py"])
        self.assertEqual(to_create, ["y/new1.py", "y/new2.py"])

    def test_question_and_bracket_globs_not_created(self):
        to_stub, to_create = stub_targets(["m?.py", "n[0-9].py"], ["m1.py", "n5.py", "n.py"], [])
        self.assertEqual(to_stub, ["m1.py", "n5.py"])
        self.assertEqual(to_create, [])


if __name__ == "__main__":
    unittest.main()
