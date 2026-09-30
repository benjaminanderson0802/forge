"""Readiness check: which of Forge's connections actually work right now.

Writes state/capabilities.json. Every agent reads it; blocker claims that contradict it
are rejected (D-030, D-031). Secrets are touched only here, never shown to agents.

Timeouts: process-spawning checks are responsible for their own tree cleanup via launch();
the outer deadline is a reporting deadline; main() waits a bounded grace so that cleanup can
finish. Checks run in daemon threads, so a hung pure-Python check can never block exit."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

FORGE_ROOT = Path(__file__).resolve().parent.parent
STATE = FORGE_ROOT / "state"
GMAIL = "benjaminanderson0802@gmail.com"
INNER_MARGIN_S = 15
REAP_GRACE_S = INNER_MARGIN_S + 5
DETAIL_MAX = 300

_stragglers: list[threading.Thread] = []


def _inner() -> int:
    """Per-check budget for anything that starts a process. Kept below check_timeout_s so the
    check's own tree kill runs before the outer deadline. This intentionally deviates from the
    plan's 120s for the AI checks, which would exceed check_timeout_s (60)."""
    from core.agents import load_limits
    return max(5, int(load_limits(FORGE_ROOT)["check_timeout_s"]) - INNER_MARGIN_S)


def _cmd(args: list[str]) -> tuple[bool, str]:
    from core.agents import launch
    exe = shutil.which(args[0])
    if not exe:
        return False, f"{args[0]} not installed or not on PATH"
    inner = _inner()
    try:
        code, out, err = launch([exe, *args[1:]], Path.home(), "", inner)
    except TimeoutError:
        return False, f"{args[0]} timed out after {inner}s"
    text = (out or err or "").strip()
    return code == 0, text.splitlines()[0][:200] if text else ""


def _http(url: str) -> tuple[bool, str]:
    with urllib.request.urlopen(url, timeout=10) as r:
        return 200 <= r.status < 300, f"HTTP {r.status}"


PROBE_PROMPT = "Reply with exactly: ok"


def probe_ok(result) -> tuple[bool, str]:
    """Judge an AI probe reply: ok only when the run succeeded and the text starts with 'ok'."""
    text = str(getattr(result, "text", "") or "")
    if getattr(result, "ok", False) and text.strip().lower().startswith("ok"):
        return True, f"ok ({getattr(result, 'tokens', 0)} tokens)"
    return False, (getattr(result, "error", None) or f"unexpected reply: {text[:100]}")


def _ai(agent) -> tuple[bool, str]:
    return probe_ok(agent.run(PROBE_PROMPT, Path.home()))


def _gmail() -> tuple[bool, str]:
    import keyring
    import smtplib
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
        b = p.chromium.launch(headless=True, timeout=_inner() * 1000)
        b.close()
    return True, "hidden Chromium launches"


def _claude():
    from core.agents import ClaudeAgent
    return _ai(ClaudeAgent(timeout_s=_inner(), permission_mode="plan"))


def _codex():
    from core.agents import CodexAgent
    return _ai(CodexAgent(timeout_s=_inner()))


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
AI_NAMES = ("claude", "codex")
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
PLAIN_CHECKS: dict[str, Callable[[], tuple[bool, str]]] = {
    k: fn for k, fn in CHECKS.items() if k not in AI_NAMES}


def _fresh(prev, hours, now: datetime) -> bool:
    """A cached result is reusable only if it was ok and its timestamp is valid and recent."""
    if not isinstance(prev, dict) or not hours or prev.get("ok") is not True:
        return False
    try:
        at = datetime.fromisoformat(prev["checked_at"])
        age_h = (now - at).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age_h < hours


def evaluate(checks: dict[str, Callable[[], tuple[bool, str]]], timeout_s: float, now: datetime) -> dict:
    """Run every check in its own daemon thread against one reporting deadline. No file I/O.
    An exception is a failure; a result reported after the deadline, or none at all, is a timeout."""
    lock = threading.Lock()
    reported: dict[str, tuple[float, bool, str]] = {}  # name -> (monotonic report time, ok, detail)

    def worker(name: str, fn: Callable) -> None:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        with lock:
            reported[name] = (time.monotonic(), bool(ok), str(detail))

    threads = {name: threading.Thread(target=worker, args=(name, fn), name=f"readiness-{name}", daemon=True)
               for name, fn in checks.items()}
    for t in threads.values():
        t.start()
    deadline = time.monotonic() + timeout_s
    for t in threads.values():
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        snapshot = dict(reported)
    _stragglers[:] = [t for t in _stragglers if t.is_alive()] + [t for t in threads.values() if t.is_alive()]

    result = {}
    for name in checks:
        got = snapshot.get(name)
        if got is not None and got[0] <= deadline:  # a result reported after the deadline never counts
            _, ok, detail = got
        else:
            ok, detail = False, f"check timed out after {timeout_s}s"
        result[name] = {"ok": ok, "detail": detail[:DETAIL_MAX], "checked_at": now.isoformat()}
    return result


def read_map(path) -> dict:
    """The capability map at path, or {} if it is missing, unreadable, invalid JSON or not an object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_map(path, m: dict) -> None:
    """Write atomically: a temp file in the same folder, then os.replace. Sorted keys, UTF-8, LF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write((json.dumps(m, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_checks(state_dir: Path, checks: dict, timeout_s: int, clock=None, ttl: dict | None = None) -> dict:
    clock = clock or (lambda: datetime.now(timezone.utc))
    path = Path(state_dir) / "capabilities.json"
    old = read_map(path)
    now = clock()
    result, todo = {}, {}
    for name, fn in checks.items():
        prev = old.get(name)
        if _fresh(prev, (ttl or {}).get(name), now):
            result[name] = prev
        else:
            todo[name] = fn
    result.update(evaluate(todo, timeout_s, now))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(json.dumps(result, indent=2, sort_keys=True).encode("utf-8"))
    os.replace(tmp, path)
    return result


FUTURE_SLACK_S = 60


def broken(entry, now: datetime, max_age_s: float) -> str | None:
    """None only for usable ok evidence; otherwise a reason with a fixed prefix."""
    if entry is None:
        return "no evidence"
    if not isinstance(entry, dict) or not isinstance(entry.get("ok"), bool):
        return "malformed evidence"
    if entry["ok"] is False:
        return f"failing: {entry.get('detail', '')}"
    raw = entry.get("checked_at")
    if not isinstance(raw, str):
        return "bad checked_at: missing or not a string"
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return f"bad checked_at: unparsable {raw[:40]!r}"
    if at.tzinfo is None or at.utcoffset() is None:
        return "bad checked_at: no timezone"
    age = (now - at).total_seconds()
    if -age > FUTURE_SLACK_S:
        return f"evidence from the future ({-age:.0f}s ahead)"
    if age > max_age_s:
        return f"stale evidence ({age:.0f}s old, limit {max_age_s:.0f}s)"
    return None


def ok_ttl_s(name: str, limits: dict) -> float:
    if name in AI_NAMES:
        return float(limits.get("ai_check_ttl_h", 6)) * 3600
    if name in ("gmail", "browser"):
        return 1800.0
    return 0.0


def fail_retry_s(name: str, limits: dict) -> float:
    if name in AI_NAMES or name == "gmail":
        return float(limits.get("readiness_fail_retry_s", 900))
    return 0.0


def max_age_for(name: str, limits: dict) -> float:
    return max(float(limits.get("readiness_max_age_s", 900)), ok_ttl_s(name, limits))


def requirements(provider: str | None, needs=None) -> set[str]:
    """Capabilities a job needs. Unknown names are kept so they fail closed."""
    req = {"git"}
    if provider:
        req.add(provider)
    req.update(n for n in (needs or ()) if isinstance(n, str))
    return req


def probe_agents(limits: dict) -> dict:
    """Agents for the AI probes. Constructing them launches nothing."""
    from core.agents import ClaudeAgent, CodexAgent
    t = max(5, int(limits.get("check_timeout_s", 60)) - INNER_MARGIN_S)
    return {"claude": ClaudeAgent(timeout_s=t, permission_mode="plan"), "codex": CodexAgent(timeout_s=t)}


# ---- Diagnosis: condition -> one exact fix line --------------------------------------------------

RERUN = "PowerShell: python -m core.readiness"
GMAIL_FIX = ("PowerShell: python -c \"import keyring,getpass; keyring.set_password('forge-gmail',"
             f"'{GMAIL}',getpass.getpass('Gmail app password: '))\"")
N8N_RUN = ("PowerShell: docker run -d --name n8n --restart unless-stopped -p 127.0.0.1:5678:5678 "
           "-v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n")
_MISSING = r"not installed|not on PATH|agent command not found|command not found"
_AI_MISSING = r"not installed|not recognized|FileNotFound|no such file|agent command not found|command not found"
_AI_LOGGED_OUT = r"log ?in|401|unauthori|authentication|api key"
_AI_RATE = r"rate|usage limit|429|overloaded"

# name -> ordered rows of (detail regex or None for "anything", condition, troubleshoot, fix, then).
# The first matching row wins; matching is case-insensitive. ollama, n8n and python_libs rows are
# refined in diagnose() because they need `which`, a probe or the detail text.
RULES: dict[str, list[tuple[str | None, str, bool, str, str]]] = {
    "git": [
        (_MISSING, "missing", False, "PowerShell: winget install --id Git.Git -e", ""),
        (None, "error", True, "PowerShell: git --version", ""),
    ],
    "github": [
        (_MISSING, "missing", False, "PowerShell: winget install --id GitHub.cli -e", ""),
        (r"not logged|auth login|no oauth|token|authentication", "logged_out", False,
         "PowerShell: gh auth login --hostname github.com --git-protocol https --web", ""),
        (None, "error", True, "PowerShell: gh auth status", ""),
    ],
    "claude": [
        (_AI_MISSING, "missing", False, "PowerShell: npm install -g @anthropic-ai/claude-code", ""),
        (_AI_LOGGED_OUT, "logged_out", False, "PowerShell: claude.cmd", "type /login and follow the browser sign-in"),
        (_AI_RATE, "rate_limited", False, "PowerShell: claude.cmd -p ok", ""),
        (None, "error", True, "PowerShell: claude.cmd -p ok", ""),
    ],
    "codex": [
        (_AI_MISSING, "missing", False, "PowerShell: npm install -g @openai/codex", ""),
        (_AI_LOGGED_OUT, "logged_out", False, "PowerShell: codex login", ""),
        (_AI_RATE, "rate_limited", False, "PowerShell: codex exec ok", ""),
        (None, "error", True, "PowerShell: codex exec ok", ""),
    ],
    "gmail": [
        (r"no app password", "no_password", False, GMAIL_FIX,
         "create the app password at https://myaccount.google.com/apppasswords first"),
        (r"SMTPAuthentication|not accepted|535", "auth_rejected", False, GMAIL_FIX,
         "Gmail rejected the saved app password; create a new one at "
         "https://myaccount.google.com/apppasswords and save it with this line"),
        (r"No module named", "missing_lib", True, "PowerShell: python -m pip install keyring", ""),
        (r"gaierror|getaddrinfo|timed out|refused|unreachable", "network", True,
         "PowerShell: Test-NetConnection smtp.gmail.com -Port 465", ""),
        (None, "error", True, RERUN, ""),
    ],
    "docker": [
        (_MISSING, "missing", False, "PowerShell: winget install --id Docker.DockerDesktop -e", ""),
        (r"error during connect|cannot connect|daemon|pipe", "daemon_down", True,
         "Win + R: C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe", ""),
        (None, "error", True, "PowerShell: docker info", ""),
    ],
    "n8n": [
        ("docker_down", "docker_down", False, "PowerShell: docker info", "start Docker first, then check n8n again"),
        ("probe_failed", "probe_failed", True, "PowerShell: docker ps -a", ""),
        ("container_missing", "container_missing", True, N8N_RUN, ""),
        ("container_stopped", "container_stopped", True, "PowerShell: docker start n8n", ""),
        ("unhealthy", "unhealthy", True, "PowerShell: docker restart n8n", ""),
    ],
    "ollama": [
        ("missing", "missing", False, "PowerShell: winget install --id Ollama.Ollama -e", ""),
        ("not_running", "not_running", True,
         "PowerShell: Start-Process ollama -ArgumentList serve -WindowStyle Hidden", ""),
    ],
    "python_libs": [
        (r"missing:", "missing_libs", True, "PowerShell: python -m pip install {libs}", ""),
        (None, "error", True, RERUN, ""),
    ],
    "browser": [
        (r"No module named", "missing_lib", True,
         "PowerShell: python -m pip install playwright; python -m playwright install chromium", ""),
        (r"Executable doesn't exist|playwright install", "no_chromium", True,
         "PowerShell: python -m playwright install chromium", ""),
        (None, "error", True, "PowerShell: python -m playwright install chromium", ""),
    ],
}
GENERIC_RULES = [
    (r"timed out", "timeout", True, RERUN, ""),
    (None, "error", True, RERUN, ""),
]
NO_EVIDENCE = ("no_evidence", True, RERUN, "")
NO_CHECK = ("no_check", False, "Reply to this email with: not needed", "")
PIP_NAMES = {"yaml": "pyyaml"}


def _default_probe(args: list[str]) -> tuple[int, str]:
    """Run a command hidden (via core.agents.launch) and return (exit code, combined output)."""
    from core.agents import launch
    exe = shutil.which(args[0])
    if not exe:
        return 127, f"{args[0]} not installed or not on PATH"
    try:
        inner = _inner()
    except Exception:  # noqa: BLE001
        inner = 45
    code, out, err = launch([exe, *args[1:]], Path.home(), "", inner)
    return code, (out or "") + (err or "")


def _n8n_state(cap_map: dict, probe) -> str:
    docker = cap_map.get("docker") if isinstance(cap_map, dict) else None
    if broken(docker, datetime.now(timezone.utc), math.inf) is not None:
        return "docker_down"
    try:
        code, out = probe(["docker", "ps", "-a", "--filter", "name=^/n8n$", "--format", "{{.Status}}"])
    except Exception:  # noqa: BLE001
        return "probe_failed"
    if code != 0:
        return "probe_failed"
    status = (out or "").strip()
    if not status:
        return "container_missing"
    if status.startswith(("Exited", "Created")):
        return "container_stopped"
    return "unhealthy"  # "Up ..." and any other state: the container exists but n8n is not answering


def _result(row, depends_on=None, **fmt) -> dict:
    condition, troubleshoot, fix, then = row
    return {"condition": condition, "fix": fix.format(**fmt) if fmt else fix, "then": then,
            "troubleshoot": troubleshoot, "depends_on": depends_on}


def diagnose(name, entry, cap_map, which=shutil.which, probe=None) -> dict:
    """Pick the condition and the one exact fix line for a broken capability. Never raises."""
    try:
        return _diagnose(name, entry, cap_map, which, probe or _default_probe)
    except Exception as e:  # noqa: BLE001
        return {"condition": "error", "fix": RERUN, "then": f"diagnosis failed: {type(e).__name__}: {e}"[:DETAIL_MAX],
                "troubleshoot": True, "depends_on": None}


def _diagnose(name, entry, cap_map, which, probe) -> dict:
    if name not in CHECKS and name not in AI_NAMES:
        return _result(NO_CHECK)
    if entry is None:
        return _result(NO_EVIDENCE)
    detail = entry.get("detail") if isinstance(entry, dict) else None
    detail = detail if isinstance(detail, str) else ""
    if name == "n8n":
        detail = _n8n_state(cap_map or {}, probe)
    elif name == "ollama":
        detail = "missing" if which("ollama") is None else "not_running"
    rows = RULES.get(name, GENERIC_RULES)
    for pattern, condition, troubleshoot, fix, then in rows:
        if pattern is None or re.search(pattern, detail, re.IGNORECASE):
            row = (condition, troubleshoot, fix, then)
            if condition == "docker_down":
                return _result(row, depends_on="docker")
            if condition == "missing_libs":
                m = re.search(r"missing:\s*(.+)", detail, re.IGNORECASE)
                libs = [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []
                if not libs:
                    continue
                return _result(row, libs=" ".join(PIP_NAMES.get(x, x) for x in libs))
            return _result(row)
    return _result(GENERIC_RULES[-1][1:])


def _reap(grace_s: float) -> None:
    """Give straggler checks a bounded time to finish their own process-tree cleanup."""
    end = time.monotonic() + grace_s
    for t in list(_stragglers):
        t.join(max(0.0, end - time.monotonic()))


def main() -> int:
    from core.agents import load_limits
    lim = load_limits(FORGE_ROOT)
    m = run_checks(STATE, CHECKS, lim["check_timeout_s"], ttl={k: lim["ai_check_ttl_h"] for k in AI_TTL})
    for name, r in sorted(m.items()):
        print(f"{'OK  ' if r['ok'] else 'FAIL'}  {name:12} {r['detail']}")
    sys.stdout.flush()
    _reap(REAP_GRACE_S)
    return 0 if all(r["ok"] for r in m.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
