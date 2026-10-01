"""Run the core test suite fast: each tests/core/test_*.py module in its own process, several at once.

The judges run this on every task (python -m core.suite). Run sequentially, the suite takes about 20
minutes on Ben's PC, longer than a judge may run; in parallel it is bounded by its slowest module.
Exit code 0 only if every module passed; a module that times out counts as failed."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def modules(root: Path) -> list[str]:
    return sorted(p.stem for p in (Path(root) / "tests" / "core").glob("test_*.py"))


def run_module(root: Path, name: str, timeout_s: float) -> tuple[str, bool, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "unittest", f"tests.core.{name}"], cwd=str(root),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout_s, **NOWIN)
    except subprocess.TimeoutExpired:
        return name, False, f"timed out after {timeout_s:.0f}s"
    out = p.stdout.decode("utf-8", "replace")
    return name, p.returncode == 0, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run tests/core in parallel, one process per module")
    ap.add_argument("--root", default=".")
    ap.add_argument("--jobs", type=int, default=max(2, min(8, (os.cpu_count() or 2))))
    ap.add_argument("--module-timeout", type=float, default=1500.0)
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    names = modules(root)
    if not names:
        print("no test modules found")
        return 1
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
