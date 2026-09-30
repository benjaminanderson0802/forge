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
import threading
import time
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


class Stopped(TimeoutError):
    """T1D3: the run was stopped because KILL appeared. A TimeoutError, so every agent turns it into a failed
    result; the conductor recognises it and records nothing (a stop is never a failure)."""


_LIVE: set = set()
_LIVE_LOCK = threading.Lock()


def _kill_tree(p: subprocess.Popen) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def live_count() -> int:
    with _LIVE_LOCK:
        return len(_LIVE)


def kill_live() -> None:
    """T1D3: kill every agent process tree this process started (used before a stall exit)."""
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        _kill_tree(p)


def launch(args: list[str], cwd: Path, stdin_text: str, timeout_s: int,
           should_stop: Callable[[], bool] | None = None, poll_s: float = 2.0) -> tuple[int, str, str]:
    """Run hidden and low-priority; on timeout kill the whole process tree and raise TimeoutError.
    T1D3: with should_stop, it is checked every poll_s seconds; when it returns True the tree is killed and
    Stopped is raised."""
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:
        kw["start_new_session"] = True
        kw["preexec_fn"] = lambda: os.nice(10)
    p = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", **kw)
    with _LIVE_LOCK:
        _LIVE.add(p)
    try:
        deadline = time.monotonic() + timeout_s
        first = True
        while True:
            left = deadline - time.monotonic()
            wait = left if should_stop is None else min(left, poll_s)
            try:
                # after a timed-out wait, communicate() resumes where it was; input may only be passed once
                out, err = p.communicate(stdin_text if first else None, timeout=max(wait, 0.01))
                return p.returncode, out, err
            except subprocess.TimeoutExpired:
                first = False
                if time.monotonic() >= deadline:
                    _kill_tree(p)
                    p.communicate()
                    raise TimeoutError(f"agent timed out after {timeout_s}s")
                if should_stop is not None and should_stop():
                    _kill_tree(p)
                    p.communicate()
                    raise Stopped("agent stopped: KILL is set")
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(p)


def _resolve(cmd: list[str]) -> list[str] | None:
    exe = shutil.which(cmd[0])
    return [exe, *cmd[1:]] if exe else None


def strict_schema(schema: dict) -> dict:
    """R17/R30: the strict form Codex's structured output requires. Every object lists all its properties as
    required and forbids extras; properties that weren't required become nullable (enums gain null too).
    anyOf, $defs and definitions are converted recursively. Never mutates the input."""
    def conv(node, optional: bool = False):
        if not isinstance(node, dict):
            return node
        n = dict(node)
        if n.get("type") == "object" or "properties" in n:
            props = n.get("properties", {})
            req = set(n.get("required", []))
            n["properties"] = {k: conv(v, k not in req) for k, v in props.items()}
            n["required"] = list(props)
            n["additionalProperties"] = False
            n.setdefault("type", "object")
        if "items" in n:
            n["items"] = conv(n["items"])
        if isinstance(n.get("anyOf"), list):
            n["anyOf"] = [conv(x) for x in n["anyOf"]]
        for key in ("$defs", "definitions"):
            if isinstance(n.get(key), dict):
                n[key] = {k: conv(v) for k, v in n[key].items()}
        if optional:
            t = n.get("type")
            if isinstance(t, str) and t != "null":
                n["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                n["type"] = [*t, "null"]
            if isinstance(n.get("enum"), list) and None not in n["enum"]:
                n["enum"] = [*n["enum"], None]
            if isinstance(n.get("anyOf"), list) and not any(x.get("type") == "null" for x in n["anyOf"]
                                                           if isinstance(x, dict)):
                n["anyOf"] = [*n["anyOf"], {"type": "null"}]
        return n
    return conv(schema)


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def schema_ok(data, schema: dict | None) -> bool:
    """R29: full validation for the schema forms Forge uses: type, enum, required, properties, items, anyOf."""
    if schema is None:
        return True
    if isinstance(schema.get("anyOf"), list):
        return any(schema_ok(data, s) for s in schema["anyOf"])
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        def is_type(name):
            if name == "integer":
                return isinstance(data, int) and not isinstance(data, bool)
            if name == "number":
                return isinstance(data, (int, float)) and not isinstance(data, bool)
            return name in _TYPES and isinstance(data, _TYPES[name])
        if not any(is_type(x) for x in types):
            return False
    if "enum" in schema and data not in schema["enum"]:
        return False
    if isinstance(data, dict):
        if any(k not in data for k in schema.get("required", [])):
            return False
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and any(k not in props for k in data):
            return False
        return all(schema_ok(data[k], sub) for k, sub in props.items() if k in data)
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        return all(schema_ok(x, schema["items"]) for x in data)
    return True


def _codex_error(events_out: str) -> str:
    """R17: Codex's own reason for a failed run, from its JSON events."""
    msgs = []
    for line in events_out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "turn.failed":
            msgs.append(str((ev.get("error") or {}).get("message", "")))
        elif ev.get("type") == "error":
            msgs.append(str(ev.get("message", "")))
    return " | ".join(m for m in msgs if m)[:1500]


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


def drop_null_optionals(data, schema: dict | None):
    """R34: a null value for a key the schema doesn't require counts as missing (Codex's strict form makes
    optional fields nullable). Recursive through nested objects and array items."""
    if not isinstance(schema, dict):
        return data
    if isinstance(data, dict):
        req = set(schema.get("required", []))
        props = schema.get("properties", {})
        return {k: drop_null_optionals(v, props.get(k)) for k, v in data.items() if not (v is None and k not in req)}
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        return [drop_null_optionals(x, schema["items"]) for x in data]
    return data


def _finish(provider: str, text: str, tokens: int, schema: dict | None) -> AgentResult:
    if schema is None:
        return AgentResult(text, tokens, True, None, None, provider)
    data = drop_null_optionals(_extract_json(text), schema)
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
        why = _codex_error(events_out)
        return AgentResult(last_message, tokens, False, f"Codex run failed (exit {code})" + (f": {why}" if why else ""),
                           None, "codex")
    return _finish("codex", last_message.strip(), tokens, schema)


def _stop_kw(agent) -> dict:
    return {"should_stop": agent.should_stop} if agent.should_stop is not None else {}


class ClaudeAgent:
    provider = "claude"  # R6/R29: the id the Meter and the token caps use
    should_stop: Callable[[], bool] | None = None  # T1D3: set by the conductor to its KILL check

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
            _, out, _ = launch(args, cwd, prompt, self.timeout_s, **_stop_kw(self))
        except TimeoutError as e:
            return AgentResult("", 0, False, str(e), None, "claude")
        return parse_claude(out, schema)


class CodexAgent:
    provider = "codex"  # R6/R29: the id the Meter and the token caps use
    should_stop: Callable[[], bool] | None = None  # T1D3: set by the conductor to its KILL check

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
        if IS_WIN:  # R16: --ignore-user-config drops the Windows sandbox; without it writes silently fail
            args += ["-c", 'windows.sandbox="elevated"']
        if schema:
            (tmp / "schema.json").write_text(json.dumps(strict_schema(schema)), encoding="utf-8")
            args += ["--output-schema", str(tmp / "schema.json")]
        args += ["-"]
        try:
            code, out, _ = launch(args, cwd, prompt, self.timeout_s, **_stop_kw(self))
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
