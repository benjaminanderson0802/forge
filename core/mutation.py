"""Mutation-testing judge: find mutants on changed lines (layer-1 design 3.4).

Sites are located by the operator or constant TOKEN itself, never by the
line of the enclosing expression, so only code on changed lines is mutated.
"""

import ast
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Mutant:
    id: str
    file: str
    line: int
    col: int
    kind: str
    original: str
    replacement: str
    source: str


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff_text: str) -> dict[str, set[int]]:
    """New-side line numbers per .py file in a `git diff -U0` text."""
    result: dict[str, set[int]] = {}
    current = None
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target.startswith("b/") and target.endswith(".py"):
                current = target[2:]
            else:
                current = None
            continue
        if raw.startswith("diff --git "):
            current = None
            continue
        match = _HUNK.match(raw)
        if match and current is not None:
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            if count:
                result.setdefault(current, set()).update(range(start, start + count))
    return result


_BINOPS = {ast.Add: "-", ast.Sub: "+", ast.Mult: "/", ast.Div: "*",
           ast.FloorDiv: "*", ast.Mod: "*"}
_COMPARES = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: "<=", ast.LtE: "<",
             ast.Gt: ">=", ast.GtE: ">", ast.Is: "is not", ast.IsNot: "is",
             ast.In: "not in", ast.NotIn: "in"}
_AUGASSIGNS = {ast.Add: "-=", ast.Sub: "+="}
_SKIP_TOKENS = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                tokenize.INDENT, tokenize.DEDENT}


class _Locator:
    """Maps ast byte positions and tokenize positions to absolute offsets."""

    def __init__(self, source: str):
        self.source = source
        self.lines = source.split("\n")
        self.starts = []
        offset = 0
        for text in self.lines:
            self.starts.append(offset)
            offset += len(text) + 1
        self.tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in _SKIP_TOKENS or tok.type == tokenize.ENDMARKER:
                continue
            if tok.type == tokenize.OP and tok.string in "()":
                continue
            self.tokens.append((self.abs(*tok.start), tok))

    def abs(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col

    def ast_abs(self, line: int, byte_col: int) -> int:
        text = self.lines[line - 1].encode("utf-8")[:byte_col].decode("utf-8")
        return self.starts[line - 1] + len(text)

    def node_start(self, node) -> int:
        return self.ast_abs(node.lineno, node.col_offset)

    def node_end(self, node) -> int:
        return self.ast_abs(node.end_lineno, node.end_col_offset)

    def between(self, start: int, end: int):
        return [tok for pos, tok in self.tokens if start <= pos < end]

    def at(self, start: int):
        for pos, tok in self.tokens:
            if pos >= start:
                return tok
        return None


def _fstring_nodes(tree) -> set[int]:
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inside.update(id(child) for child in ast.walk(node))
    return inside


def find_mutants(path: str, source: str, lines: set[int]) -> list[Mutant]:
    """Every mutant whose mutated token lies on one of `lines`, in source order."""
    try:
        tree = ast.parse(source)
        loc = _Locator(source)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return []
    skip = _fstring_nodes(tree)
    sites = []  # (line, col, kind, abs_start, original, replacement)

    def add(tok_start, tok_end_abs, kind, replacement):
        line, col = tok_start
        if line not in lines:
            return
        start = loc.abs(line, col)
        sites.append((line, col, kind, start, source[start:tok_end_abs], replacement))

    def add_token(tok, kind, replacement):
        if tok is not None:
            add(tok.start, loc.abs(*tok.end), kind, replacement)

    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            toks = loc.between(loc.node_end(node.left), loc.node_start(node.right))
            ops = [t for t in toks if t.type == tokenize.OP]
            if ops:
                add_token(ops[0], "binop", _BINOPS[type(node.op)])
        elif isinstance(node, ast.Compare):
            prev = node.left
            for op, right in zip(node.ops, node.comparators):
                toks = loc.between(loc.node_end(prev), loc.node_start(right))
                count = 2 if isinstance(op, (ast.NotIn, ast.IsNot)) else 1
                if type(op) in _COMPARES and len(toks) >= count:
                    add(toks[0].start, loc.abs(*toks[count - 1].end),
                        "compare", _COMPARES[type(op)])
                prev = right
        elif isinstance(node, ast.BoolOp):
            word = "and" if isinstance(node.op, ast.And) else "or"
            for left, right in zip(node.values, node.values[1:]):
                toks = loc.between(loc.node_end(left), loc.node_start(right))
                words = [t for t in toks if t.type == tokenize.NAME and t.string == word]
                if words:
                    add_token(words[0], "boolop", "or" if word == "and" else "and")
        elif isinstance(node, ast.UnaryOp):
            tok = loc.at(loc.node_start(node))
            if tok is None:
                continue
            if isinstance(node.op, ast.Not) and tok.string == "not":
                end = loc.abs(*tok.end)
                spaces = re.match(r"[ \t]*", source[end:]).group(0)
                add(tok.start, end + len(spaces), "unary", "")
            elif isinstance(node.op, ast.USub) and tok.string == "-":
                add_token(tok, "unary", "")
        elif isinstance(node, ast.AugAssign) and type(node.op) in _AUGASSIGNS:
            toks = loc.between(loc.node_end(node.target), loc.node_start(node.value))
            ops = [t for t in toks if t.type == tokenize.OP]
            if ops:
                add_token(ops[0], "augassign", _AUGASSIGNS[type(node.op)])
        elif isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                replacement = "False" if value else "True"
            elif isinstance(value, int):
                replacement = str(value + 1)
            else:
                continue
            start = loc.node_start(node)
            line = node.lineno
            col = start - loc.starts[line - 1]
            add((line, col), loc.node_end(node), "constant", replacement)

    sites.sort(key=lambda s: (s[0], s[1], s[5]))
    mutants = []
    seen: dict[str, int] = {}
    for line, col, kind, start, original, replacement in sites:
        mutated = source[:start] + replacement + source[start + len(original):]
        if mutated == source:
            continue
        try:
            compile(mutated, path, "exec")
        except (SyntaxError, ValueError):
            continue
        mutant_id = f"{path}:{line}:{col}:{kind}:{original}->{replacement}"
        seen[mutant_id] = seen.get(mutant_id, 0) + 1
        if seen[mutant_id] > 1:
            mutant_id = f"{mutant_id}#{seen[mutant_id]}"
        mutants.append(Mutant(mutant_id, path, line, col, kind, original,
                              replacement, mutated))
    assert len({m.id for m in mutants}) == len(mutants)
    return mutants


@dataclass
class MutationResult:
    total: int
    killed: int
    survivors: list[Mutant] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    complete: bool = True
    score: float = 1.0
    passed: bool = True
    reason: str = ""
    sampled: int | None = None  # R67: how many mutants ran (None: all of total)

    def survivor_ids(self) -> list[str]:
        return [m.id for m in self.survivors]

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "killed": self.killed,
            "score": self.score,
            "complete": self.complete,
            "passed": self.passed,
            "reason": self.reason,
            "sampled": self.total if self.sampled is None else self.sampled,
            "not_run": list(self.not_run),
            "survivors": [{"id": m.id, "file": m.file, "line": m.line,
                           "original": m.original, "replacement": m.replacement}
                          for m in self.survivors],
        }


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the test process and everything it started (same approach as agents.launch)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _default_run(argv: list[str], root: Path, timeout: float,
                 env: dict) -> tuple[int, bool]:
    """Run the tests once; on timeout kill the whole process tree.

    Output is discarded to DEVNULL rather than captured through pipes: a
    grandchild that inherited a pipe would keep it open after the test process
    dies, and waiting to drain it (what subprocess.run does on Windows after a
    timeout) would hang for as long as that orphan lives. Only the test
    process itself is waited on.
    """
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(list(argv), cwd=root, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, **kw)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return 124, True
    if os.name != "nt":
        # a finished test run must not leave helpers behind to touch the tree
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return code, False


def run_mutation(root: Path, changed: dict[str, set[int]], test_argv: list[str], *,
                 mutation_min: float, budget_s: float, per_mutant_timeout_s: float,
                 clock: Callable[[], float] = time.monotonic,
                 run: Callable[[list[str], Path, float, dict], tuple[int, bool]]
                 | None = None, max_mutants: int | None = None) -> MutationResult:
    """Run each mutant's tests under one hard total budget (layer-1 design 3.4).

    A mutant only gets a verdict if its run finished inside the budget; anything
    cut short by the deadline is reported in `not_run`, so an out-of-budget
    run is never complete.
    """
    deadline = clock() + budget_s  # taken once, before generation
    runner = _default_run if run is None else run
    root = Path(root)
    originals: dict[str, bytes] = {}
    mutants: list[Mutant] = []
    for rel in sorted(changed):
        target = root / rel
        try:
            raw = target.read_bytes()
            text = raw.decode("utf-8")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError,
                UnicodeDecodeError):
            continue
        found = find_mutants(rel, text, set(changed[rel]))
        if found:
            originals[rel] = raw
            mutants.extend(found)

    generated = len(mutants)
    if max_mutants is not None and max_mutants > 0 and generated > max_mutants:  # R67: deterministic sample
        ordered = sorted(mutants, key=lambda m: m.id)
        step = generated / max_mutants
        mutants = [ordered[int(i * step)] for i in range(max_mutants)]

    killed = 0
    survivors: list[Mutant] = []
    not_run: list[str] = []
    for index, mutant in enumerate(mutants):
        remaining = deadline - clock()
        if remaining <= 0:
            not_run.extend(m.id for m in mutants[index:])
            break
        target = root / mutant.file
        cache = tempfile.mkdtemp(prefix="forge-mut-pyc-")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = cache
        launched = False
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(mutant.source)
            # setup takes time too: recompute right before launching
            remaining = deadline - clock()
            if remaining > 0:
                timeout = min(per_mutant_timeout_s, remaining)
                launched = True
                code, timed_out = runner(list(test_argv), root, timeout, env)
        finally:
            target.write_bytes(originals[mutant.file])
            shutil.rmtree(cache, ignore_errors=True)
        if not launched:
            not_run.extend(m.id for m in mutants[index:])
            break
        overran = clock() >= deadline
        if overran or (timed_out and timeout < per_mutant_timeout_s):
            not_run.append(mutant.id)
        elif timed_out or code != 0:
            killed += 1
        else:
            survivors.append(mutant)

    for rel, raw in originals.items():
        try:
            same = (root / rel).read_bytes() == raw
        except OSError:
            same = False
        if not same:
            raise RuntimeError(f"mutation restore failed: {rel}")

    judged = killed + len(survivors)
    score = killed / judged if judged else 1.0
    complete = not not_run
    passed = complete and score >= mutation_min
    if not mutants:
        reason = "no mutation sites on changed lines"
    elif not complete:
        reason = (f"incomplete: budget ran out, {len(not_run)} of {len(mutants)} "
                  f"mutants not run")
    else:
        sign = ">=" if score >= mutation_min else "<"
        plural = "survivor" if len(survivors) == 1 else "survivors"
        reason = (f"killed {killed} of {judged} ({score:.2f} {sign} "
                  f"{mutation_min:.2f}); {len(survivors)} {plural}")
    if len(mutants) < generated:
        reason += f" (sampled {len(mutants)} of {generated})"
    return MutationResult(total=generated, killed=killed, survivors=survivors,
                          not_run=not_run, complete=complete, score=score,
                          passed=passed, reason=reason, sampled=len(mutants))
