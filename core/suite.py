"""Run the core test suite fast: each tests/core/test_*.py module in its own process, several at once.

The full suite (python -m core.suite) runs at the layer gate and in CI. Run sequentially it takes about 20
minutes on Ben's PC; in parallel it is bounded by its slowest module. Exit code 0 only if every module passed;
a module that times out counts as failed.

R57: per-task judges run python -m core.suite --changed <base>..<sha>, which runs only
  - every tests/core module that imports (directly or through other repo modules) a module the range changed,
  - the task's own test files (--include) and test modules the range itself changed,
  - every module listed in tests/core/FAST_MODULES.txt (regenerate with --write-fast).
A change to a high-blast-radius path (HIGH_BLAST), or one the selection can't account for, runs the full suite."""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

FAST_FILE = "tests/core/FAST_MODULES.txt"
FAST_LIMIT_S = 20.0
# R57: a change to any of these runs the full suite (they reach nearly everything, often without an import).
HIGH_BLAST = ("core/bootstrap.py", "core/ledger.py", "core/agents.py", "core/protect.py", "drills/")
# Paths no test can depend on through code: changes here select nothing by themselves.
INERT = ("docs/", ".github/")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "work", "state"}
_MOD_NAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def modules(root: Path) -> list[str]:
    return sorted(p.stem for p in (Path(root) / "tests" / "core").glob("test_*.py"))


# ---------------------------------------------------------------------------- R57 selection
def _mod_of(rel: str) -> str | None:
    """'core/x.py' -> 'core.x', 'core/__init__.py' -> 'core'; None for anything else."""
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def repo_sources(root: Path) -> dict[str, tuple[str, str]]:
    """Every Python module in the repo: {module name: (posix path, source text)}."""
    root = Path(root)
    out: dict[str, tuple[str, str]] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            name = _mod_of(rel)
            if name is None:
                continue
            try:
                out[name] = (rel, (Path(dirpath) / f).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return out


def _imports(name: str, rel: str, src: str, known: set[str]) -> set[str]:
    """Repo modules `name` depends on: its imports (absolute, relative, sibling fallbacks), its parent packages,
    and repo module names it spells out as a string (e.g. "-m", "core.status_page")."""
    deps: set[str] = set()
    pkg = name if rel.endswith("__init__.py") else name.rpartition(".")[0]

    def add(n: str) -> None:
        if not n:
            return
        cands = [n] + ([f"{pkg}.{n}"] if pkg else [])
        for cand in cands:
            parts = cand.split(".")
            hit = False
            for i in range(len(parts), 0, -1):  # the longest prefix that is a repo module, and its packages
                if ".".join(parts[:i]) in known:
                    hit = True
                    for j in range(1, i + 1):
                        if ".".join(parts[:j]) in known:
                            deps.add(".".join(parts[:j]))
                    break
            if hit:
                return

    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return {m for m in known if m != name}  # unreadable: assume it depends on everything (fail safe)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".") if pkg else []
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            add(mod)
            for a in node.names:
                if mod:
                    add(f"{mod}.{a.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _MOD_NAME.match(node.value):
            if node.value in known:
                add(node.value)
    parts = name.split(".")
    for j in range(1, len(parts)):  # importing a.b.c runs a/__init__ and a/b/__init__
        if ".".join(parts[:j]) in known:
            deps.add(".".join(parts[:j]))
    deps.discard(name)
    return deps


def import_graph(root: Path) -> tuple[dict[str, set[str]], dict[str, tuple[str, str]]]:
    srcs = repo_sources(root)
    known = set(srcs)
    return {n: _imports(n, rel, src, known) for n, (rel, src) in srcs.items()}, srcs


def read_fast(root: Path) -> list[str]:
    p = Path(root) / FAST_FILE
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        ln = ln.split("#", 1)[0].strip()
        if ln:
            out.append(ln.split()[0])
    return out


def _is_test_module(rel: str) -> bool:
    return bool(re.fullmatch(r"tests/core/test_\w+\.py", rel))


def select(root: Path, changed: list[str], include: list[str] | None = None) -> tuple[list[str] | None, str]:
    """R57: the test modules to run for these changed paths. (None, why) means run the full suite."""
    root = Path(root)
    changed = sorted({c.replace("\\", "/").strip() for c in changed if c and c.strip()})
    for c in changed:
        if any(c == h or (h.endswith("/") and c.startswith(h)) for h in HIGH_BLAST):
            return None, f"high-blast-radius change: {c}"
        if c.endswith(".py") and not (root / c).exists():  # a deleted module: its importers are unknowable now
            return None, f"deleted module: {c}"
    graph, srcs = import_graph(root)
    all_tests = set(modules(root))
    changed_mods: set[str] = set()
    picked: set[str] = set()
    for c in changed:
        name = _mod_of(c)
        if name is not None:
            changed_mods.add(name)
            if _is_test_module(c) and Path(c).stem in all_tests:
                picked.add(Path(c).stem)
            continue
        # a non-Python file counts as a change to every module whose source names it (e.g. a test reading a doc)
        mentions = {n for n, (_rel, src) in srcs.items() if Path(c).name in src}
        inert = c.startswith(INERT) or (c.endswith(".md") and not c.startswith("agents/"))
        if not mentions and not inert:  # a data file no code names: its readers are unknown, so everything runs
            return None, f"changed file with unknown readers: {c}"
        changed_mods |= mentions
    # reverse closure: everything that (transitively) imports a changed module
    rdeps: dict[str, set[str]] = {}
    for n, deps in graph.items():
        for d in deps:
            rdeps.setdefault(d, set()).add(n)
    affected, todo = set(changed_mods), list(changed_mods)
    while todo:
        for user in rdeps.get(todo.pop(), ()):
            if user not in affected:
                affected.add(user)
                todo.append(user)
    for name in affected:
        if name.startswith("tests.core.test_") and name.count(".") == 2 and name.split(".")[-1] in all_tests:
            picked.add(name.split(".")[-1])
    for inc in include or []:
        stem = Path(inc.replace("\\", "/")).stem
        if stem in all_tests:
            picked.add(stem)
    picked |= set(read_fast(root)) & all_tests
    return sorted(picked), f"{len(changed)} changed file(s), {len(picked)} of {len(all_tests)} modules selected"


def changed_files(root: Path, rng: str) -> list[str]:
    p = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", rng], cwd=str(root),
                       stdin=subprocess.DEVNULL, capture_output=True, **NOWIN)
    if p.returncode != 0:
        raise RuntimeError(f"git diff {rng}: {p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f.strip()]


# ---------------------------------------------------------------------------- running
# Modules that take ~11 min alone and far longer under full parallel load get a longer limit, so a
# whole-suite judge (high-blast changes) is not failed by load alone.
SLOW_MODULES = {"test_merge_pipeline": 3.0}


def module_timeout(name: str, timeout_s: float) -> float:
    return timeout_s * SLOW_MODULES.get(name, 1.0)


def run_module(root: Path, name: str, timeout_s: float) -> tuple[str, bool, str]:
    timeout_s = module_timeout(name, timeout_s)
    try:
        p = subprocess.run([sys.executable, "-m", "unittest", f"tests.core.{name}"], cwd=str(root),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout_s, **NOWIN)
    except subprocess.TimeoutExpired:
        return name, False, f"timed out after {timeout_s:.0f}s"
    out = p.stdout.decode("utf-8", "replace")
    return name, p.returncode == 0, out


def _timed(root: Path, name: str, timeout_s: float) -> tuple[str, bool, str, float]:
    t0 = time.monotonic()
    n, ok, out = run_module(root, name, timeout_s)
    return n, ok, out, time.monotonic() - t0


def write_fast(root: Path, jobs: int, timeout_s: float, limit_s: float = FAST_LIMIT_S) -> int:
    """Run every module once and write FAST_FILE: the modules that passed in under limit_s seconds."""
    names = modules(root)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        res = list(ex.map(lambda n: _timed(root, n, timeout_s), names))
    fast = sorted((n, s) for n, ok, _o, s in res if ok and s < limit_s)
    lines = [f"# R57: tests/core modules that ran in under {limit_s:.0f} s; every per-task judge runs them.",
             f"# Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} with --jobs {jobs}. "
             "Regenerate: python -m core.suite --write-fast",
             *[f"{n}  {s:.1f}" for n, s in fast]]
    (Path(root) / FAST_FILE).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    for n, ok, _o, s in sorted(res, key=lambda r: r[3]):
        print(f"{s:7.1f}s {'ok  ' if ok else 'FAIL'} {n}")
    print(f"\n{len(fast)} of {len(names)} modules are fast; written to {FAST_FILE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run tests/core in parallel, one process per module")
    ap.add_argument("--root", default=".")
    ap.add_argument("--jobs", type=int, default=max(2, min(8, (os.cpu_count() or 2))))
    ap.add_argument("--module-timeout", type=float, default=1500.0)
    ap.add_argument("--changed", metavar="BASE..SHA", help="R57: run only the modules this range can affect")
    ap.add_argument("--include", action="append", default=[], help="R57: a test file that always runs")
    ap.add_argument("--exclude", action="append", default=[],
                    help="R59: a test file that must not run (the tests of a layer task not built yet)")
    ap.add_argument("--write-fast", action="store_true", help=f"R57: time every module and rewrite {FAST_FILE}")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if a.write_fast:
        return write_fast(root, a.jobs, a.module_timeout)
    names = modules(root)
    if not names:
        print("no test modules found")
        return 1
    if a.changed:
        try:
            picked, why = select(root, changed_files(root, a.changed), a.include)
        except (RuntimeError, OSError) as e:  # a range we can't read: fail safe, everything runs
            picked, why = None, f"could not read the change: {e}"
        print(f"R57 --changed {a.changed}: " + (why if picked is not None else f"full suite ({why})"))
        if picked is not None:
            names = picked
            if not names:
                print("no test modules selected")
                return 0
    skip = {Path(str(x).replace("\\", "/")).stem for x in a.exclude}
    if skip:  # R59: tests committed ahead of their (not yet built) task are not this task's judges
        print("R59 excluded: " + ", ".join(sorted(skip & set(names))))
        names = [n for n in names if n not in skip]
    failed = []
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for name, ok, out in ex.map(lambda n: run_module(root, n, a.module_timeout), names):
            last = out.strip().splitlines()[-1] if out.strip() else ""
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {last}")
            if not ok:
                failed.append((name, out))
    for name, out in failed:
        print(f"\n===== {name} =====\n{out[-6000:]}")
    print(f"\n{len(names) - len(failed)} of {len(names)} modules passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
