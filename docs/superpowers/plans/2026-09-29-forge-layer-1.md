# Forge Layer 1: Orchestrator Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Given a project with an approved spec, Forge repeatedly plans contracts, runs a fresh Claude Code agent on each, verifies the result by running its acceptance test on the exact commit, and merges passing work, with no human in the loop.

**Architecture:** Plain Python in `core/` (protected, no AI) drives everything; AI is only ever called through one narrow interface (`core/agents.py`) so drills can swap in fake agents. Each Forge-built project is its own local git repo with `spec/`, `tests/acceptance/`, `roles.json` and a git-ignored `ledger/`; the existing `Ledger` and `runner` operate on that project root. Windows Task Scheduler starts one cycle every 30 minutes; a lock file prevents overlap.

**Tech Stack:** Python 3.12+ stdlib only (subprocess, json, pathlib, datetime), git, Claude Code CLI (`claude.cmd -p --output-format json`), Windows Task Scheduler (`schtasks`).

**Spec:** "Plan for Layer 1" in the Forge Master Build Plan (claude.ai doc 6695a7ba-015e-4919-a5e9-7f2728e77ff8) plus `charter/authority.md`.

## Global Constraints

- Everything new lives under `core/` (orchestrator code), `drills/` (drills) or `charter/` (limits) — all protected paths; agents can never edit them.
- No new third-party Python dependencies. Python >= 3.10.
- Agents never write ledger files; every state change goes through `Ledger.apply` under a fixed identity from `roles.json`.
- Identities used: `forge-manager` (manager), `forge-executor` (executor), `forge-auditor` (auditor), `forge-core` (core), `ci` (ci), `benjamin` (human).
- Executors may only change files matching the contract's `files_in_scope`; only the Manager may add files under `tests/acceptance/`.
- Daily Claude usage cap: from `charter/limits.json` `daily_token_cap`. At or over the cap, a cycle does nothing and reports `capped`.
- Kill switch: `ledger/KILL` in the project. When present, a cycle stops at the next step boundary and reports `killed`.
- Claude Code is invoked as `claude.cmd` on Windows, `claude` elsewhere, always with stdin closed (`stdin=subprocess.DEVNULL`) and `--output-format json`.
- Windows: all file writes use explicit `newline="\n"`/bytes so drills pass identically on Windows and Linux.

## Review Focus

1. **Manager returns malformed or hostile output** (not JSON, wrong types, test paths outside `tests/acceptance/`, `..` in paths, or edits files directly): nothing but its validated acceptance tests survives, a `plan rejected` note is reported, the cycle continues with existing contracts. Pinned in Task 3 and `test_manager_file_edits_are_discarded` (Task 5). The live Manager also runs read-only (`permission_mode="plan"`).
2. **Agent hangs or the Claude CLI is missing/not logged in**: the call times out or errors, the attempt is failed (not crashed), usage still counted. Pinned in Task 1 and Task 5.
3. **Two cycles start at once** (Task Scheduler fires while a slow cycle runs): the second exits immediately with `busy`; a lock older than 3 hours is treated as stale. Pinned in Task 5.
4. **Executor leaves the working tree dirty or on a branch after a crash**: the next cycle returns the repo to a clean `main` before doing anything. Pinned in Task 5.
5. **Acceptance test hangs or edits files**: evidence runs in a throwaway `git worktree` at the exact commit with a timeout; a timeout is a fail and the project checkout is untouched. Pinned in Task 4.

---

## File Structure

| File | Responsibility |
|---|---|
| `charter/limits.json` (create) | Numeric limits: token cap, timeouts, contracts per cycle |
| `core/agents.py` (create) | `AgentResult`, `ClaudeAgent` (real CLI), `FakeAgent` (drills). The only place AI is called |
| `core/usage.py` (create) | Daily token meter per UTC day, cap check |
| `core/project.py` (create) | Create a project repo; git helpers (`git`, `clean_main`, `commit_paths`) |
| `core/manager.py` (create) | Build the Manager prompt from ledger+spec only; validate and apply its plan |
| `core/evidence.py` (create) | Run a contract's acceptance test at an exact commit; record `test_run` as `ci` |
| `core/loop.py` (create) | One cycle end to end + `python -m core.loop` entry point |
| `core/runner.py` (modify) | Accept `run_contract(..., identity_exec=...)` unchanged API; add `changed_in_scope` to returned report |
| `drills/run_drills.py` (modify) | Drills 11, 12, 13 |
| `scripts/schedule_forge.ps1` (create) | Register/unregister the Task Scheduler job |
| `README.md` (modify) | Layer 1 usage |

---

### Task 0: Prerequisite (human, 1 minute)

Claude Code on the PC reports `Not logged in` when run headless (verified 2026-09-29). Before Task 7's live run, Ben opens PowerShell, runs `claude`, types `/login`, completes the browser sign-in, then `/exit`. Verify:

Run: `cmd /c "claude.cmd -p "Reply with exactly: ok" --output-format json --max-turns 1 < NUL"`
Expected: JSON with `"is_error":false` and `"result":"ok"`.

Tasks 1–6 do not need this (they use `FakeAgent`).

---

### Task 1: Limits file and agent interface

**Files:**
- Create: `charter/limits.json`
- Create: `core/agents.py`
- Test: `tests/core/test_agents.py`

**Interfaces:**
- Produces: `AgentResult(text: str, tokens: int, ok: bool, error: str | None)`; `ClaudeAgent(cmd: list[str] | None = None, timeout_s: int = 1800, permission_mode: str = "acceptEdits", allowed_tools: list[str] | None = None).run(prompt: str, cwd: Path) -> AgentResult`; `FakeAgent(script: Callable[[str, Path], tuple[str, int]]).run(prompt, cwd) -> AgentResult`; `load_limits(forge_root: Path) -> dict`.

- [ ] **Step 1: Write `charter/limits.json`**

```json
{
  "daily_token_cap": 10000000,
  "agent_timeout_s": 1800,
  "test_timeout_s": 600,
  "max_contracts_per_cycle": 3,
  "lock_stale_hours": 3
}
```
`daily_token_cap` is a starting value for "50% of the Max plan"; the weekly report (Layer 6) shows real use so Ben can adjust it through a human-approved PR.

- [ ] **Step 2: Write the failing tests**

```python
# tests/core/test_agents.py
import json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.agents import ClaudeAgent, FakeAgent, load_limits, parse_claude_json

class AgentTests(unittest.TestCase):
    def test_parse_success(self):
        out = json.dumps({"is_error": False, "result": "ok",
                          "usage": {"input_tokens": 10, "output_tokens": 5,
                                    "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}})
        r = parse_claude_json(out)
        self.assertTrue(r.ok); self.assertEqual(r.text, "ok"); self.assertEqual(r.tokens, 20)

    def test_parse_not_logged_in_is_error(self):
        out = json.dumps({"is_error": True, "result": "Not logged in · Please run /login", "usage": {}})
        r = parse_claude_json(out)
        self.assertFalse(r.ok); self.assertIn("Not logged in", r.error)

    def test_parse_garbage(self):
        r = parse_claude_json("not json at all")
        self.assertFalse(r.ok); self.assertEqual(r.tokens, 0)

    def test_missing_cli(self):
        r = ClaudeAgent(cmd=["definitely-not-a-real-binary-xyz"]).run("hi", Path("."))
        self.assertFalse(r.ok); self.assertIn("not found", r.error)

    def test_timeout(self):
        r = ClaudeAgent(cmd=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=1).run("hi", Path("."))
        self.assertFalse(r.ok); self.assertIn("timed out", r.error)

    def test_fake_agent(self):
        r = FakeAgent(lambda p, cwd: ("done", 7)).run("x", Path("."))
        self.assertTrue(r.ok); self.assertEqual((r.text, r.tokens), ("done", 7))

    def test_fake_agent_exception_is_failed_result(self):
        def boom(p, cwd): raise RuntimeError("crash")
        r = FakeAgent(boom).run("x", Path("."))
        self.assertFalse(r.ok); self.assertIn("crash", r.error)

    def test_limits(self):
        self.assertGreater(load_limits(ROOT)["daily_token_cap"], 0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m unittest tests/core/test_agents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.agents'`

- [ ] **Step 4: Implement `core/agents.py`**

```python
"""The only place Forge calls AI. Everything else is plain code.

ClaudeAgent runs Claude Code headless. FakeAgent lets drills script an
agent's behaviour. Both return AgentResult and never raise.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class AgentResult:
    text: str
    tokens: int
    ok: bool
    error: str | None = None


def load_limits(forge_root: Path) -> dict:
    return json.loads((Path(forge_root) / "charter" / "limits.json").read_text(encoding="utf-8"))


def parse_claude_json(out: str) -> AgentResult:
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return AgentResult("", 0, False, "unreadable output from Claude Code")
    u = data.get("usage") or {}
    tokens = sum(int(u.get(k) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    text = str(data.get("result") or "")
    if data.get("is_error") or not data:
        return AgentResult(text, tokens, False, text or "Claude Code reported an error")
    return AgentResult(text, tokens, True, None)


EXECUTOR_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(python:*)", "Bash(python -m unittest:*)"]


class ClaudeAgent:
    """permission_mode "plan" = read-only (Manager); "acceptEdits" + tools = Executor."""

    def __init__(self, cmd: list[str] | None = None, timeout_s: int = 1800,
                 permission_mode: str = "acceptEdits", allowed_tools: list[str] | None = None):
        self.cmd = cmd or ["claude.cmd" if os.name == "nt" else "claude"]
        self.timeout_s = timeout_s
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools if allowed_tools is not None else EXECUTOR_TOOLS

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        if self.cmd[0].startswith("claude"):
            args = self.cmd + ["-p", prompt, "--output-format", "json", "--permission-mode", self.permission_mode]
            if self.allowed_tools:
                args += ["--allowedTools", ",".join(self.allowed_tools)]
        else:
            args = self.cmd
        try:
            p = subprocess.run(args, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=self.timeout_s)
        except FileNotFoundError:
            return AgentResult("", 0, False, f"agent command not found: {self.cmd[0]}")
        except subprocess.TimeoutExpired:
            return AgentResult("", 0, False, f"agent timed out after {self.timeout_s}s")
        return parse_claude_json(p.stdout)


class FakeAgent:
    """script(prompt, cwd) -> (text, tokens). Exceptions become failed results."""

    def __init__(self, script: Callable[[str, Path], tuple[str, int]]):
        self.script = script
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        self.prompts.append(prompt)
        try:
            text, tokens = self.script(prompt, Path(cwd))
            return AgentResult(text, tokens, True, None)
        except Exception as e:  # noqa: BLE001
            return AgentResult("", 0, False, repr(e))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests/core/test_agents.py -v` — Expected: 8 tests OK.

- [ ] **Step 6: Commit**

```bash
git add charter/limits.json core/agents.py tests/core/test_agents.py
git commit -m "Layer 1: agent interface (Claude Code headless + fake) and limits"
```

---

### Task 2: Project repo, git helpers and usage meter

**Files:**
- Create: `core/project.py`, `core/usage.py`
- Test: `tests/core/test_project_usage.py`

**Interfaces:**
- Consumes: `core.ledger.Ledger`
- Produces: `git(root: Path, *args: str, check: bool = True) -> str`; `init_project(root: Path, spec_text: str) -> None`; `clean_main(root: Path) -> None`; `commit_paths(root: Path, paths: list[str], message: str) -> str` (returns sha, "" if nothing to commit); `Meter(root: Path, clock: Callable[[], datetime])` with `.add(tokens: int) -> None`, `.used_today() -> int`, `.over(cap: int) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_project_usage.py
import sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.project import init_project, git, clean_main, commit_paths
from core.usage import Meter

class ProjectTests(unittest.TestCase):
    def test_init_creates_layout_and_clean_main(self):
        d = Path(tempfile.mkdtemp())
        init_project(d, "# Spec\nMake a thing.\n")
        for p in ["spec/spec.md", "roles.json", ".gitignore", "tests/acceptance/.keep"]:
            self.assertTrue((d / p).exists(), p)
        self.assertEqual(git(d, "branch", "--show-current"), "main")
        self.assertIn("ledger/", (d / ".gitignore").read_text())
        self.assertEqual(git(d, "status", "--porcelain"), "")

    def test_clean_main_recovers_dirty_branch(self):
        d = Path(tempfile.mkdtemp()); init_project(d, "# S\n")
        git(d, "checkout", "-q", "-b", "forge/C1-1")
        (d / "junk.txt").write_text("x"); (d / "spec" / "spec.md").write_text("changed")
        clean_main(d)
        self.assertEqual(git(d, "branch", "--show-current"), "main")
        self.assertEqual(git(d, "status", "--porcelain"), "")
        self.assertFalse((d / "junk.txt").exists())

    def test_commit_paths_only_listed(self):
        d = Path(tempfile.mkdtemp()); init_project(d, "# S\n")
        (d / "src").mkdir(); (d / "src/a.py").write_text("a"); (d / "other.txt").write_text("o")
        sha = commit_paths(d, ["src/a.py"], "add a")
        self.assertEqual(len(sha), 40)
        self.assertIn("other.txt", git(d, "status", "--porcelain"))
        self.assertEqual(commit_paths(d, [], "nothing"), "")

class MeterTests(unittest.TestCase):
    def test_daily_rollover_and_cap(self):
        d = Path(tempfile.mkdtemp())
        now = [datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)]
        m = Meter(d, clock=lambda: now[0])
        m.add(600); m.add(500)
        self.assertEqual(m.used_today(), 1100); self.assertTrue(m.over(1000))
        now[0] += timedelta(hours=2)
        self.assertEqual(Meter(d, clock=lambda: now[0]).used_today(), 0)
        self.assertFalse(Meter(d, clock=lambda: now[0]).over(1000))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            Meter(Path(tempfile.mkdtemp())).add(-1)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests/core/test_project_usage.py -v` — Expected: FAIL, `No module named 'core.project'`.

- [ ] **Step 3: Implement `core/project.py`**

```python
"""A Forge-built project: its own git repo with spec/, tests/acceptance/,
roles.json and a git-ignored ledger/. Plus the few git operations the loop needs."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROLES = {"ci": "ci", "forge-manager": "manager", "forge-executor": "executor",
         "forge-auditor": "auditor", "forge-core": "core", "benjamin": "human"}


def git(root: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def init_project(root: Path, spec_text: str) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "spec" / "spec.md", spec_text)
    _write(root / "roles.json", json.dumps(ROLES, indent=2) + "\n")
    _write(root / ".gitignore", "ledger/\n__pycache__/\n*.pyc\n")
    _write(root / "tests" / "acceptance" / ".keep", "")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Forge")
    git(root, "config", "user.email", "forge@localhost")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Project created from spec")


def clean_main(root: Path) -> None:
    """Throw away any half-finished attempt and stand on a clean main."""
    git(root, "reset", "-q", "--hard")
    git(root, "clean", "-q", "-fd")
    git(root, "checkout", "-q", "main")
    git(root, "reset", "-q", "--hard")
    git(root, "clean", "-q", "-fd")


def commit_paths(root: Path, paths: list[str], message: str) -> str:
    if not paths:
        return ""
    git(root, "add", "-A", "--", *paths)
    if not git(root, "diff", "--cached", "--name-only"):
        return ""
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")
```

- [ ] **Step 4: Implement `core/usage.py`**

```python
"""Daily token meter (UTC days). Written only by the loop, never by agents."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class Meter:
    def __init__(self, root: Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(root) / "ledger" / "meter.json"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def used_today(self) -> int:
        return int(self._read().get(self._day(), 0))

    def add(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        data = self._read()
        day = self._day()
        data[day] = int(data.get(day, 0)) + tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def over(self, cap: int) -> bool:
        return self.used_today() >= cap
```

- [ ] **Step 5: Run tests** — `python -m unittest tests/core/test_project_usage.py -v` — Expected: 5 tests OK.

- [ ] **Step 6: Commit**

```bash
git add core/project.py core/usage.py tests/core/test_project_usage.py
git commit -m "Layer 1: project repos, git helpers, daily usage meter"
```

---

### Task 3: Manager (plans from the ledger alone)

**Files:**
- Create: `core/manager.py`
- Test: `tests/core/test_manager.py`

**Interfaces:**
- Consumes: `Ledger`, `AgentResult`, `git`, `commit_paths`
- Produces: `manager_prompt(root: Path) -> str`; `apply_plan(root: Path, text: str, cycle_id: str) -> dict` returning `{"created": [ids], "rejected": str | None}`.

Manager output contract (the prompt demands exactly this JSON, nothing else):
```json
{"contracts": [{"id": "C1", "title": "...", "files_in_scope": ["src/*"],
  "acceptance": "python -m unittest tests/acceptance/test_c1.py",
  "tests": {"tests/acceptance/test_c1.py": "<file contents>"},
  "max_attempts": 3, "token_budget": 200000}], "done": false}
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_manager.py
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.project import init_project, git
from core.ledger import Ledger
from core.manager import manager_prompt, apply_plan

GOOD = {"contracts": [{"id": "C1", "title": "hello", "files_in_scope": ["src/*"],
         "acceptance": "python -m unittest tests/acceptance/test_c1.py",
         "tests": {"tests/acceptance/test_c1.py": "import unittest\n"},
         "max_attempts": 3, "token_budget": 1000}], "done": False}

def proj():
    d = Path(tempfile.mkdtemp()); init_project(d, "# Spec\nSay hello.\n"); return d

class ManagerTests(unittest.TestCase):
    def test_prompt_is_built_from_ledger_and_spec_only(self):
        d = proj()
        p = manager_prompt(d)
        self.assertIn("Say hello.", p); self.assertIn('"contracts": {}', p)

    def test_good_plan_creates_contract_and_commits_test_on_main(self):
        d = proj()
        out = apply_plan(d, json.dumps(GOOD), "cy1")
        self.assertEqual(out, {"created": ["C1"], "rejected": None})
        self.assertEqual(Ledger(d).contracts()["C1"]["status"], "open")
        self.assertIn("test_c1.py", git(d, "show", "--name-only", "HEAD"))

    def test_rejects_non_json(self):
        d = proj(); out = apply_plan(d, "sure, here's a plan!", "cy1")
        self.assertEqual(out["created"], []); self.assertTrue(out["rejected"])

    def test_rejects_test_outside_acceptance_dir(self):
        d = proj(); bad = json.loads(json.dumps(GOOD))
        bad["contracts"][0]["tests"] = {"core/evil.py": "x"}
        out = apply_plan(d, json.dumps(bad), "cy1")
        self.assertEqual(out["created"], []); self.assertFalse((d / "core/evil.py").exists())

    def test_rejects_path_traversal(self):
        d = proj(); bad = json.loads(json.dumps(GOOD))
        bad["contracts"][0]["tests"] = {"tests/acceptance/../../x.py": "x"}
        out = apply_plan(d, json.dumps(bad), "cy1")
        self.assertEqual(out["created"], [])

    def test_scope_may_not_cover_tests_or_spec(self):
        d = proj(); bad = json.loads(json.dumps(GOOD))
        bad["contracts"][0]["files_in_scope"] = ["*"]
        out = apply_plan(d, json.dumps(bad), "cy1")
        self.assertEqual(out["created"], [])

    def test_existing_contract_id_is_skipped_not_fatal(self):
        d = proj(); apply_plan(d, json.dumps(GOOD), "cy1")
        out = apply_plan(d, json.dumps(GOOD), "cy2")
        self.assertEqual(out["created"], [])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — `python -m unittest tests/core/test_manager.py -v` — Expected: FAIL, no module `core.manager`.

- [ ] **Step 3: Implement `core/manager.py`**

```python
"""The Manager: a fresh agent each cycle that sees only the spec and ledger.

This module builds its prompt and turns its answer into ledger proposals.
Plain code decides whether the answer is acceptable; nothing the Manager
says is applied without passing these checks.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path, PurePosixPath

from core.ledger import Ledger, Rejected
from core.project import commit_paths

ID_RE = re.compile(r"^C\d{1,4}$")
FORBIDDEN_SCOPE = ["spec/spec.md", "tests/acceptance/x.py", "roles.json", "ledger/x", "core/x", ".github/x"]

PROMPT = """You are the Manager of an autonomous build system. You have no memory of earlier cycles:
everything you know is below. Split the spec into small contracts that one agent can finish in one
session. For each new contract write a Python unittest acceptance test that fails until the work is
done. Do not re-create contracts that already exist. If every part of the spec is covered by a
contract that is done, answer {{"contracts": [], "done": true}}.

Answer with ONLY this JSON, no prose:
{{"contracts": [{{"id": "C<n>", "title": "...", "files_in_scope": ["src/..."],
  "acceptance": "python -m unittest tests/acceptance/test_c<n>.py",
  "tests": {{"tests/acceptance/test_c<n>.py": "<full file contents>"}},
  "max_attempts": 3, "token_budget": 200000}}], "done": false}}

Rules: files_in_scope may never match spec/, tests/, roles.json, ledger/. Test files go only in
tests/acceptance/.

=== SPEC ===
{spec}
=== LEDGER ===
{ledger}
"""


def manager_prompt(root: Path) -> str:
    root = Path(root)
    spec = (root / "spec" / "spec.md").read_text(encoding="utf-8")
    contracts = Ledger(root).contracts()
    return PROMPT.format(spec=spec, ledger=json.dumps({"contracts": contracts}, indent=2, sort_keys=True))


def _safe_test_path(p: str) -> bool:
    pp = PurePosixPath(p)
    return (p.startswith("tests/acceptance/") and ".." not in pp.parts and p.endswith(".py")
            and "\\" not in p)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in Manager answer")
    return json.loads(m.group(0))


def apply_plan(root: Path, text: str, cycle_id: str) -> dict:
    root = Path(root)
    try:
        plan = _extract_json(text)
        items = plan.get("contracts", [])
        if not isinstance(items, list):
            raise ValueError("contracts must be a list")
    except (ValueError, json.JSONDecodeError) as e:
        return {"created": [], "rejected": f"plan unreadable: {e}"}

    led = Ledger(root)
    existing = led.contracts()
    created, problems = [], []
    for c in items:
        try:
            cid = c["id"]
            if not isinstance(cid, str) or not ID_RE.match(cid):
                raise ValueError(f"bad id {cid!r}")
            if cid in existing:
                continue
            scope = c["files_in_scope"]
            if not isinstance(scope, list) or not scope or not all(isinstance(s, str) for s in scope):
                raise ValueError(f"{cid}: files_in_scope must be a non-empty list")
            if any(fnmatch.fnmatch(f, pat) for f in FORBIDDEN_SCOPE for pat in scope):
                raise ValueError(f"{cid}: files_in_scope covers protected files")
            tests = c["tests"]
            if not isinstance(tests, dict) or not tests:
                raise ValueError(f"{cid}: needs at least one acceptance test file")
            for p, body in tests.items():
                if not _safe_test_path(p) or not isinstance(body, str):
                    raise ValueError(f"{cid}: test path {p!r} not allowed")
            # write tests on main, then create the contract
            for p, body in tests.items():
                f = root / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(body.encode("utf-8"))
            commit_paths(root, list(tests), f"Manager: acceptance tests for {cid}")
            led.apply({"proposal_id": f"{cycle_id}-create-{cid}", "action": "create", "contract_id": cid,
                       "payload": {"title": str(c["title"]), "spec_ref": "spec/spec.md",
                                   "acceptance": str(c["acceptance"]), "files_in_scope": scope,
                                   "max_attempts": int(c.get("max_attempts", 3)),
                                   "token_budget": int(c.get("token_budget", 200000))}}, "forge-manager")
            created.append(cid)
        except (KeyError, TypeError, ValueError, Rejected) as e:
            problems.append(str(e))
    return {"created": created, "rejected": "; ".join(problems) or None}
```

- [ ] **Step 4: Run tests** — Expected: 7 tests OK (the empty-ledger prompt contains `"contracts": {}`).

- [ ] **Step 5: Commit**

```bash
git add core/manager.py tests/core/test_manager.py
git commit -m "Layer 1: Manager prompt from ledger alone; plan validation"
```

---

### Task 4: Evidence (acceptance test at the exact commit)

**Files:**
- Create: `core/evidence.py`
- Test: `tests/core/test_evidence.py`

**Interfaces:**
- Consumes: `Ledger`, `git`
- Produces: `run_evidence(root: Path, cid: str, commit: str, timeout_s: int, run_id: str) -> bool` (records `test_run` as identity `ci`; project checkout untouched).

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_evidence.py
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.project import init_project, git, commit_paths
from core.ledger import Ledger
from core.evidence import run_evidence

def contract(d, acceptance):
    Ledger(d).apply({"proposal_id": "c", "action": "create", "contract_id": "C1", "payload": {
        "title": "t", "spec_ref": "s", "acceptance": acceptance, "files_in_scope": ["src/*"],
        "max_attempts": 3, "token_budget": 1000}}, "forge-manager")

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); init_project(self.d, "# S\n")
        (self.d / "src").mkdir(); (self.d / "src/v.txt").write_bytes(b"good")
        self.sha = commit_paths(self.d, ["src/v.txt"], "v")

    def test_pass_recorded_for_exact_commit(self):
        contract(self.d, f'"{sys.executable}" -c "import sys; sys.exit(open(\'src/v.txt\').read()!=\'good\')"')
        self.assertTrue(run_evidence(self.d, "C1", self.sha, 60, "r1"))
        self.assertEqual(Ledger(self.d).test_runs()["r1"], {"contract_id": "C1", "commit": self.sha, "passed": True})

    def test_runs_old_commit_not_working_tree(self):
        (self.d / "src/v.txt").write_bytes(b"bad")  # uncommitted edit must not affect evidence
        contract(self.d, f'"{sys.executable}" -c "import sys; sys.exit(open(\'src/v.txt\').read()!=\'good\')"')
        self.assertTrue(run_evidence(self.d, "C1", self.sha, 60, "r1"))
        self.assertEqual((self.d / "src/v.txt").read_bytes(), b"bad")

    def test_timeout_is_fail(self):
        contract(self.d, f'"{sys.executable}" -c "import time; time.sleep(10)"')
        self.assertFalse(run_evidence(self.d, "C1", self.sha, 1, "r1"))
        self.assertFalse(Ledger(self.d).test_runs()["r1"]["passed"])

    def test_worktree_removed(self):
        contract(self.d, f'"{sys.executable}" -c "pass"')
        run_evidence(self.d, "C1", self.sha, 60, "r1")
        self.assertEqual(git(self.d, "worktree", "list").count("\n"), 0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL, no module `core.evidence`.

- [ ] **Step 3: Implement `core/evidence.py`**

```python
"""Evidence: run a contract's acceptance test on exactly the submitted commit,
in a throwaway git worktree, and record the result as the ci identity."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from core.ledger import Ledger
from core.project import git


def run_evidence(root: Path, cid: str, commit: str, timeout_s: int, run_id: str) -> bool:
    root = Path(root)
    acceptance = Ledger(root).contracts()[cid]["acceptance"]
    wt = Path(tempfile.mkdtemp(prefix="forge-evidence-"))
    shutil.rmtree(wt)
    passed = False
    try:
        git(root, "worktree", "add", "-q", "--detach", str(wt), commit)
        try:
            p = subprocess.run(acceptance, shell=True, cwd=str(wt), stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=timeout_s)
            passed = p.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
    finally:
        git(root, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
        git(root, "worktree", "prune", check=False)
    Ledger(root).apply({"proposal_id": f"ci-{run_id}", "action": "test_run", "contract_id": cid,
                        "payload": {"run_id": run_id, "commit": commit, "passed": passed}}, "ci")
    return passed
```

- [ ] **Step 4: Run tests** — Expected: 4 tests OK.

- [ ] **Step 5: Commit**

```bash
git add core/evidence.py tests/core/test_evidence.py
git commit -m "Layer 1: evidence runs acceptance at the exact commit in a worktree"
```

---

### Task 5: The cycle

**Files:**
- Create: `core/loop.py`
- Test: `tests/core/test_loop.py`

**Interfaces:**
- Consumes: everything above plus `core.runner.run_contract`, `core.runner.resume`, `core.protect.violations`
- Produces: `run_cycle(root: Path, manager: Agent, executor: Agent, limits: dict, clock=None, cycle_id: str | None = None) -> dict` with keys `status` (`ok` | `idle` | `killed` | `capped` | `busy` | `stopped`), `created`, `ran` (list of `{id, verdict}`), `merged` (ids), `notes` (list[str]); and `main(argv) -> int` for `python -m core.loop --project PATH [--fake]`.

Cycle, in order:
1. Take lock `ledger/LOCK` (exclusive create; stale after `lock_stale_hours`) else `busy`.
2. `clean_main`. Kill switch → `killed`. `verify_chain` false or `spec_drift()` → `stopped` with note. Meter over cap → `capped`.
3. `resume(root)`.
4. Manager: `manager.run(manager_prompt(root), root)`; meter.add(tokens); `apply_plan`.
5. For up to `max_contracts_per_cycle` open contracts (sorted by id): stop with `killed` if KILL exists or `capped` if over cap; `git checkout -b forge/<cid>-<attempts+1>`; `run_contract` with an executor callable that runs the executor agent on the contract prompt and returns its text as the claim; meter.add + ledger `usage` (executor identity, ignore Rejected); if clean report: `commit_paths(changed_in_scope)`; if sha: `submit`, `run_evidence`, then mechanical audit (`pass` if evidence passed else `fail`) as `forge-auditor`; on pass merge the branch into main with `--no-ff`; on fail `reopen` as manager unless parked. If not clean or no sha: `fail` requires `submitted`, so apply `release` as `forge-core`. Always `clean_main` and delete the branch afterwards.
6. Release lock in `finally`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_loop.py
import json, os, sys, tempfile, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.project import init_project, git
from core.ledger import Ledger
from core.agents import FakeAgent
from core.loop import run_cycle

LIMITS = {"daily_token_cap": 10_000, "agent_timeout_s": 60, "test_timeout_s": 60,
          "max_contracts_per_cycle": 3, "lock_stale_hours": 3}
TEST = ("import unittest\nclass T(unittest.TestCase):\n"
        "    def test_it(self):\n        self.assertEqual(open('src/hello.txt').read(), 'hello')\n")
PLAN = json.dumps({"contracts": [{"id": "C1", "title": "hello", "files_in_scope": ["src/*"],
    "acceptance": f'"{sys.executable}" -m unittest tests/acceptance/test_c1.py',
    "tests": {"tests/acceptance/test_c1.py": TEST}, "max_attempts": 2, "token_budget": 5000}], "done": False})

def manager_once():
    def script(prompt, cwd):
        return (PLAN if '"contracts": {}' in prompt else '{"contracts": [], "done": true}', 100)
    return FakeAgent(script)

def writer(text=b"hello", extra=None):
    def script(prompt, cwd):
        (cwd / "src").mkdir(exist_ok=True); (cwd / "src/hello.txt").write_bytes(text)
        if extra: extra(cwd)
        return ("done", 200)
    return FakeAgent(script)

def proj():
    d = Path(tempfile.mkdtemp()); init_project(d, "# Spec\nWrite hello to src/hello.txt\n"); return d

class LoopTests(unittest.TestCase):
    def test_happy_path_merges(self):
        d = proj(); r = run_cycle(d, manager_once(), writer(), LIMITS, cycle_id="cy1")
        self.assertEqual(r["status"], "ok"); self.assertEqual(r["merged"], ["C1"])
        self.assertEqual(Ledger(d).contracts()["C1"]["status"], "done")
        self.assertEqual((d / "src/hello.txt").read_bytes(), b"hello")
        self.assertEqual(git(d, "branch", "--show-current"), "main")

    def test_wrong_work_fails_then_reopens(self):
        d = proj(); r = run_cycle(d, manager_once(), writer(b"nope"), LIMITS, cycle_id="cy1")
        self.assertEqual(r["ran"], [{"id": "C1", "verdict": "fail"}])
        self.assertEqual(Ledger(d).contracts()["C1"]["status"], "open")
        self.assertFalse((d / "src/hello.txt").exists())

    def test_executor_touching_tests_is_not_merged(self):
        def tamper(cwd): (cwd / "tests/acceptance/test_c1.py").write_bytes(b"pass\n")
        d = proj(); r = run_cycle(d, manager_once(), writer(extra=tamper), LIMITS, cycle_id="cy1")
        self.assertEqual(r["merged"], [])
        self.assertIn("assertEqual", (d / "tests/acceptance/test_c1.py").read_text())

    def test_busy_when_locked(self):
        d = proj(); (d / "ledger").mkdir(exist_ok=True); (d / "ledger/LOCK").write_text("123")
        self.assertEqual(run_cycle(d, manager_once(), writer(), LIMITS)["status"], "busy")

    def test_stale_lock_is_taken_over(self):
        d = proj(); (d / "ledger").mkdir(exist_ok=True); lock = d / "ledger/LOCK"; lock.write_text("123")
        old = time.time() - 4 * 3600; os.utime(lock, (old, old))
        self.assertEqual(run_cycle(d, manager_once(), writer(), LIMITS, cycle_id="cy1")["status"], "ok")

    def test_dirty_branch_left_by_crash_is_cleaned(self):
        d = proj(); git(d, "checkout", "-q", "-b", "forge/C9-1"); (d / "junk").write_text("x")
        r = run_cycle(d, manager_once(), writer(), LIMITS, cycle_id="cy1")
        self.assertEqual(r["status"], "ok"); self.assertFalse((d / "junk").exists())

    def test_manager_garbage_does_not_crash(self):
        d = proj(); r = run_cycle(d, FakeAgent(lambda p, c: ("no idea", 10)), writer(), LIMITS, cycle_id="cy1")
        self.assertEqual(r["status"], "idle"); self.assertTrue(any("plan" in n for n in r["notes"]))

    def test_manager_file_edits_are_discarded(self):
        def sneaky(prompt, cwd):
            (cwd / "src").mkdir(exist_ok=True); (cwd / "src/hello.txt").write_bytes(b"hello")
            return ('{"contracts": [], "done": true}', 10)
        d = proj(); run_cycle(d, FakeAgent(sneaky), writer(), LIMITS, cycle_id="cy1")
        self.assertFalse((d / "src/hello.txt").exists())

    def test_executor_error_releases_attempt(self):
        def boom(p, c): raise RuntimeError("agent crashed")
        d = proj(); r = run_cycle(d, manager_once(), FakeAgent(boom), LIMITS, cycle_id="cy1")
        c = Ledger(d).contracts()["C1"]
        self.assertEqual(c["status"], "open"); self.assertEqual(c["attempts"], 1)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL, no module `core.loop`.

- [ ] **Step 3: Implement `core/loop.py`**

```python
"""One Forge cycle, end to end. Plain code; AI only through the agents passed in.

    python -m core.loop --project C:\\Users\\benja\\ForgeProjects\\demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.agents import ClaudeAgent, load_limits
from core.evidence import run_evidence
from core.ledger import Ledger, Rejected
from core.manager import apply_plan, manager_prompt
from core.project import clean_main, commit_paths, git
from core.protect import violations
from core.runner import resume, run_contract
from core.usage import Meter

FORGE_ROOT = Path(__file__).resolve().parent.parent

EXECUTOR_PROMPT = """You are an Executor. Do exactly this one contract and nothing else.
Contract {id}: {title}
You may change only files matching: {scope}
The work is done when this command passes: {acceptance}
The acceptance test is in tests/acceptance/ (read it; never edit it).
When finished, reply with one word: done. If you truly cannot finish, reply: blocked: <reason>.
"""


def _take_lock(root: Path, stale_hours: float) -> bool:
    lock = root / "ledger" / "LOCK"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime > stale_hours * 3600:
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _apply(led: Ledger, pid: str, action: str, cid: str, ident: str, payload: dict | None = None) -> bool:
    try:
        led.apply({"proposal_id": pid, "action": action, "contract_id": cid, "payload": payload or {}}, ident)
        return True
    except Rejected:
        return False


def run_cycle(root: Path, manager, executor, limits: dict, clock=None, cycle_id: str | None = None) -> dict:
    root = Path(root)
    cycle_id = cycle_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = {"status": "ok", "created": [], "ran": [], "merged": [], "notes": []}
    if not _take_lock(root, limits["lock_stale_hours"]):
        return dict(out, status="busy")
    meter = Meter(root, clock=clock)
    kill = root / "ledger" / "KILL"
    try:
        clean_main(root)
        led = Ledger(root)
        if kill.exists():
            return dict(out, status="killed")
        if not led.verify_chain():
            return dict(out, status="stopped", notes=["ledger hash chain broken"])
        drift = led.spec_drift()
        if drift:
            return dict(out, status="stopped", notes=[drift])
        if meter.over(limits["daily_token_cap"]):
            return dict(out, status="capped")

        resumed = resume(root)
        if any(resumed.values()):
            out["notes"].append(f"resumed: {resumed}")

        plan = manager.run(manager_prompt(root), root)
        meter.add(plan.tokens)
        if not plan.ok:
            out["notes"].append(f"manager failed: {plan.error}")
        else:
            res = apply_plan(root, plan.text, cycle_id)
            out["created"] = res["created"]
            if res["rejected"]:
                out["notes"].append(f"plan rejected in part: {res['rejected']}")
        clean_main(root)  # the Manager's only lasting output is its committed acceptance tests

        open_ids = sorted(cid for cid, c in Ledger(root).contracts().items() if c["status"] == "open")
        for cid in open_ids[: limits["max_contracts_per_cycle"]]:
            if kill.exists():
                out["status"] = "killed"
                break
            if meter.over(limits["daily_token_cap"]):
                out["status"] = "capped"
                break
            out["ran"].append({"id": cid, "verdict": _attempt(root, cid, executor, limits, meter, cycle_id, out)})
            if kill.exists():
                out["status"] = "killed"
                break

        if out["status"] == "ok" and not out["created"] and not out["ran"]:
            out["status"] = "idle"
        return out
    finally:
        try:
            clean_main(root)
        finally:
            (root / "ledger" / "LOCK").unlink(missing_ok=True)


def _attempt(root: Path, cid: str, executor, limits: dict, meter: Meter, cycle_id: str, out: dict) -> str:
    c = Ledger(root).contracts()[cid]
    attempt = c["attempts"] + 1
    branch = f"forge/{cid}-{attempt}"
    tag = f"{cycle_id}-{cid}-{attempt}"
    git(root, "checkout", "-q", "-B", branch, "main")
    used = {"tokens": 0}

    def run_agent(ctx):
        k = ctx["contract"]
        r = executor.run(EXECUTOR_PROMPT.format(id=cid, title=k["title"], scope=", ".join(k["files_in_scope"]),
                                                acceptance=k["acceptance"]), root)
        used["tokens"] = r.tokens
        if not r.ok:
            raise RuntimeError(r.error or "agent failed")
        return r.text.strip()[:200]

    try:
        rep = run_contract(root, cid, run_agent, run_id=tag)
    except Rejected as e:  # e.g. kill switch flipped mid-run: stop cleanly, resume() releases the claim later
        meter.add(used["tokens"])
        out["notes"].append(f"{cid}: stopped mid-attempt ({e})")
        return "stopped"
    meter.add(used["tokens"])
    led = Ledger(root)
    _apply(led, f"{tag}-usage", "usage", cid, "forge-executor", {"tokens": used["tokens"]})

    clean = not rep["violations"] and not rep["out_of_scope"] and not rep["error"]
    in_scope = [f for f in rep["changed"] if not violations([f])]
    sha = commit_paths(root, in_scope, f"{cid}: {c['title']}") if clean else ""
    if not sha:
        _apply(led, f"{tag}-release", "release", cid, "forge-core")
        if rep["error"]:
            out["notes"].append(f"{cid}: {rep['error']}")
        return "released"
    if not _apply(led, f"{tag}-submit", "submit", cid, "forge-executor", {"commit": sha}):
        _apply(led, f"{tag}-release", "release", cid, "forge-core")
        return "released"

    passed = run_evidence(root, cid, sha, limits["test_timeout_s"], f"{tag}-ci")
    if passed and _apply(led, f"{tag}-pass", "pass", cid, "forge-auditor", {"run_id": f"{tag}-ci"}):
        git(root, "checkout", "-q", "main")
        git(root, "merge", "-q", "--no-ff", "-m", f"Merge {cid}: {c['title']}", branch)
        git(root, "branch", "-q", "-D", branch)
        out["merged"].append(cid)
        return "pass"
    _apply(led, f"{tag}-fail", "fail", cid, "forge-auditor")
    if Ledger(root).contracts()[cid]["status"] == "failed":
        _apply(led, f"{tag}-reopen", "reopen", cid, "forge-manager")
    git(root, "checkout", "-q", "-f", "main")
    git(root, "branch", "-q", "-D", branch, check=False)
    return "fail"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    args = ap.parse_args(argv)
    limits = load_limits(FORGE_ROOT)
    manager = ClaudeAgent(timeout_s=limits["agent_timeout_s"], permission_mode="plan", allowed_tools=[])
    executor = ClaudeAgent(timeout_s=limits["agent_timeout_s"])
    report = run_cycle(Path(args.project), manager, executor, limits)
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **report})
    log = Path(args.project) / "ledger" / "cycles.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)
    return 0 if report["status"] in {"ok", "idle", "busy", "capped", "killed"} else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests** — `python -m unittest tests/core/test_loop.py -v` — Expected: 9 tests OK. If `test_executor_touching_tests_is_not_merged` fails because the runner's snapshot sees the Manager's committed test as "changed", check `run_contract` is called after checkout (snapshot taken inside `run_contract`).

- [ ] **Step 5: Run the whole suite and existing drills** — `python -m unittest discover -s tests/core -v` and `python drills/run_drills.py`. Expected: all OK, `ALL DRILLS PASS`.

- [ ] **Step 6: Commit**

```bash
git add core/loop.py tests/core/test_loop.py
git commit -m "Layer 1: the cycle - plan, execute, verify, merge, with lock, kill and cap"
```

---

### Task 6: Drills 11–13 and CI

**Files:**
- Modify: `drills/run_drills.py` (add drills 11–13 to `DRILLS`)
- Modify: `.github/workflows/core-checks.yml` (also run `tests/core`)

**Interfaces:**
- Consumes: `run_cycle`, `FakeAgent`, `init_project`

- [ ] **Step 1: Add the drills** (insert before `DRILLS = [`)

```python
def _l1_project():
    from core.project import init_project
    d = Path(tempfile.mkdtemp(prefix="forge-l1-"))
    init_project(d, "# Spec\nWrite hello to src/hello.txt\n")
    return d

L1_LIMITS = {"daily_token_cap": 10_000, "agent_timeout_s": 60, "test_timeout_s": 60,
             "max_contracts_per_cycle": 3, "lock_stale_hours": 3}
L1_TEST = ("import unittest\nclass T(unittest.TestCase):\n"
           "    def test_it(self):\n        self.assertEqual(open('src/hello.txt').read(), 'hello')\n")


def _l1_plan(n):
    return json.dumps({"contracts": [{"id": f"C{i}", "title": f"hello {i}", "files_in_scope": ["src/*"],
        "acceptance": f'"{sys.executable}" -m unittest tests/acceptance/test_c{i}.py',
        "tests": {f"tests/acceptance/test_c{i}.py": L1_TEST}, "max_attempts": 2, "token_budget": 5000}
        for i in range(1, n + 1)], "done": False})


def drill_11():
    """A full cycle runs from the ledger alone: a brand-new Manager each cycle, no memory."""
    from core.agents import FakeAgent
    from core.loop import run_cycle
    d = _l1_project()
    seen = []
    def manager(prompt, cwd):
        seen.append(prompt)
        return (_l1_plan(1) if '"contracts": {}' in prompt else '{"contracts": [], "done": true}', 50)
    def executor(prompt, cwd):
        (cwd / "src").mkdir(exist_ok=True); (cwd / "src/hello.txt").write_bytes(b"hello"); return ("done", 50)
    r1 = run_cycle(d, FakeAgent(manager), FakeAgent(executor), L1_LIMITS, cycle_id="a")
    assert r1["status"] == "ok" and r1["merged"] == ["C1"], r1
    # second cycle: fresh Manager object; everything it knows must come from the ledger
    r2 = run_cycle(d, FakeAgent(manager), FakeAgent(executor), L1_LIMITS, cycle_id="b")
    assert r2["status"] == "idle", r2
    assert '"status": "done"' in seen[1], "second Manager did not see the ledger state"
    assert "cycle a" not in seen[1].lower()
    assert Ledger(d).verify_chain()
    return "spec -> contract -> agent -> test at exact commit -> merged; next cycle's fresh Manager sees it done and idles"


def drill_12():
    """Kill switch flipped while an agent is working -> cycle stops, nothing lost, resumes cleanly."""
    from core.agents import FakeAgent
    from core.loop import run_cycle
    d = _l1_project()
    manager = FakeAgent(lambda p, c: (_l1_plan(2) if '"contracts": {}' in p else '{"contracts": [], "done": true}', 10))
    def killer(prompt, cwd):
        (cwd / "src").mkdir(exist_ok=True); (cwd / "src/hello.txt").write_bytes(b"hello")
        (cwd / "ledger" / "KILL").write_text("stop")
        return ("done", 10)
    r = run_cycle(d, manager, FakeAgent(killer), L1_LIMITS, cycle_id="k")
    assert r["status"] == "killed" and len(r["ran"]) == 1, r
    c = Ledger(d).contracts()
    assert c["C2"]["status"] == "open" and c["C2"]["attempts"] == 0, "second contract ran after kill"
    assert Ledger(d).verify_chain()
    assert run_cycle(d, manager, FakeAgent(killer), L1_LIMITS, cycle_id="k2")["status"] == "killed"
    (d / "ledger" / "KILL").unlink()
    def ok(prompt, cwd):
        (cwd / "src").mkdir(exist_ok=True); (cwd / "src/hello.txt").write_bytes(b"hello"); return ("done", 10)
    r = run_cycle(d, manager, FakeAgent(ok), L1_LIMITS, cycle_id="k3")
    assert all(Ledger(d).contracts()[i]["status"] == "done" for i in ("C1", "C2")), Ledger(d).contracts()
    return "kill mid-agent stops the cycle after that step; later cycles refuse to start; after removing KILL all work finishes"


def drill_13():
    """Daily usage cap reached -> Forge pauses until the next UTC day."""
    from datetime import datetime, timedelta, timezone
    from core.agents import FakeAgent
    from core.loop import run_cycle
    d = _l1_project()
    now = [datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)]
    calls = []
    def manager(prompt, cwd):
        calls.append(1)
        return (_l1_plan(3) if '"contracts": {}' in prompt else '{"contracts": [], "done": true}', 4000)
    def executor(prompt, cwd):
        (cwd / "src").mkdir(exist_ok=True); (cwd / "src/hello.txt").write_bytes(b"hello"); return ("done", 4000)
    limits = dict(L1_LIMITS, daily_token_cap=10_000)
    r = run_cycle(d, FakeAgent(manager), FakeAgent(executor), limits, clock=lambda: now[0], cycle_id="u1")
    assert r["status"] == "capped" and len(r["ran"]) == 2, r   # 4000 + 4000 + 4000 >= 10000 stops the 3rd
    before = len(calls)
    r = run_cycle(d, FakeAgent(manager), FakeAgent(executor), limits, clock=lambda: now[0], cycle_id="u2")
    assert r["status"] == "capped" and len(calls) == before, "an agent was called while over the cap"
    now[0] += timedelta(days=1)
    r = run_cycle(d, FakeAgent(manager), FakeAgent(executor), limits, clock=lambda: now[0], cycle_id="u3")
    assert r["status"] == "ok" and r["merged"] == ["C3"], r
    return "cap stops work mid-cycle and blocks every agent call the rest of the day; next day work resumes"
```

Register them:

```python
    (11, "Full cycle from the ledger alone", drill_11),
    (12, "Kill switch during a live cycle", drill_12),
    (13, "Daily usage cap", drill_13),
```

Also add `import sys` is already present; ensure `json`, `tempfile` imports exist at the top (they do).

- [ ] **Step 2: Run** — `python drills/run_drills.py` — Expected: 13 PASS lines, `ALL DRILLS PASS`.

- [ ] **Step 3: CI runs the unit tests too** — in `.github/workflows/core-checks.yml`, after the "Sabotage drills" step add:

```yaml
      - name: Core unit tests
        run: python -m unittest discover -s tests/core -v
```

- [ ] **Step 4: Mutation check** — break each of these one at a time and confirm at least one drill or test fails, then restore: (a) remove the kill check inside the contract loop; (b) remove the cap check before the Manager call; (c) make `apply_plan` skip `_safe_test_path`; (d) run evidence in `root` instead of the worktree; (e) delete `clean_main` at cycle start; (f) remove the lock.

- [ ] **Step 5: Commit**

```bash
git add drills/run_drills.py .github/workflows/core-checks.yml
git commit -m "Layer 1: drills 11-13 (full cycle, kill mid-cycle, daily cap); CI runs core unit tests"
```

---

### Task 7: Schedule, docs, and the live proof

**Files:**
- Create: `scripts/schedule_forge.ps1`
- Create: `core/newproject.py` (tiny CLI around `init_project`)
- Modify: `README.md`

- [ ] **Step 1: `core/newproject.py`**

```python
"""python -m core.newproject PATH SPEC_FILE   -> creates a Forge project repo."""
import sys
from pathlib import Path
from core.project import init_project

if __name__ == "__main__":
    path, spec = Path(sys.argv[1]), Path(sys.argv[2])
    init_project(path, spec.read_text(encoding="utf-8"))
    print(f"Created {path}.\nNext: python -m core.cli --project {path} approve-spec\n"
          f"Then:  python -m core.loop --project {path}")
```

Spec approval needs `core/cli.py` to act on a project instead of the Forge repo, so add to `core/cli.py` a `--project PATH` option read before the command (default `ROOT`), used by `verify`, `status`, `approve-spec`, `resume`:

```python
    if argv and argv[0] == "--project":
        root, argv = Path(argv[1]), argv[2:]
    else:
        root = ROOT
    led = Ledger(root)
```
and replace remaining `ROOT` uses inside `main` with `root`.

- [ ] **Step 2: `scripts/schedule_forge.ps1`**

```powershell
# Register (or remove) the Task Scheduler job that runs one Forge cycle every 30 minutes.
#   .\scripts\schedule_forge.ps1 -Project C:\Users\benja\ForgeProjects\demo
#   .\scripts\schedule_forge.ps1 -Project C:\Users\benja\ForgeProjects\demo -Remove
param([Parameter(Mandatory)][string]$Project, [switch]$Remove, [int]$Minutes = 30)
$name = 'Forge cycle - ' + (Split-Path $Project -Leaf)
if ($Remove) { schtasks /Delete /TN $name /F; exit $LASTEXITCODE }
$forge = Split-Path -Parent $PSScriptRoot
$py = (Get-Command python).Source
$cmd = "cmd /c cd /d `"$forge`" && `"$py`" -m core.loop --project `"$Project`""
schtasks /Create /TN $name /SC MINUTE /MO $Minutes /TR $cmd /F
```

- [ ] **Step 3: README** — add a "Layer 1: running Forge" section: create a project (`python -m core.newproject`), approve its spec (`python -m core.cli --project PATH approve-spec`), run one cycle by hand (`python -m core.loop --project PATH`), schedule it (`scripts\schedule_forge.ps1`), stop everything (create `ledger\KILL` in the project), read what happened (`ledger\cycles.log`).

- [ ] **Step 4: Commit and open the PR** (protected paths, so Ben approves)

```bash
git add core/newproject.py core/cli.py scripts/schedule_forge.ps1 README.md
git commit -m "Layer 1: project CLI, scheduler script, docs"
git push -u origin layer-1
gh pr create --title "Layer 1: orchestrator loop" --body "Drills 1-13 and core unit tests pass. Needs owner approval (protected paths)."
```

- [ ] **Step 5: Live proof (after Task 0 and PR merge)** — on the PC:

```powershell
cd C:\Users\benja\Forge; git pull
"# Spec`nCreate src/greet.py with a function greet(name) that returns 'Hello, <name>!'. Include a README.md line describing it." | Set-Content -Encoding utf8 $env:TEMP\demo-spec.md
python -m core.newproject C:\Users\benja\ForgeProjects\demo $env:TEMP\demo-spec.md
python -m core.cli --project C:\Users\benja\ForgeProjects\demo approve-spec
python -m core.loop --project C:\Users\benja\ForgeProjects\demo
```
Expected: JSON line with `"status": "ok"` and at least one id in `"merged"`; `git -C C:\Users\benja\ForgeProjects\demo log --oneline` shows a `Merge C1` commit; a second run prints `"status": "idle"` once all contracts are done. Only then mark Layer 1 done in the Build Plan doc.

---

## Self-review notes

- Spec coverage: cycle steps 1–5 → Task 5; Manager from ledger only → Task 3 + drill 11; fresh executors → Task 1 (`-p`, new process each call); evidence at exact commit → Task 4; schedule → Task 7 (Task Scheduler instead of n8n, because n8n runs inside Docker and cannot start Windows programs; n8n stays for venture workflows); usage cap → Task 2 + drill 13; kill → drill 12; final proof → Task 7 Step 5.
- Change from the earlier plan: GitHub App identity is not needed for Layer 1 because projects are local repos and Forge never pushes; it moves to the first layer that pushes to GitHub (venture runner).
- Layer 1 audit is mechanical (evidence decides pass/fail); Layer 2 replaces it with the Codex Auditor.
