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
