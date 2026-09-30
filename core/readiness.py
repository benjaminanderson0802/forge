"""Readiness check: which of Forge's connections actually work right now.

Writes state/capabilities.json. Every agent reads it; blocker claims that contradict it
are rejected (D-030, D-031). Secrets are touched only here, never shown to agents.

Timeouts: process-spawning checks are responsible for their own tree cleanup via launch();
the outer deadline is a reporting deadline; main() waits a bounded grace so that cleanup can
finish. Checks run in daemon threads, so a hung pure-Python check can never block exit."""
from __future__ import annotations

import json
import os
import shutil
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


def _ai(agent) -> tuple[bool, str]:
    r = agent.run("Reply with exactly: ok", Path.home())
    return (r.ok and r.text.strip().lower().startswith("ok")), (r.error or f"ok ({r.tokens} tokens)")


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


def run_checks(state_dir: Path, checks: dict, timeout_s: int, clock=None, ttl: dict | None = None) -> dict:
    clock = clock or (lambda: datetime.now(timezone.utc))
    path = Path(state_dir) / "capabilities.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
    now = clock()
    result, todo = {}, {}
    for name, fn in checks.items():
        prev = old.get(name)
        if _fresh(prev, (ttl or {}).get(name), now):
            result[name] = prev
        else:
            todo[name] = fn

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
               for name, fn in todo.items()}
    for t in threads.values():
        t.start()
    deadline = time.monotonic() + timeout_s
    for t in threads.values():
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        snapshot = dict(reported)
    _stragglers[:] = [t for t in _stragglers if t.is_alive()] + [t for t in threads.values() if t.is_alive()]

    for name in todo:
        got = snapshot.get(name)
        if got is not None and got[0] <= deadline:  # a result reported after the deadline never counts
            _, ok, detail = got
        else:
            ok, detail = False, f"check timed out after {timeout_s}s"
        result[name] = {"ok": ok, "detail": detail[:DETAIL_MAX], "checked_at": now.isoformat()}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(json.dumps(result, indent=2, sort_keys=True).encode("utf-8"))
    os.replace(tmp, path)
    return result


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
