"""Per-task git worktrees and throwaway worktrees at an exact commit (T1B3b).

Plain code, standard library only, no imports from core.bootstrap. Every git call runs without a shell,
with no stdin and (on Windows) no window.

    work/tasks/<tid>   the task's own worktree, on branch forge-task/<tid> (main lane) or forge-lane/<lane>/<tid>
                       (any other lane, R60: two lanes may both have a task T1)
    work/tmp/<12 hex>  a throwaway worktree, detached at one exact commit
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
IDENT = ["-c", "user.name=Forge", "-c", "user.email=forge@localhost"]
TID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
REMOVE_TRIES, REMOVE_PAUSE_S = 5, 0.5


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *IDENT, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, **NOWIN)


def _git(cwd: Path, *args: str) -> str:
    p = _run(cwd, *args)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(p.stderr or '').strip()[:500]}")
    return (p.stdout or "").strip()


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def _unlock(fn, path, _exc) -> None:
    """Windows marks git object files read-only; make them writable and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    except OSError:
        pass


class Worktrees:
    def __init__(self, repo: Path, work: Path, branch_prefix: str = "forge-task/"):
        self.repo = Path(repo)
        self.work = Path(work)
        # R60: main keeps forge-task/<tid>; another lane uses forge-lane/<lane>/<tid>. Not forge-task/<lane>/<tid>:
        # git can't hold both a branch forge-task/p2 (a main task named p2) and forge-task/p2/T1.
        self.prefix = branch_prefix

    # ------------------------------------------------------------------ names
    @staticmethod
    def _check(tid) -> str:
        if not isinstance(tid, str) or not TID_RE.fullmatch(tid):
            raise ValueError(f"bad task id {tid!r}")
        return tid

    def task_path(self, tid: str) -> Path:
        return self.work / "tasks" / self._check(tid)

    def task_branch(self, tid: str) -> str:
        return f"{self.prefix}{self._check(tid)}"

    # ------------------------------------------------------------------ git helpers
    def _commit(self, ref: str) -> str:
        """The full SHA of `ref` as a commit, or RuntimeError."""
        p = _run(self.repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        sha = (p.stdout or "").strip()
        if p.returncode != 0 or not sha:
            raise RuntimeError(f"unknown commit {ref!r}")
        return sha

    def _registered(self, path: Path) -> bool:
        out = _git(self.repo, "worktree", "list", "--porcelain")
        return any(_same(Path(ln[len("worktree "):]), path) for ln in out.splitlines() if ln.startswith("worktree "))

    def _branch_exists(self, branch: str) -> bool:
        return _run(self.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0

    def _remove_tree(self, path: Path) -> None:
        """Unregister and delete one worktree folder (retrying for Windows file locks), then prune."""
        path = Path(path)
        if self._registered(path):
            _run(self.repo, "worktree", "remove", "--force", str(path))
        for attempt in range(REMOVE_TRIES):
            if not path.exists():
                break
            try:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(path, onexc=_unlock)
                else:
                    shutil.rmtree(path, onerror=_unlock)
            except OSError:
                pass
            if path.exists() and attempt < REMOVE_TRIES - 1:
                time.sleep(REMOVE_PAUSE_S)
        _run(self.repo, "worktree", "prune")
        if path.exists():
            raise OSError(f"could not remove worktree folder {path}")

    # ------------------------------------------------------------------ task worktrees
    def prepare_task(self, tid: str, base: str) -> Path:
        """Make task_path(tid) a clean worktree on task_branch(tid) whose HEAD is exactly `base`."""
        path, branch = self.task_path(tid), self.task_branch(tid)
        sha = self._commit(base)
        if self._registered(path) and (path / ".git").exists():
            _git(path, "checkout", "-q", "-f", "-B", branch, sha)
            _git(path, "clean", "-q", "-fd")
        else:
            _git(self.repo, "worktree", "prune")
            if path.exists():
                self._remove_tree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(self.repo, "worktree", "add", "-f", "-B", branch, str(path), sha)
        head = _git(path, "rev-parse", "HEAD")
        if head != sha:
            raise RuntimeError(f"task worktree {tid} is at {head}, expected {sha}")
        return path

    def remove_task(self, tid: str) -> None:
        """Idempotent: the task's worktree folder and its forge-task/<tid> branch are gone afterwards."""
        path, branch = self.task_path(tid), self.task_branch(tid)
        self._remove_tree(path)
        if self._branch_exists(branch):
            _git(self.repo, "branch", "-D", branch)

    # ------------------------------------------------------------------ throwaway
    @contextlib.contextmanager
    def throwaway(self, sha: str):
        """A detached worktree at exactly `sha` under work/tmp, removed on exit (failures left for sweep)."""
        full = self._commit(sha)
        path = self.work / "tmp" / secrets.token_hex(6)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(path), full)
        try:
            yield path
        finally:
            try:
                self._remove_tree(path)
            except Exception:  # noqa: BLE001 - left for sweep; never masks the body's exception
                pass

    # ------------------------------------------------------------------ sweep
    def sweep(self, keep_tasks: set[str]) -> list[str]:
        """Remove every throwaway folder and every task worktree not in keep_tasks. Returns removed paths
        relative to work (POSIX). Never touches the layer worktree or the repo."""
        keep = set(keep_tasks or ())
        removed: list[str] = []
        _run(self.repo, "worktree", "prune")
        tmp = self.work / "tmp"
        if tmp.is_dir():
            for p in sorted(tmp.iterdir()):
                try:
                    self._remove_tree(p) if p.is_dir() else p.unlink()
                    removed.append(p.relative_to(self.work).as_posix())
                except OSError:
                    pass  # still locked: tried again on the next sweep
        tasks = self.work / "tasks"
        seen = set()
        if tasks.is_dir():
            for p in sorted(tasks.iterdir()):
                seen.add(p.name)
                if p.name in keep:
                    continue
                try:
                    if TID_RE.fullmatch(p.name):
                        self.remove_task(p.name)
                    elif p.is_dir():
                        self._remove_tree(p)
                    else:
                        p.unlink()
                    removed.append(p.relative_to(self.work).as_posix())
                except (OSError, RuntimeError):
                    pass
        # a crash between removing a task folder and deleting its branch leaves an orphan branch
        out = _run(self.repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{self.prefix}").stdout or ""
        for branch in out.split():  # only this lane's own namespace (another lane's tid holds a "/", never matches)
            tid = branch[len(self.prefix):]
            if tid in keep or tid in seen or not TID_RE.fullmatch(tid):
                continue
            if not self._registered(self.work / "tasks" / tid):
                _run(self.repo, "branch", "-D", branch)
        _run(self.repo, "worktree", "prune")
        return removed
