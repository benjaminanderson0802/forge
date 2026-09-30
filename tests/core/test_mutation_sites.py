"""Contract tests for changed-line, token-accurate mutation discovery."""

import dataclasses
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.mutation import Mutant, changed_lines, find_mutants


class ChangedLinesTests(unittest.TestCase):
    def test_multiple_files_hunk_sizes_and_ignored_files(self):
        diff = """diff --git a/core/alpha.py b/core/alpha.py
--- a/core/alpha.py
+++ b/core/alpha.py
@@ -2,2 +2,3 @@ optional context
-old
-old
+one
+two
+three
@@ -10 +11 @@
-old
+new
@@ -20,2 +21,0 @@
-removed
-removed
diff --git a/core/beta.py b/core/beta.py
--- a/core/beta.py
+++ b/core/beta.py
@@ -0,0 +1,2 @@
+one
+two
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
diff --git a/shrunk.py b/shrunk.py
--- a/shrunk.py
+++ b/shrunk.py
@@ -7,2 +6,0 @@
-one
-two
"""
        self.assertEqual(
            changed_lines(diff),
            {"core/alpha.py": {2, 3, 4, 11}, "core/beta.py": {1, 2}},
        )

    def test_empty_diff(self):
        self.assertEqual(changed_lines(""), {})

    def test_new_path_is_used_and_repeated_hunks_are_unioned(self):
        diff = (
            "--- a/old.py\n+++ b/pkg/new.py\n"
            "@@ -1 +4 @@\n-old\n+new\n"
            "@@ -2,2 +4,2 @@\n-old\n-old\n+new\n+new\n"
        )
        self.assertEqual(changed_lines(diff), {"pkg/new.py": {4, 5}})


class MutationSitesTests(unittest.TestCase):
    path = "pkg/example.py"

    def check_mutants(self, source, lines, expected):
        """Expected sites are (line, character column, kind, old, new)."""
        compile(source, self.path, "exec")
        mutants = find_mutants(self.path, source, lines)
        self.assertIsInstance(mutants, list)
        self.assertEqual(
            [(m.line, m.col, m.kind, m.original, m.replacement) for m in mutants],
            expected,
        )
        self.assertEqual(
            [(m.line, m.col, m.replacement) for m in mutants],
            sorted((m.line, m.col, m.replacement) for m in mutants),
        )
        self.assertEqual(len({m.id for m in mutants}), len(mutants))
        source_lines = source.splitlines(keepends=True)
        for mutant in mutants:
            with self.subTest(mutant=mutant.id):
                self.assertIsInstance(mutant, Mutant)
                self.assertEqual(mutant.file, self.path)
                self.assertIn(mutant.line, lines)
                self.assertEqual(
                    mutant.id,
                    f"{self.path}:{mutant.line}:{mutant.col}:{mutant.kind}:"
                    f"{mutant.original}->{mutant.replacement}",
                )
                offset = sum(map(len, source_lines[:mutant.line - 1])) + mutant.col
                self.assertEqual(source[offset:offset + len(mutant.original)], mutant.original)
                self.assertEqual(
                    mutant.source,
                    source[:offset] + mutant.replacement + source[offset + len(mutant.original):],
                )
                self.assertNotEqual(mutant.source, source)
                compile(mutant.source, self.path, "exec")
        return mutants

    def test_mutant_is_a_frozen_dataclass_with_exact_fields(self):
        self.assertTrue(dataclasses.is_dataclass(Mutant))
        self.assertEqual(
            [field.name for field in dataclasses.fields(Mutant)],
            ["id", "file", "line", "col", "kind", "original", "replacement", "source"],
        )
        mutant = self.check_mutants("x = a + b\n", {1}, [(1, 6, "binop", "+", "-")])[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            mutant.line = 99

    def test_multiline_binop_uses_operator_line(self):
        source = "total = (a +\n         b -\n         c)\n"
        self.check_mutants(source, {1}, [(1, 11, "binop", "+", "-")])
        self.check_mutants(source, {2}, [(2, 11, "binop", "-", "+")])
        self.check_mutants(source, {3}, [])

    def test_chained_comparisons_have_distinct_columns_and_ids(self):
        self.check_mutants(
            "x = a < b < c\n", {1},
            [(1, 6, "compare", "<", "<="), (1, 10, "compare", "<", "<=")],
        )

    def test_all_supported_binary_and_comparison_operators(self):
        families = {
            "binop": {"+": "-", "-": "+", "*": "/", "/": "*", "//": "*", "%": "*"},
            "compare": {
                "==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">",
                "is": "is not", "is not": "is", "in": "not in", "not in": "in",
            },
            "boolop": {"and": "or", "or": "and"},
        }
        for kind, replacements in families.items():
            for original, replacement in replacements.items():
                with self.subTest(kind=kind, original=original):
                    self.check_mutants(
                        f"x = a {original} b\n", {1},
                        [(1, 6, kind, original, replacement)],
                    )

    def test_not_in_is_one_site_on_first_token_line(self):
        source = "x = (a\n     not in b)\n"
        self.check_mutants(source, {1}, [])
        self.check_mutants(source, {2}, [(2, 5, "compare", "not in", "in")])

    def test_multiline_comparison_and_boolean_sites(self):
        self.check_mutants(
            "x = (a\n     < b\n     <= c)\n", {2, 3},
            [(2, 5, "compare", "<", "<="), (3, 5, "compare", "<=", "<")],
        )
        source = "x = (a\n     and b\n     and c)\n"
        self.check_mutants(source, {1}, [])
        self.check_mutants(source, {3}, [(3, 5, "boolop", "and", "or")])
        self.check_mutants(
            source, {2, 3},
            [(2, 5, "boolop", "and", "or"), (3, 5, "boolop", "and", "or")],
        )

    def test_unary_removes_not_and_following_whitespace_or_minus(self):
        self.check_mutants("x = not a\n", {1}, [(1, 4, "unary", "not ", "")])
        self.check_mutants("x = not \t a\n", {1}, [(1, 4, "unary", "not \t ", "")])
        self.check_mutants("x = -a\n", {1}, [(1, 4, "unary", "-", "")])
        self.check_mutants("x = (\n    not a)\n", {1}, [])
        self.check_mutants("x = (\n    -a)\n", {2}, [(2, 4, "unary", "-", "")])

    def test_augassign(self):
        for original, replacement in [("+=", "-="), ("-=", "+=")]:
            with self.subTest(original=original):
                self.check_mutants(
                    f"total {original} value\n", {1},
                    [(1, 6, "augassign", original, replacement)],
                )
        source = "items[\n    key] += value\n"
        self.check_mutants(source, {1}, [])
        self.check_mutants(source, {2}, [(2, 9, "augassign", "+=", "-=")])

    def test_constants_use_literal_text_and_decimal_replacement(self):
        for original, replacement in [
            ("True", "False"), ("False", "True"), ("0", "1"), ("42", "43"),
            ("0x2a", "43"), ("0b101", "6"), ("0o17", "16"), ("1_000", "1001"),
        ]:
            with self.subTest(original=original):
                self.check_mutants(
                    f"x = {original}\n", {1}, [(1, 4, "constant", original, replacement)],
                )
        self.check_mutants(
            "x = -42\n", {1},
            [(1, 4, "unary", "-", ""), (1, 5, "constant", "42", "43")],
        )
        self.check_mutants("x = (\n    42)\n", {1}, [])
        self.check_mutants("x = (\n    42)\n", {2}, [(2, 4, "constant", "42", "43")])

    def test_skips_strings_other_constants_and_all_fstring_contents(self):
        source = (
            '"""Docstring: 42 + True and not False."""\n'
            "plain = '42 + True'\n"
            "values = (None, 1.5, 2j, b'42', ...)\n"
            'text = f"{a + 42} {not flag} {a < b} {True} {value:{2 + 3}}"\n'
            "def function():\n"
            "    'another docstring'\n"
            "    return False\n"
        )
        self.check_mutants(source, set(range(1, 8)), [(7, 11, "constant", "False", "True")])

    def test_unicode_columns_are_characters_for_every_site_kind(self):
        for tail, kind, original, replacement in [
            ("é + b", "binop", "+", "-"), ("é < b", "compare", "<", "<="),
            ("é and b", "boolop", "and", "or"), ("not é", "unary", "not ", ""),
            ("-é", "unary", "-", ""), ("42", "constant", "42", "43"),
        ]:
            source = "résultat = " + tail + "\n"
            with self.subTest(kind=kind, original=original):
                self.check_mutants(source, {1}, [(1, source.index(original), kind, original, replacement)])
        self.check_mutants("résultat += valeur\n", {1}, [(1, 9, "augassign", "+=", "-=")])

    def test_parentheses_comments_and_line_endings_are_preserved(self):
        source = "x = ((a)  # + not in 42\r\n     + (b))  # tail\r\n"
        self.check_mutants(source, {1}, [])
        self.check_mutants(source, {2}, [(2, 5, "binop", "+", "-")])
        self.check_mutants("x = a  +\tb  # keep spacing", {1}, [(1, 7, "binop", "+", "-")])

    def test_many_operators_on_one_line_are_ordered_and_unique(self):
        source = "x = (a + b - c) < d < e and not flag or -n; count += 42\n"
        specs = [
            ("+", "binop", "-"), ("-", "binop", "+"),
            ("<", "compare", "<="), ("<", "compare", "<="),
            ("and", "boolop", "or"), ("not ", "unary", ""),
            ("or", "boolop", "and"), ("-", "unary", ""),
            ("+=", "augassign", "-="), ("42", "constant", "43"),
        ]
        expected = []
        start = 0
        for original, kind, replacement in specs:
            col = source.index(original, start)
            expected.append((1, col, kind, original, replacement))
            start = col + len(original)
        self.check_mutants(source, {1}, expected)

    def test_unsupported_operators_are_skipped(self):
        self.check_mutants(
            "x = (a ** b, a @ b, a << b, a >> b, a & b, a | b, a ^ b, ~a, +b)\nx *= y\n",
            {1, 2}, [],
        )

    def test_unchanged_lines_and_empty_selection_have_no_mutants(self):
        source = "x = a + 42\n# unchanged code above\ny = True\n"
        self.check_mutants(source, set(), [])
        self.check_mutants(source, {2, 99}, [])

    def test_invalid_source_returns_empty_list(self):
        for source in ["x = (\n", "x = a +\n", "def broken(:\n    return True\n"]:
            with self.subTest(source=source):
                self.assertEqual(find_mutants(self.path, source, {1, 2}), [])


if __name__ == "__main__":
    unittest.main()
