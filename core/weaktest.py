"""Plain-code pieces of the weak-test check (layer-1 design section 3 step 1, rule R4).

- empty_implementation: a module's source with every function body emptied.
- real_failing_run: R4, did a test command really run tests and fail?
- stub_targets: which files make up the empty implementation.
"""

from __future__ import annotations

import ast
import fnmatch
import re

_RAN_RE = re.compile(r"Ran ([1-9]\d*) tests?")
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _outermost_functions(node):
    """Yield FunctionDef/AsyncFunctionDef nodes not nested inside another function."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FUNC_TYPES):
            yield child
        else:
            yield from _outermost_functions(child)


def _is_docstring(stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _stmt_start(stmt):
    """(lineno, col_offset) where a statement's text begins, including decorators."""
    decorators = getattr(stmt, "decorator_list", None)
    if decorators:
        # The '@' sits at the statement's own indentation column.
        return decorators[0].lineno, stmt.col_offset
    return stmt.lineno, stmt.col_offset


def empty_implementation(source: str) -> str:
    """Return source with every outermost function body replaced by `return None`.

    Signatures, decorators and docstrings are kept; all text outside function
    bodies is preserved exactly. Raises SyntaxError if source does not parse.
    """
    tree = ast.parse(source)
    lines = _LINE_RE.findall(source)
    starts = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line)

    def offset(lineno: int, col_bytes: int) -> int:
        line = lines[lineno - 1]
        chars = len(line.encode("utf-8")[:col_bytes].decode("utf-8"))
        return starts[lineno - 1] + chars

    edits = []  # (start, end, replacement) as absolute character offsets
    for fn in _outermost_functions(tree):
        body = fn.body
        if _is_docstring(body[0]):
            doc = body[0]
            if len(body) > 1:
                first, last = body[1], body[-1]
                edits.append((
                    offset(*_stmt_start(first)),
                    offset(last.end_lineno, last.end_col_offset),
                    "return None",
                ))
            else:
                at = offset(doc.end_lineno, doc.end_col_offset)
                line = lines[doc.lineno - 1]
                indent = line[: offset(doc.lineno, doc.col_offset) - starts[doc.lineno - 1]]
                if indent.strip():
                    # Docstring shares a line with the header: `def f(): "doc"`.
                    text = "; return None"
                else:
                    text = _newline_of(lines[doc.end_lineno - 1]) + indent + "return None"
                edits.append((at, at, text))
        else:
            first, last = body[0], body[-1]
            edits.append((
                offset(*_stmt_start(first)),
                offset(last.end_lineno, last.end_col_offset),
                "return None",
            ))

    result = source
    for start, end, text in sorted(edits, reverse=True):
        result = result[:start] + text + result[end:]
    return result


def _newline_of(line: str) -> str:
    for ending in ("\r\n", "\r", "\n"):
        if line.endswith(ending):
            return ending
    return "\n"


def real_failing_run(code: int, output: str, timed_out: bool) -> str | None:
    """R4: None if the run is a real failing run, else the reason it is not."""
    if timed_out:
        return "timed out"
    if not _RAN_RE.search(output or ""):
        return "no tests ran"
    if code == 0:
        return "passed"
    return None


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_glob(entry: str) -> bool:
    return any(ch in entry for ch in "*?[")


def stub_targets(
    files_in_scope: list[str], tracked: list[str], test_files: list[str]
) -> tuple[list[str], list[str]]:
    """Return (to_stub, to_create) for the empty-implementation run, both sorted."""
    scope = [_norm(p) for p in files_in_scope]
    tracked_set = {_norm(p) for p in tracked}
    tests = {_norm(p) for p in test_files}

    to_stub = {
        path
        for path in tracked_set
        if path.endswith(".py")
        and path not in tests
        and any(fnmatch.fnmatch(path, entry) for entry in scope)
    }
    to_create = {
        entry
        for entry in scope
        if not _is_glob(entry)
        and entry.endswith(".py")
        and entry not in tracked_set
        and entry not in tests
    }
    return sorted(to_stub), sorted(to_create)
