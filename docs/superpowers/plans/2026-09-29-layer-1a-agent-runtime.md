# Layer 1A: Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Bootstrap rule (D-025):** for each task, Codex writes the tests first (step 1 is given to Codex as its job), Claude implements, the judges run, and Codex reviews. Every task is a ledger contract.

**Goal:** Run Claude Code and Codex as hidden, low-priority, time-limited background agents with usage metered per provider, and know at all times which of Forge's connections actually work.

**Architecture:** Three focused modules in `core/`:
- `agents.py`: one process launcher plus a Claude adapter, a Codex adapter and a fake adapter, all returning `AgentResult`.
- `usage.py`: daily token meter per provider, with caps from the charter.
- `readiness.py`: named checks that write `state/capabilities.json`.

Forge-wide runtime state lives in `state/` (git-ignored).

**Tech Stack:** Python 3.12 stdlib (`subprocess`, `json`, `shutil`, `urllib`, `smtplib`), plus `keyring` (installed), `playwright` (installed); Claude Code CLI 2.1.x; Codex CLI 0.158.

**Spec:** `docs/specs/layer-1-design.md` §1, §4 and §7 (sub-plan 1A); decisions D-019, D-020, D-029, D-030.

## Global Constraints

- Every agent process: no window, below-normal priority on Windows, hard timeout that kills the whole process tree.
- Prompts go to agents on **stdin**, never argv. `.cmd` shims hit cmd.exe's 8,191-character line limit.
- Codex always runs with `--ignore-user-config --ephemeral --json --skip-git-repo-check`.
- Caps come from `charter/limits.json`: `claude_daily_token_cap`, `codex_daily_token_cap`.
- Secrets are read only inside plain-code checks (`keyring`), never passed to agents, printed or logged.
- New files are protected (`core/`, `charter/`). `state/` is added to `.gitignore`.
- All file writes are LF bytes. Tests pass on Windows and Linux.

## Review Focus

1. **Agent prints non-JSON, partial JSON, or nothing** (crash, not logged in, network drop). Expected: `ok=False` with a readable error and tokens counted if known; never an exception. Pinned in Task 1 tests (`test_codex_no_turn_completed`, `test_claude_garbage`).
2. **Agent spawns children and hangs.** Expected: the timeout kills the whole tree and no orphan keeps running. Pinned in `test_timeout_kills_tree`.
3. **Structured output requested but the agent returns prose.** Expected: `ok=False`, error "output did not match the required shape", raw text kept for the log. Pinned in `test_schema_mismatch`.
4. **Readiness check hangs or throws** (Docker hung, DNS down). Expected: each check has its own timeout; one failing check never stops the others; the map records the failure detail. Pinned in `test_check_exception_isolated`, `test_check_timeout`.
5. **Midnight rollover and two providers.** Expected: a Codex cap never blocks Claude and vice versa; a new UTC day resets both. Pinned in Task 2 tests.

---

## File Structure

| File | Responsibility |
|---|---|
| `charter/limits.json` (modify) | Add `claude_daily_token_cap`, `codex_daily_token_cap`, `agent_timeout_s`, `check_timeout_s`, `ai_check_ttl_h` |
| `core/agents.py` (create) | `AgentResult`, `launch()`, `ClaudeAgent`, `CodexAgent`, `FakeAgent`, `load_limits()` |
| `core/usage.py` (create) | `Meter` with per-provider daily totals |
| `core/readiness.py` (create) | `CHECKS`, `run_checks()`, CLI `python -m core.readiness` |
| `.gitignore` (modify) | add `state/` |
| `tests/core/test_agents.py`, `test_usage.py`, `test_readiness.py` (create) | unit tests |
| `.github/workflows/core-checks.yml` (modify) | also run `python -m unittest discover -s tests/core` |

---

### Task 1: Process launcher and agent adapters

**Files:** Create `core/agents.py`, `tests/core/__init__.py` (empty), `tests/core/test_agents.py`. Modify `charter/limits.json`, `.gitignore`.

**Interfaces:**
- Produces:
  - `AgentResult(text: str, tokens: int, ok: bool, error: str | None = None, data: dict | None = None, provider: str = "")`
  - `launch(args: list[str], cwd: Path, stdin_text: str, timeout_s: int) -> tuple[int, str, str]`: exit code, stdout, stderr. Raises `TimeoutError` after killing the tree.
  - `ClaudeAgent(timeout_s=1800, permission_mode="acceptEdits", allowed_tools=None, cmd=None).run(prompt: str, cwd: Path, schema: dict | None = None) -> AgentResult`
  - `CodexAgent(timeout_s=1800, sandbox="read-only", cmd=None).run(prompt: str, cwd: Path, schema: dict | None = None) -> AgentResult`
  - `FakeAgent(script).run(prompt, cwd, schema=None) -> AgentResult`, where `script(prompt, cwd) -> (text, tokens)`
  - `load_limits(forge_root: Path) -> dict`

- [ ] **Step 1: Write `charter/limits.json` and `.gitignore` entry**

```json
{
  "claude_daily_token_cap": 10000000,
  "codex_daily_token_cap": 10000000,
  "agent_timeout_s": 1800,
  "check_timeout_s": 60,
  "ai_check_ttl_h": 6,
  "test_timeout_s": 600,
  "mutation_min": 0.8,
  "max_parallel_idle": 3
}
```
Append `state/` to `.gitignore`.

- [ ] **Step 2: Codex writes the failing tests** (the prompt to Codex is this step's text plus the Interfaces block)

```python
# tests/core/test_agents.py
import json, os, sys, tempfile, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.agents import (AgentResult, ClaudeAgent, CodexAgent, FakeAgent, launch, load_limits,
                         parse_claude, parse_codex)

PY = sys.executable

class LaunchTests(unittest.TestCase):
    def test_stdin_reaches_process(self):
        code, out, _ = launch([PY, "-c", "import sys; print(sys.stdin.read().upper())"], Path("."), "hi", 30)
        self.assertEqual((code, out.strip()), (0, "HI"))

    def test_long_prompt_via_stdin(self):
        big = "x" * 50_000
        code, out, _ = launch([PY, "-c", "import sys; print(len(sys.stdin.read()))"], Path("."), big, 30)
        self.assertEqual(out.strip(), "50000")

    def test_timeout_kills_tree(self):
        d = Path(tempfile.mkdtemp()); flag = d / "child_alive"
        child = f"import time,pathlib; time.sleep(3); pathlib.Path(r'{flag}').write_text('x')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
        with self.assertRaises(TimeoutError):
            launch([PY, "-c", parent], Path("."), "", 1)
        time.sleep(4)
        self.assertFalse(flag.exists(), "child process survived the timeout")

class ClaudeParseTests(unittest.TestCase):
    def test_success_counts_all_tokens(self):
        out = json.dumps({"is_error": False, "result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}})
        r = parse_claude(out, None)
        self.assertEqual((r.ok, r.text, r.tokens, r.provider), (True, "ok", 20, "claude"))

    def test_not_logged_in(self):
        r = parse_claude(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}), None)
        self.assertFalse(r.ok); self.assertIn("Not logged in", r.error)

    def test_claude_garbage(self):
        r = parse_claude("Traceback: boom", None)
        self.assertFalse(r.ok); self.assertEqual(r.tokens, 0)

    def test_schema_ok(self):
        schema = {"type": "object", "required": ["verdict"]}
        r = parse_claude(json.dumps({"is_error": False, "result": 'Here: {"verdict": "pass"}', "usage": {}}), schema)
        self.assertTrue(r.ok); self.assertEqual(r.data, {"verdict": "pass"})

    def test_schema_mismatch(self):
        schema = {"type": "object", "required": ["verdict"]}
        r = parse_claude(json.dumps({"is_error": False, "result": "I think it passes", "usage": {}}), schema)
        self.assertFalse(r.ok); self.assertIn("required shape", r.error); self.assertIn("passes", r.text)

class CodexParseTests(unittest.TestCase):
    def events(self, *evs): return "\n".join(json.dumps(e) for e in evs)

    def test_success(self):
        out = self.events({"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                          {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80,
                                                               "output_tokens": 5, "reasoning_output_tokens": 7}})
        r = parse_codex(0, out, "ok", None)
        self.assertEqual((r.ok, r.text, r.tokens, r.provider), (True, "ok", 112, "codex"))

    def test_codex_no_turn_completed(self):
        r = parse_codex(1, self.events({"type": "thread.started"}), "", None)
        self.assertFalse(r.ok); self.assertEqual(r.tokens, 0)

    def test_codex_schema(self):
        out = self.events({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        r = parse_codex(0, out, '{"verdict": "fail", "reasons": ["x"]}', {"type": "object", "required": ["verdict"]})
        self.assertTrue(r.ok); self.assertEqual(r.data["verdict"], "fail")

class AdapterTests(unittest.TestCase):
    def test_missing_cli_is_failed_result(self):
        for a in (ClaudeAgent(cmd=["no-such-binary-xyz"]), CodexAgent(cmd=["no-such-binary-xyz"])):
            r = a.run("hi", Path("."))
            self.assertFalse(r.ok); self.assertIn("not found", r.error)

    def test_fake_agent(self):
        r = FakeAgent(lambda p, c: ('{"verdict": "pass"}', 7)).run("x", Path("."), {"type": "object", "required": ["verdict"]})
        self.assertTrue(r.ok); self.assertEqual((r.tokens, r.data), (7, {"verdict": "pass"}))

    def test_fake_agent_exception(self):
        def boom(p, c): raise RuntimeError("crash")
        r = FakeAgent(boom).run("x", Path("."))
        self.assertFalse(r.ok); self.assertIn("crash", r.error)

    def test_limits(self):
        lim = load_limits(ROOT)
        self.assertGreater(lim["claude_daily_token_cap"], 0); self.assertGreater(lim["codex_daily_token_cap"], 0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure.** Run `python -m unittest tests/core/test_agents.py -v`. Expected: FAIL, `No module named 'core.agents'`.

- [ ] **Step 4: Claude implements `core/agents.py`**

```python
"""The only place Forge calls AI. Every call is a hidden, low-priority, time-limited
background process; prompts go in on stdin; results come back as AgentResult (never raises)."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

IS_WIN = os.name == "nt"


@dataclass
class AgentResult:
    text: str
    tokens: int
    ok: bool
    error: str | None = None
    data: dict | None = None
    provider: str = ""


def load_limits(forge_root: Path) -> dict:
    return json.loads((Path(forge_root) / "charter" / "limits.json").read_text(encoding="utf-8"))


def launch(args: list[str], cwd: Path, stdin_text: str, timeout_s: int) -> tuple[int, str, str]:
    """Run hidden and low-priority; on timeout kill the whole process tree and raise TimeoutError."""
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:
        kw["start_new_session"] = True
        kw["preexec_fn"] = lambda: os.nice(10)
    p = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", **kw)
    try:
        out, err = p.communicate(stdin_text, timeout=timeout_s)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise TimeoutError(f"agent timed out after {timeout_s}s")


def _resolve(cmd: list[str]) -> list[str] | None:
    exe = shutil.which(cmd[0])
    return [exe, *cmd[1:]] if exe else None


def _extract_json(text: str) -> dict | None:
    for m in re.finditer(r"\{.*\}", text, re.S):
        try:
            v = json.loads(m.group(0))
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


def _shape_ok(data: dict | None, schema: dict | None) -> bool:
    if schema is None:
        return True
    if not isinstance(data, dict):
        return False
    return all(k in data for k in schema.get("required", []))


def _finish(provider: str, text: str, tokens: int, schema: dict | None) -> AgentResult:
    if schema is None:
        return AgentResult(text, tokens, True, None, None, provider)
    data = _extract_json(text)
    if not _shape_ok(data, schema):
        return AgentResult(text, tokens, False, "output did not match the required shape", None, provider)
    return AgentResult(text, tokens, True, None, data, provider)


def parse_claude(out: str, schema: dict | None) -> AgentResult:
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except (json.JSONDecodeError, IndexError):
        data = None
    if not isinstance(data, dict):
        return AgentResult(out[-500:], 0, False, "unreadable output from Claude Code", None, "claude")
    u = data.get("usage") or {}
    tokens = sum(int(u.get(k) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    text = str(data.get("result") or "")
    if data.get("is_error"):
        return AgentResult(text, tokens, False, text or "Claude Code reported an error", None, "claude")
    return _finish("claude", text, tokens, schema)


def parse_codex(code: int, events_out: str, last_message: str, schema: dict | None) -> AgentResult:
    tokens, done = 0, False
    for line in events_out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "reasoning_output_tokens"))
            done = True
    if code != 0 or not done or not last_message.strip():
        return AgentResult(last_message, tokens, False, f"Codex run failed (exit {code})", None, "codex")
    return _finish("codex", last_message.strip(), tokens, schema)


class ClaudeAgent:
    def __init__(self, timeout_s: int = 1800, permission_mode: str = "acceptEdits",
                 allowed_tools: list[str] | None = None, cmd: list[str] | None = None):
        self.timeout_s, self.permission_mode, self.allowed_tools = timeout_s, permission_mode, allowed_tools
        self.cmd = cmd or ["claude"]

    def run(self, prompt: str, cwd: Path, schema: dict | None = None) -> AgentResult:
        base = _resolve(self.cmd)
        if not base:
            return AgentResult("", 0, False, f"agent command not found: {self.cmd[0]}", None, "claude")
        args = base + ["-p", "--output-format", "json", "--permission-mode", self.permission_mode]
        if self.allowed_tools:
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        if schema:
            prompt += "\n\nAnswer with ONLY a JSON object with these keys: " + ", ".join(schema.get("required", []))
        try:
            _, out, _ = launch(args, cwd, prompt, self.timeout_s)
        except TimeoutError as e:
            return AgentResult("", 0, False, str(e), None, "claude")
        return parse_claude(out, schema)


class CodexAgent:
    def __init__(self, timeout_s: int = 1800, sandbox: str = "read-only", cmd: list[str] | None = None):
        self.timeout_s, self.sandbox = timeout_s, sandbox
        self.cmd = cmd or ["codex"]

    def run(self, prompt: str, cwd: Path, schema: dict | None = None) -> AgentResult:
        base = _resolve(self.cmd)
        if not base:
            return AgentResult("", 0, False, f"agent command not found: {self.cmd[0]}", None, "codex")
        tmp = Path(tempfile.mkdtemp(prefix="forge-codex-"))
        last = tmp / "last.txt"
        args = base + ["exec", "--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check",
                       "-s", self.sandbox, "-C", str(cwd), "-o", str(last)]
        if schema:
            (tmp / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            args += ["--output-schema", str(tmp / "schema.json")]
        args += ["-"]
        try:
            code, out, _ = launch(args, cwd, prompt, self.timeout_s)
        except TimeoutError as e:
            return AgentResult("", 0, False, str(e), None, "codex")
        finally:
            text = last.read_text(encoding="utf-8") if last.exists() else ""
            shutil.rmtree(tmp, ignore_errors=True)
        return parse_codex(code, out, text, schema)


class FakeAgent:
    """For drills and tests. script(prompt, cwd) -> (text, tokens); exceptions become failed results."""

    def __init__(self, script: Callable[[str, Path], tuple[str, int]], provider: str = "fake"):
        self.script, self.provider, self.prompts = script, provider, []

    def run(self, prompt: str, cwd: Path, schema: dict | None = None) -> AgentResult:
        self.prompts.append(prompt)
        try:
            text, tokens = self.script(prompt, Path(cwd))
        except Exception as e:  # noqa: BLE001
            return AgentResult("", 0, False, repr(e), None, self.provider)
        return _finish(self.provider, text, tokens, schema)
```

- [ ] **Step 5: Run tests.** Run `python -m unittest tests/core/test_agents.py -v`. Expected: 15 tests OK, on the PC (Windows) and in CI (Linux).

- [ ] **Step 6: Judges.** `python drills/run_drills.py` shows ALL DRILLS PASS. Mutation check: delete the `taskkill` / `killpg` line, confirm `test_timeout_kills_tree` fails, restore. Delete the `_shape_ok` check in `_finish`, confirm `test_schema_mismatch` fails, restore.

- [ ] **Step 7: Codex reviews** the diff against this task and `docs/PURPOSE.md` (read-only, `review.schema.json`). Fix and repeat until it passes.

- [ ] **Step 8: Commit** `git commit -m "1A: hidden low-priority agent launcher; Claude and Codex adapters"`

---

### Task 2: Two-provider usage meter

**Files:** Create `core/usage.py`, `tests/core/test_usage.py`.

**Interfaces:**
- Consumes: `load_limits`
- Produces:
  - `Meter(state_dir: Path, clock=None)` with `.add(provider: str, tokens: int)`, `.used_today(provider: str) -> int`, `.over(provider: str, limits: dict) -> bool`
  - caps read as `limits[f"{provider}_daily_token_cap"]`; providers without a cap (e.g. `fake`) are never over

- [ ] **Step 1: Codex writes the failing tests**

```python
# tests/core/test_usage.py
import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.usage import Meter

LIM = {"claude_daily_token_cap": 1000, "codex_daily_token_cap": 500}

class MeterTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.now = [datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)]
        self.m = lambda: Meter(self.d, clock=lambda: self.now[0])

    def test_providers_independent(self):
        m = self.m(); m.add("codex", 600)
        self.assertTrue(m.over("codex", LIM)); self.assertFalse(m.over("claude", LIM))

    def test_persists_across_instances(self):
        self.m().add("claude", 400); self.m().add("claude", 700)
        self.assertEqual(self.m().used_today("claude"), 1100); self.assertTrue(self.m().over("claude", LIM))

    def test_utc_rollover(self):
        self.m().add("claude", 5000)
        self.now[0] += timedelta(hours=1)
        self.assertEqual(self.m().used_today("claude"), 0); self.assertFalse(self.m().over("claude", LIM))

    def test_uncapped_provider_never_over(self):
        m = self.m(); m.add("fake", 10**9); self.assertFalse(m.over("fake", LIM))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError): self.m().add("claude", -1)

    def test_corrupt_file_starts_fresh_not_crash(self):
        (self.d / "meter.json").write_text("{not json")
        self.m().add("claude", 5); self.assertEqual(self.m().used_today("claude"), 5)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure.** Expected: `No module named 'core.usage'`.

- [ ] **Step 3: Claude implements `core/usage.py`**

```python
"""Forge-wide daily token meter per provider (UTC days). Written only by plain code."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class Meter:
    def __init__(self, state_dir: Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(state_dir) / "meter.json"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def used_today(self, provider: str) -> int:
        return int(self._read().get(self._day(), {}).get(provider, 0))

    def add(self, provider: str, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        data = self._read()
        day = data.setdefault(self._day(), {})
        day[provider] = int(day.get(provider, 0)) + tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def over(self, provider: str, limits: dict) -> bool:
        cap = limits.get(f"{provider}_daily_token_cap")
        return cap is not None and self.used_today(provider) >= cap
```

- [ ] **Step 4: Run tests.** Expected: 6 tests OK.
- [ ] **Step 5: Judges and Codex review** (as in Task 1). Mutation: make `over` ignore the provider; `test_providers_independent` must fail.
- [ ] **Step 6: Commit** `git commit -m "1A: per-provider daily usage meter"`

---

### Task 3: Readiness check and capability map

**Files:** Create `core/readiness.py`, `tests/core/test_readiness.py`. Modify `.github/workflows/core-checks.yml`.

**Interfaces:**
- Consumes: `ClaudeAgent`, `CodexAgent`, `load_limits`
- Produces:
  - `run_checks(state_dir: Path, checks: dict[str, Callable[[], tuple[bool, str]]], timeout_s: int, clock=None, ttl: dict[str, float] | None = None) -> dict`, which writes `state/capabilities.json` as `{name: {"ok": bool, "detail": str, "checked_at": iso}}`. Checks with a TTL, like the AI checks, are reused if still fresh and ok.
  - `CHECKS: dict[str, Callable]`, the real checks
  - `python -m core.readiness` prints a table; exits 0 if everything is ok, else 1

- [ ] **Step 1: Codex writes the failing tests**

```python
# tests/core/test_readiness.py
import json, sys, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.readiness import run_checks, CHECKS

class ReadinessTests(unittest.TestCase):
    def setUp(self): self.d = Path(tempfile.mkdtemp())

    def test_writes_map(self):
        m = run_checks(self.d, {"a": lambda: (True, "fine"), "b": lambda: (False, "down")}, 5)
        saved = json.loads((self.d / "capabilities.json").read_text())
        self.assertEqual(saved["a"]["ok"], True); self.assertEqual(saved["b"]["detail"], "down")
        self.assertIn("checked_at", saved["a"]); self.assertEqual(m, saved)

    def test_check_exception_isolated(self):
        def boom(): raise RuntimeError("dns down")
        m = run_checks(self.d, {"x": boom, "y": lambda: (True, "ok")}, 5)
        self.assertFalse(m["x"]["ok"]); self.assertIn("dns down", m["x"]["detail"]); self.assertTrue(m["y"]["ok"])

    def test_check_timeout(self):
        m = run_checks(self.d, {"slow": lambda: (time.sleep(5), (True, ""))[1], "fast": lambda: (True, "")}, 1)
        self.assertFalse(m["slow"]["ok"]); self.assertIn("timed out", m["slow"]["detail"]); self.assertTrue(m["fast"]["ok"])

    def test_ttl_reuses_fresh_ok_results(self):
        calls = []
        def ai(): calls.append(1); return (True, "ok")
        now = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
        run_checks(self.d, {"ai": ai}, 5, clock=lambda: now[0], ttl={"ai": 6})
        now[0] += timedelta(hours=2); run_checks(self.d, {"ai": ai}, 5, clock=lambda: now[0], ttl={"ai": 6})
        self.assertEqual(len(calls), 1)
        now[0] += timedelta(hours=5); run_checks(self.d, {"ai": ai}, 5, clock=lambda: now[0], ttl={"ai": 6})
        self.assertEqual(len(calls), 2)

    def test_failed_results_never_reused(self):
        calls = []
        def ai(): calls.append(1); return (False, "not logged in")
        run_checks(self.d, {"ai": ai}, 5, ttl={"ai": 6}); run_checks(self.d, {"ai": ai}, 5, ttl={"ai": 6})
        self.assertEqual(len(calls), 2)

    def test_real_check_names(self):
        for name in ["git", "github", "claude", "codex", "gmail", "docker", "n8n", "ollama", "python_libs", "browser"]:
            self.assertIn(name, CHECKS)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure.** Expected: `No module named 'core.readiness'`.

- [ ] **Step 3: Claude implements `core/readiness.py`**

```python
"""Readiness check: which of Forge's connections actually work right now.

Writes state/capabilities.json. Every agent reads it; blocker claims that contradict it
are rejected (D-030, D-031). Secrets are touched only here, never shown to agents."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

FORGE_ROOT = Path(__file__).resolve().parent.parent
STATE = FORGE_ROOT / "state"
GMAIL = "benjaminanderson0802@gmail.com"
NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _cmd(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    exe = shutil.which(args[0])
    if not exe:
        return False, f"{args[0]} not installed or not on PATH"
    p = subprocess.run([exe, *args[1:]], capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, **NOWIN)
    return p.returncode == 0, (p.stdout or p.stderr).strip().splitlines()[0][:200] if (p.stdout or p.stderr) else ""


def _http(url: str) -> tuple[bool, str]:
    with urllib.request.urlopen(url, timeout=10) as r:
        return 200 <= r.status < 300, f"HTTP {r.status}"


def _ai(agent) -> tuple[bool, str]:
    from core.agents import load_limits
    r = agent.run("Reply with exactly: ok", Path.home())
    return (r.ok and r.text.strip().lower().startswith("ok")), (r.error or f"ok ({r.tokens} tokens)")


def _gmail() -> tuple[bool, str]:
    import keyring, smtplib
    pw = keyring.get_password("forge-gmail", GMAIL)
    if not pw:
        return False, "no app password in Windows Credential Manager (forge-gmail)"
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
    try:
        s.login(GMAIL, pw)
    finally:
        s.quit()
    return True, f"SMTP login ok for {GMAIL}"


def _libs() -> tuple[bool, str]:
    missing = []
    for m in ["requests", "yaml", "httpx", "pandas", "pdfplumber", "keyring", "playwright"]:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    return (not missing), ("all present" if not missing else "missing: " + ", ".join(missing))


def _browser() -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        b.close()
    return True, "hidden Chromium launches"


def _claude():
    from core.agents import ClaudeAgent
    return _ai(ClaudeAgent(timeout_s=120, permission_mode="plan"))


def _codex():
    from core.agents import CodexAgent
    return _ai(CodexAgent(timeout_s=120))


CHECKS: dict[str, Callable[[], tuple[bool, str]]] = {
    "git": lambda: _cmd(["git", "--version"]),
    "github": lambda: _cmd(["gh", "auth", "status"]),
    "claude": _claude,
    "codex": _codex,
    "gmail": _gmail,
    "docker": lambda: _cmd(["docker", "info", "--format", "{{.ServerVersion}}"]),
    "n8n": lambda: _http("http://127.0.0.1:5678/healthz"),
    "ollama": lambda: _http("http://127.0.0.1:11434/api/tags"),
    "python_libs": _libs,
    "browser": _browser,
}
AI_TTL = {"claude": 6, "codex": 6}


def run_checks(state_dir: Path, checks: dict, timeout_s: int, clock=None, ttl: dict | None = None) -> dict:
    clock = clock or (lambda: datetime.now(timezone.utc))
    path = Path(state_dir) / "capabilities.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}
    now = clock()
    result, todo = {}, {}
    for name, fn in checks.items():
        prev = old.get(name)
        hours = (ttl or {}).get(name)
        if prev and hours and prev.get("ok"):
            age = (now - datetime.fromisoformat(prev["checked_at"])).total_seconds() / 3600
            if age < hours:
                result[name] = prev
                continue
        todo[name] = fn
    pool = ThreadPoolExecutor(max_workers=max(1, len(todo)))
    futures = {name: pool.submit(fn) for name, fn in todo.items()}
    for name, fut in futures.items():
        try:
            ok, detail = fut.result(timeout=timeout_s)
        except FutTimeout:
            ok, detail = False, f"check timed out after {timeout_s}s"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"[:300]
        result[name] = {"ok": bool(ok), "detail": str(detail), "checked_at": now.isoformat()}
    pool.shutdown(wait=False, cancel_futures=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(result, indent=2, sort_keys=True).encode("utf-8"))
    return result


def main() -> int:
    from core.agents import load_limits
    lim = load_limits(FORGE_ROOT)
    m = run_checks(STATE, CHECKS, lim["check_timeout_s"], ttl={k: lim["ai_check_ttl_h"] for k in AI_TTL})
    for name, r in sorted(m.items()):
        print(f"{'OK  ' if r['ok'] else 'FAIL'}  {name:12} {r['detail']}")
    return 0 if all(r["ok"] for r in m.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests.** Expected: 6 tests OK.

- [ ] **Step 5: CI.** In `.github/workflows/core-checks.yml`, after "Sabotage drills", add:

```yaml
      - name: Core unit tests
        run: python -m unittest discover -s tests/core -v
```

- [ ] **Step 6: Gate: the real map on the PC.** Run `python -m core.readiness` in `C:\Users\benja\Forge`. Expected: all 10 lines are OK. For any FAIL, the Troubleshooter fixes it, or its exact fix goes to Ben's queue. The gate passes only when every line shows OK, or is a documented, Ben-accepted exception.

- [ ] **Step 7: Judges, Codex review, commit** `git commit -m "1A: readiness check and capability map"`, then open the layer-branch PR per D-027 (merged only at the Layer 1 gate).

---

## Self-review notes

- **Spec coverage:** §1 launcher, adapters and usage → Tasks 1 and 2. §4 readiness and capability map → Task 3. §7 1A gate → Task 3 Step 6. Blocker validation against the map belongs to 1B, which consumes `capabilities.json`.
- **Types:** `AgentResult.data` is used by 1B's roles. `Meter.add(provider, tokens)` takes the provider from `AgentResult.provider`.
- **Placeholders:** none. The first-draft limits values (10M tokens per provider per day) are real starting numbers, tuned by the Learner with Ben's approval (D-026).
