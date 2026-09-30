"""Mutation-testing judge, part 1: find mutation sites on changed lines (layer-1 design 3.4).

changed_lines() reads `git diff -U0` output; find_mutants() builds one Mutant per operator or
constant token that sits on a changed line. A site is chosen by the line of the token itself,
never by the line where the enclosing expression starts. Standard library only, no side effects.
"""
import ast
import io
import re
import tokenize
from dataclasses import dataclass, replace


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


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OCTAL = re.compile(r"\\([0-7]{1,3})")
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
            "\\": "\\", '"': '"'}


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path such as "b/caf\\303\\251.py"."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            octal = _OCTAL.match(body, i)
            if octal:
                out.append(int(octal.group(1), 8) & 0xFF)
                i = octal.end()
                continue
            out.extend(_ESCAPES.get(body[i + 1], body[i + 1]).encode("utf-8"))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _new_path(header: str) -> str | None:
    """Path named by a '+++ ' header, or None for a deleted file."""
    path = header[4:].split("\t", 1)[0]
    if path.startswith('"'):
        path = _unquote(path)
    else:
        path = path.rstrip()
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith("b/") else path


def changed_lines(diff_text: str) -> dict[str, set[int]]:
    """New-side line numbers added by each .py file in a unified diff."""
    result: dict[str, set[int]] = {}
    current = None
    old_left = new_left = 0
    for raw in diff_text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if old_left > 0 or new_left > 0:
            # Hunk body: never read as a header, so '+++ x' or '--- x' content is safe.
            if line.startswith("\\"):
                continue
            if line.startswith("+"):
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            else:
                old_left -= 1
                new_left -= 1
            old_left, new_left = max(old_left, 0), max(new_left, 0)
            continue
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = _new_path(line)
            current = path if path is not None and path.endswith(".py") else None
        elif line.startswith("@@"):
            hunk = _HUNK.match(line)
            if not hunk:
                continue
            old_count = int(hunk.group(1)) if hunk.group(1) is not None else 1
            start = int(hunk.group(2))
            new_count = int(hunk.group(3)) if hunk.group(3) is not None else 1
            old_left, new_left = old_count, new_count
            if current is not None and new_count > 0:
                result.setdefault(current, set()).update(range(start, start + new_count))
    return {path: lines for path, lines in result.items() if lines}


_BINOPS = {ast.Add: "-", ast.Sub: "+", ast.Mult: "/", ast.Div: "*", ast.FloorDiv: "*",
           ast.Mod: "*"}
_BINOP_TEXT = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//",
               ast.Mod: "%"}
_COMPARES = {ast.Eq: (["=="], "!="), ast.NotEq: (["!="], "=="), ast.Lt: (["<"], "<="),
             ast.LtE: (["<="], "<"), ast.Gt: ([">"], ">="), ast.GtE: ([">="], ">"),
             ast.Is: (["is"], "is not"), ast.IsNot: (["is", "not"], "is"),
             ast.In: (["in"], "not in"), ast.NotIn: (["not", "in"], "in")}
_BOOLOPS = {ast.And: ("and", "or"), ast.Or: ("or", "and")}
_AUGASSIGNS = {ast.Add: ("+=", "-="), ast.Sub: ("-=", "+=")}
_SKIPPED_TOKENS = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                   tokenize.DEDENT, tokenize.ENDMARKER}
_TRAILING_SPACE = re.compile(r"(?:[ \t\f\r\n]|\\\r\n|\\\r|\\\n)*")


class _Source:
    """Line table and token index shared by every site lookup in one file."""

    def __init__(self, source: str):
        self.text = source
        self.starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", source)]
        self.lines = []
        for i, start in enumerate(self.starts):
            end = self.starts[i + 1] if i + 1 < len(self.starts) else len(source)
            self.lines.append(source[start:end].rstrip("\r\n"))
        self.by_row: dict[int, list[tokenize.TokenInfo]] = {}
        self.at: dict[tuple[int, int], tokenize.TokenInfo] = {}
        for tok in tokenize.generate_tokens(io.StringIO(source, newline="").readline):
            if tok.type in _SKIPPED_TOKENS:
                continue
            self.by_row.setdefault(tok.start[0], []).append(tok)
            self.at[tok.start] = tok

    def char_pos(self, lineno: int, byte_col: int) -> tuple[int, int]:
        """ast (line, UTF-8 byte offset) -> (line, character column)."""
        text = self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""
        return lineno, len(text.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))

    def start(self, node: ast.AST) -> tuple[int, int]:
        return self.char_pos(node.lineno, node.col_offset)

    def end(self, node: ast.AST) -> tuple[int, int]:
        return self.char_pos(node.end_lineno, node.end_col_offset)

    def between(self, after: tuple[int, int], before: tuple[int, int]):
        """Tokens starting in [after, before), parentheses excluded."""
        found = []
        for row in range(after[0], before[0] + 1):
            for tok in self.by_row.get(row, ()):
                if after <= tok.start < before and tok.string not in ("(", ")"):
                    found.append(tok)
        return found

    def offset(self, pos: tuple[int, int]) -> int:
        return self.starts[pos[0] - 1] + pos[1]


def _walk(node: ast.AST):
    """ast.walk that never enters an f-string."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.JoinedStr):
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def _sites(src: _Source, tree: ast.AST):
    """Yield (start_pos, kind, original_text, replacement) for every mutation site."""
    for node in _walk(tree):
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            toks = src.between(src.end(node.left), src.start(node.right))
            if len(toks) == 1 and toks[0].string == _BINOP_TEXT[type(node.op)]:
                yield toks[0].start, "binop", toks[0].string, _BINOPS[type(node.op)]
        elif isinstance(node, ast.Compare):
            left = node.left
            for op, right in zip(node.ops, node.comparators):
                words, replacement = _COMPARES[type(op)]
                toks = src.between(src.end(left), src.start(right))
                if [t.string for t in toks] == words:
                    original = src.text[src.offset(toks[0].start):src.offset(toks[-1].end)]
                    yield toks[0].start, "compare", original, replacement
                left = right
        elif isinstance(node, ast.BoolOp):
            word, replacement = _BOOLOPS[type(node.op)]
            for left, right in zip(node.values, node.values[1:]):
                toks = src.between(src.end(left), src.start(right))
                if len(toks) == 1 and toks[0].string == word:
                    yield toks[0].start, "boolop", word, replacement
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
            pos = src.start(node)
            tok = src.at.get(pos)
            if isinstance(node.op, ast.Not) and tok is not None and tok.string == "not":
                offset = src.offset(pos)
                space = _TRAILING_SPACE.match(src.text, offset + 3)
                yield pos, "unary", src.text[offset:space.end()], ""
            elif isinstance(node.op, ast.USub) and tok is not None and tok.string == "-":
                yield pos, "unary", "-", ""
        elif isinstance(node, ast.AugAssign) and type(node.op) in _AUGASSIGNS:
            text, replacement = _AUGASSIGNS[type(node.op)]
            toks = src.between(src.end(node.target), src.start(node.value))
            if len(toks) == 1 and toks[0].string == text:
                yield toks[0].start, "augassign", text, replacement
        elif isinstance(node, ast.Constant):
            tok = src.at.get(src.start(node))
            if tok is None:
                continue
            if node.value is True or node.value is False:
                if tok.string == str(node.value):
                    yield tok.start, "constant", tok.string, str(not node.value)
            elif type(node.value) is int and tok.type == tokenize.NUMBER:
                yield tok.start, "constant", tok.string, str(node.value + 1)


def find_mutants(path: str, source: str, lines: set[int]) -> list[Mutant]:
    """Every compilable mutant whose mutated token lies on one of `lines`, in source order."""
    try:
        tree = ast.parse(source, filename=path)
        src = _Source(source)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return []
    mutants = []
    for (line, col), kind, original, replacement in _sites(src, tree):
        if line not in lines:
            continue
        offset = src.offset((line, col))
        if source[offset:offset + len(original)] != original:
            continue
        mutated = source[:offset] + replacement + source[offset + len(original):]
        try:
            compile(mutated, path, "exec")
        except (SyntaxError, ValueError):
            continue
        mutants.append(Mutant(f"{path}:{line}:{col}:{kind}:{original}->{replacement}",
                              path, line, col, kind, original, replacement, mutated))
    mutants.sort(key=lambda m: (m.line, m.col, m.replacement))
    used: set[str] = set()
    unique = []
    for mutant in mutants:
        new_id, count = mutant.id, 1
        while new_id in used:
            count += 1
            new_id = f"{mutant.id}#{count}"
        used.add(new_id)
        unique.append(replace(mutant, id=new_id) if new_id != mutant.id else mutant)
    assert len({m.id for m in unique}) == len(unique)
    return unique
