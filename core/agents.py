"""The only place Forge calls AI. Every call is a hidden, low-priority, time-limited
background process; prompts go in on stdin; results come back as AgentResult (never raises)."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
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


def _n(v) -> int:
    """R43: a usage field as a count. Anything that can't be read as a finite non-negative number (numbers and
    numeric strings can) counts as 0, so a bad, negative or infinite field can never cancel out real usage."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):  # exact, any size
        return v if v > 0 else 0
    if isinstance(v, str) and v.strip().isdecimal():  # isdecimal: "²" is a digit but not a number
        try:
            return int(v.strip())
        except ValueError:
            return 0
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return 0
    return int(f)


def parse_claude(out: str, schema: dict | None) -> AgentResult:
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except (json.JSONDecodeError, IndexError):
        data = None
    if not isinstance(data, dict):
        return AgentResult(out[-500:], 0, False, "unreadable output from Claude Code", None, "claude")
    u = data.get("usage") or {}
    tokens = sum(_n(u.get(k)) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")) \
        + _n(u.get("cache_read_input_tokens")) // 10  # R43: cache reads cost about a tenth
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
            inp = _n(u.get("input_tokens"))
            cached = min(_n(u.get("cached_input_tokens")), inp)  # R43: clamped to 0..input
            tokens = (inp - cached) + cached // 10 + _n(u.get("output_tokens")) + _n(u.get("reasoning_output_tokens"))
            done = True
    if code != 0 or not done or not last_message.strip():
        why = _codex_error(events_out)
        return AgentResult(last_message, tokens, False, f"Codex run failed (exit {code})" + (f": {why}" if why else ""),
                           None, "codex")
    return _finish("codex", last_message.strip(), tokens, schema)


def claude_log_tokens(session_id: str, projects_dir: Path) -> int:
    """R46: a Claude run's use, read from Claude Code's session log (R43 weighting, each message id once)."""
    seen, total = set(), 0
    for f in Path(projects_dir).glob(f"*/{session_id}.jsonl"):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = e.get("message") if isinstance(e, dict) else None
            if not isinstance(m, dict):
                continue
            mid, u = m.get("id"), m.get("usage")
            if not isinstance(mid, str) or not mid or not isinstance(u, dict) or mid in seen:
                continue
            seen.add(mid)
            total += sum(_n(u.get(k)) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")) \
                + _n(u.get("cache_read_input_tokens")) // 10
    return total


def _deadline(own: int, timeout_s: float | None) -> int:
    """T1C4: a caller's deadline can only shorten an agent's own timeout (whole seconds, at least 1)."""
    if timeout_s is None:
        return own
    return max(1, min(int(own), int(math.ceil(float(timeout_s)))))


class ClaudeAgent:
    provider = "claude"  # R6/R29: the id the Meter and the token caps use

    def __init__(self, timeout_s: int = 1800, permission_mode: str = "acceptEdits",
                 allowed_tools: list[str] | None = None, cmd: list[str] | None = None,
                 projects_dir: Path | None = None):
        self.timeout_s, self.permission_mode, self.allowed_tools = timeout_s, permission_mode, allowed_tools
        self.cmd = cmd or ["claude"]
        self.projects_dir = Path(projects_dir) if projects_dir else Path.home() / ".claude" / "projects"  # R46

    def run(self, prompt: str, cwd: Path, schema: dict | None = None, timeout_s: float | None = None) -> AgentResult:
        base = _resolve(self.cmd)
        if not base:
            return AgentResult("", 0, False, f"agent command not found: {self.cmd[0]}", None, "claude")
        sid = str(uuid.uuid4())  # R46: known up front, so a failed run can still be metered from its log
        args = base + ["-p", "--output-format", "json", "--permission-mode", self.permission_mode, "--session-id", sid]
        if self.allowed_tools:
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        if schema:
            prompt += "\n\nAnswer with ONLY a JSON object with these keys: " + ", ".join(schema.get("required", []))
        try:
            _, out, _ = launch(args, cwd, prompt, _deadline(self.timeout_s, timeout_s))
        except TimeoutError as e:
            return AgentResult("", claude_log_tokens(sid, self.projects_dir), False, str(e), None, "claude")
        r = parse_claude(out, schema)
        if not r.ok and not r.tokens:  # R46: unreadable output still costs what the log says
            r.tokens = claude_log_tokens(sid, self.projects_dir)
        return r


class CodexAgent:
    provider = "codex"  # R6/R29: the id the Meter and the token caps use

    def __init__(self, timeout_s: int = 1800, sandbox: str = "read-only", cmd: list[str] | None = None):
        self.timeout_s, self.sandbox = timeout_s, sandbox
        self.cmd = cmd or ["codex"]

    def run(self, prompt: str, cwd: Path, schema: dict | None = None, timeout_s: float | None = None) -> AgentResult:
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
            code, out, _ = launch(args, cwd, prompt, _deadline(self.timeout_s, timeout_s))
        except TimeoutError as e:
            return AgentResult("", 0, False, str(e), None, "codex")
        finally:
            text = last.read_text(encoding="utf-8") if last.exists() else ""
            shutil.rmtree(tmp, ignore_errors=True)
        return parse_codex(code, out, text, schema)


class FakeAgent:
    """For drills and tests. script(prompt, cwd) -> (text, tokens); exceptions become failed results."""

    def __init__(self, script: Callable[[str, Path], tuple[str, int]], provider: str = "fake"):
        self.script, self.provider, self.prompts, self.timeouts = script, provider, [], []

    def run(self, prompt: str, cwd: Path, schema: dict | None = None, timeout_s: float | None = None) -> AgentResult:
        self.prompts.append(prompt)
        self.timeouts.append(timeout_s)
        try:
            text, tokens = self.script(prompt, Path(cwd))
        except Exception as e:  # noqa: BLE001
            return AgentResult("", 0, False, repr(e), None, self.provider)
        return _finish(self.provider, text, tokens, schema)
