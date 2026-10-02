"""Blockers (R66): Forge fixes what it can itself; what only Ben can do comes as a ready fix kit.

Spec: docs/specs/bootstrap-conductor.md, "Blockers amendment (2026-10-01, R66)". Plain code, standard library only.
Nothing here sends mail (the conductor's `_send` stays the only way out) and nothing here runs a fix on its own: a
prepared script runs only through `run_fix`, which the status page calls when Ben presses Do it.

Each lane keeps `<lane state>/blockers.json`: {"seq": n, "items": {id: record}}. A record is one problem Forge is
fixing (`fixing`), one that needs Ben (`ready_for_ben`, with a fix kit), or one that is `fixed` (Forge re-checked it).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

BEN_ONLY = ("money", "account", "credentials", "message", "legal", "unpark", "hardware", "admin")
CATEGORIES = BEN_ONLY + ("exhausted",)
STATUSES = ("fixing", "ready_for_ben", "fixed")
NO_SCRIPT = ("credentials", "admin", "unpark")  # these need a prompt or Ben's own account: never a hidden script
WHERES = ("PowerShell", "Win + R")
SUMMARY_CAP, PASTE_CAP, TEXT_CAP, SCRIPT_CAP = 200, 500, 4000, 20000
FIXED_KEEP, OUTBOX_KEEP = 50, 50
FIX_TIMEOUT_S = 600
LOG_TAIL = 4000
FILE = "blockers.json"

_SECRET_RES = [
    re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|"
               r"app[_-]?password)\s*[:=]\s*['\"]?[^\s'\"]{4,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_QUOTED_PATH = re.compile(r"[\"'](?:[A-Za-z]:[\\/]|\\\\|~[\\/]|%[A-Za-z]+%)")


def has_secret(text) -> bool:
    """True when the text looks like it carries a secret value (R66j: kits never do)."""
    s = str(text or "")
    return any(r.search(s) for r in _SECRET_RES)


def validate_kit(kit) -> list[str]:
    """The problems with a fix kit; an empty list means it may be stored and shown to Ben."""
    if not isinstance(kit, dict):
        return ["kit is not an object"]
    out = []
    for k in ("why", "paste", "expect"):
        if not isinstance(kit.get(k), str) or not kit.get(k).strip():
            out.append(f"kit {k} is missing")
    cat = kit.get("category")
    if cat not in CATEGORIES:
        out.append(f"kit category {cat!r} is not one of {', '.join(CATEGORIES)}")
    if kit.get("where") not in WHERES:
        out.append(f"kit where {kit.get('where')!r} is not PowerShell or Win + R")
    paste = kit.get("paste") if isinstance(kit.get("paste"), str) else ""
    if "\n" in paste or "\r" in paste:
        out.append("kit paste must be exactly one line")
    if len(paste) > PASTE_CAP:
        out.append(f"kit paste is longer than {PASTE_CAP} characters")
    if _QUOTED_PATH.search(paste):
        out.append("kit paste puts quotes around a path")
    script = kit.get("script")
    if script is not None:
        if not isinstance(script, str) or not script.strip():
            out.append("kit script is empty")
        elif len(script) > SCRIPT_CAP:
            out.append(f"kit script is longer than {SCRIPT_CAP} characters")
        if cat in NO_SCRIPT:
            out.append(f"no prepared script for a {cat} step: it needs Ben himself")
    for k in ("why", "paste", "expect", "script"):
        if k in kit and has_secret(kit.get(k)):
            out.append(f"kit {k} looks like it holds a secret")
    return out


def _one_line(text, cap: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= cap else s[:cap - 3].rstrip() + "..."


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(3)}")
    tmp.write_bytes(raw)
    for i in range(6):  # Windows: a reader may hold the target open for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.05 * (i + 1))


def read_json(path: Path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


class Blockers:
    """One lane's blocker records (R66j)."""

    def __init__(self, state: Path, lane: str = "main", clock=None):
        self.state, self.lane = Path(state), str(lane)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = self.state / FILE

    # ---------------------------------------------------------------- storage
    def _load(self) -> dict:
        d = read_json(self.path, {})
        items = d.get("items") if isinstance(d.get("items"), dict) else {}
        return {"seq": int(d.get("seq") or 0) if str(d.get("seq") or 0).isdigit() else 0,
                "items": {k: v for k, v in items.items() if isinstance(v, dict)}}

    def _save(self, d: dict) -> None:
        items = d["items"]
        fixed = sorted((r for r in items.values() if r.get("status") == "fixed"),
                       key=lambda r: str(r.get("fixed_at") or ""))
        drop = {r["id"] for r in fixed[:-FIXED_KEEP]} if len(fixed) > FIXED_KEEP else set()
        d["items"] = {k: v for k, v in items.items() if k not in drop}
        _atomic(self.path, json.dumps(d, indent=2, sort_keys=True).encode("utf-8"))

    def _now(self) -> str:
        return self.clock().isoformat()

    def all(self) -> dict:
        return self._load()["items"]

    def get(self, bid: str) -> dict | None:
        return self.all().get(bid)

    def active(self) -> list[dict]:
        return [r for r in self.all().values() if r.get("status") != "fixed"]

    def find(self, kind: str, key: str) -> dict | None:
        return next((r for r in self.active() if r.get("kind") == kind and r.get("key") == str(key)), None)

    def _change(self, bid: str, fn) -> dict:
        d = self._load()
        rec = d["items"].get(bid)
        if rec is None:
            raise KeyError(bid)
        fn(rec)
        rec["updated_at"] = self._now()
        self._save(d)
        return dict(rec)

    # ---------------------------------------------------------------- changes
    def open(self, kind: str, key, summary: str, category: str | None = None) -> dict:
        """The not-fixed record for (kind, key), with its summary refreshed, or a new one being fixed."""
        d = self._load()
        summary = _one_line(summary, SUMMARY_CAP)
        for rec in d["items"].values():
            if rec.get("status") != "fixed" and rec.get("kind") == kind and rec.get("key") == str(key):
                if rec.get("summary") != summary:
                    rec["summary"], rec["updated_at"] = summary, self._now()
                    self._save(d)
                return dict(rec)
        d["seq"] += 1
        bid = f"b-{d['seq']}" if self.lane == "main" else f"{self.lane}-b-{d['seq']}"
        now = self._now()
        rec = {"id": bid, "lane": self.lane, "kind": str(kind), "key": str(key), "summary": summary,
               "status": "fixing", "attempts": 0, "category": category, "code": secrets.token_urlsafe(6)[:8],
               "created_at": now, "updated_at": now, "fixed_at": None, "kit": None}
        d["items"][bid] = rec
        self._save(d)
        return dict(rec)

    def attempt(self, bid: str) -> dict:
        return self._change(bid, lambda r: r.update(attempts=int(r.get("attempts") or 0) + 1))

    def ready_for_ben(self, bid: str, kit: dict) -> dict:
        """Validate the kit (ValueError and no change when it has problems), write its script, mark it ready."""
        problems = validate_kit(kit)
        if problems:
            raise ValueError("; ".join(problems))
        if bid not in self.all():
            raise KeyError(bid)
        kit = {k: v for k, v in dict(kit).items() if k not in ("script_file", "script_sha256")}
        if kit.get("script"):
            raw = str(kit["script"]).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").encode("utf-8") + b"\n"
            rel = f"fixkits/{bid}.ps1"
            _atomic(self.state / rel, raw)
            kit["script_file"], kit["script_sha256"] = rel, _sha(raw)
        return self._change(bid, lambda r: r.update(status="ready_for_ben", kit=kit, category=kit["category"]))

    def update(self, bid: str, **changes) -> dict:
        return self._change(bid, lambda r: r.update(changes))

    def back_to_fixing(self, bid: str) -> dict:
        return self._change(bid, lambda r: r.update(status="fixing", attempts=0))

    def fixed(self, bid: str) -> dict:
        return self._change(bid, lambda r: r.update(status="fixed", fixed_at=self._now()))


# ---------------------------------------------------------------------- kits
def retry_line(repo, lane: str, bid: str, not_needed: bool = False) -> str:
    return f"cd {repo}; python -m core.bootstrap retry --lane {lane} {bid}" + (" --not-needed" if not_needed else "")


def retry_kit(repo, lane: str, bid: str, why: str, expect: str, not_needed: bool = False) -> dict:
    line = retry_line(repo, lane, bid, not_needed)
    return {"why": why, "category": "exhausted", "where": "PowerShell", "paste": line, "script": line,
            "expect": expect}


def gmail_compose_line(owner: str, subject: str, body: str) -> str:
    """A Win + R line that opens a Gmail compose window to Ben himself, ready to send (R66d, unpark)."""
    return ("https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(owner, safe="@") + "&su=" +
            quote(subject, safe="") + "&body=" + quote(body, safe=""))


# ---------------------------------------------------------------------- email
STOP_LINE = "To stop everything: reply STOP to any Forge email."


def opened_mail(rec: dict, page_url: str) -> tuple[str, str]:
    summary = str(rec.get("summary", ""))
    if rec.get("status") != "ready_for_ben" or not isinstance(rec.get("kit"), dict):
        subject = f"[Forge] Blocked: {summary} — fixing it myself, nothing for you to do"
        body = (f"Forge hit a blocker: {summary}\n\nIt is fixing this itself, and other work carries on. Nothing for "
                f"you to do; you'll get one more email when it's fixed.\n\nStatus page (on your PC): {page_url}\n\n"
                + STOP_LINE)
        return subject, body
    kit = rec["kit"]
    where = "Press Win + R and paste this line:" if kit.get("where") == "Win + R" else \
        "Paste this line into PowerShell:"
    parts = [f"Forge needs you for about 2 minutes: {summary}", f"Why it needs you: {kit.get('why', '')}",
             f"{where}\n\n{kit.get('paste', '')}"]
    if kit.get("script"):
        parts.append("Or press Do it on the status page. It runs this prepared script (shown in full):\n\n"
                     + str(kit["script"]).rstrip("\n"))
    parts += [f"What you'll see when it's fixed: {kit.get('expect', '')}",
              f"Status page (on your PC, with the Copy and Do it buttons): {page_url}",
              "Forge checks the fix itself and emails you when it's fixed. Other work carries on meanwhile. "
              + STOP_LINE]
    return f"[Forge] Needs you (~2 min): {summary}", "\n\n".join(parts)


def fixed_mail(rec: dict) -> tuple[str, str]:
    summary = str(rec.get("summary", ""))
    return (f"[Forge] Fixed: {summary}",
            f"Fixed and checked by Forge: {summary}\n\nNothing for you to do.\n\n" + STOP_LINE)


def digest_lines(records: list[dict]) -> list[str]:
    """The digest's blocker section (R66g)."""
    open_ = [r for r in records if r.get("status") != "fixed"]
    done = [r for r in records if r.get("status") == "fixed"]
    lines = []
    if open_:
        lines.append(f"Blockers ({len(open_)}):")
        for r in open_:
            if r.get("status") == "ready_for_ben" and isinstance(r.get("kit"), dict):
                k = r["kit"]
                lines.append(f"* NEEDS YOU: {r.get('summary')}  ({k.get('where')}: {k.get('paste')})")
            else:
                lines.append(f"* fixing it myself: {r.get('summary')}")
        lines.append("")
    if done:
        lines.append("Fixed: " + "; ".join(str(r.get("summary")) for r in done)[:2000])
        lines.append("")
    return lines


# ---------------------------------------------------------------------- Do it (the status page)
def fixruns_dir(state_root) -> Path:
    return Path(state_root) / "channel" / "fixruns"


def lane_state(state_root, lane: str) -> Path:
    return Path(state_root) / "bootstrap" if lane == "main" else Path(state_root) / "lanes" / lane


_LANE_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_BID_RE = re.compile(r"^(?:[a-z][a-z0-9_]{0,23}-)?b-\d{1,9}$")
_running: set = set()
_run_lock = threading.Lock()


def script_text(state, rec: dict) -> str | None:
    """The script's text, only while its bytes are still exactly what the record names (R66i)."""
    kit = rec.get("kit") if isinstance(rec, dict) else None
    if not isinstance(kit, dict) or not kit.get("script_file") or not kit.get("script_sha256"):
        return None
    rel = str(kit["script_file"])
    if ".." in rel or rel.startswith(("/", "\\")) or not re.match(r"^fixkits/[\w-]+\.ps1$", rel):
        return None
    try:
        raw = (Path(state) / rel).read_bytes()
    except OSError:
        return None
    if _sha(raw) != kit["script_sha256"]:
        return None
    return raw.decode("utf-8", "replace")


def powershell_text_runner(text: str, cwd: Path | None = None):
    """A runner that feeds PowerShell exactly the validated script bytes on stdin (`-Command -`), so the file on
    disk can't be swapped between the check and the run (R66i review P1)."""
    def run(script: Path, log: Path, timeout_s: float) -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as f:
            try:
                p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-Command", "-"], input=text.encode("utf-8"), stdout=f, stderr=subprocess.STDOUT,
                                   timeout=timeout_s, creationflags=flags, cwd=str(cwd or log.parent))
                return p.returncode
            except subprocess.TimeoutExpired:
                f.write(f"\n[Forge] the fix ran longer than {int(timeout_s)} s and was stopped\n".encode())
                return 124
            except OSError as e:
                f.write(f"\n[Forge] could not start PowerShell: {e}\n".encode())
                return 127
    return run


def run_status(state_root, bid: str) -> dict | None:
    if not isinstance(bid, str) or not _BID_RE.match(bid):
        return None
    d = fixruns_dir(state_root)
    st = read_json(d / f"{bid}.json", {})
    if not st:
        return None
    try:
        log = (d / f"{bid}.log").read_bytes()[-LOG_TAIL * 4:].decode("utf-8", "replace")[-LOG_TAIL:]
    except OSError:
        log = ""
    return dict(st, log=log)


def run_fix(state_root, lane: str, bid: str, sha: str, runner=None) -> tuple[bool, str]:
    """Start the prepared script of a ready_for_ben blocker, once, in the background (R66i). The caller is the status
    page after Ben pressed Do it: that click is his consent. Every check is plain code; refusals run nothing."""
    if not isinstance(lane, str) or not (lane == "main" or _LANE_RE.match(lane)):
        return False, "bad lane"
    if not isinstance(bid, str) or not _BID_RE.match(bid):
        return False, "bad blocker id"
    state = lane_state(state_root, lane)
    rec = Blockers(state, lane).get(bid)
    if rec is None or rec.get("status") != "ready_for_ben":
        return False, "that blocker is not waiting for you"
    kit = rec.get("kit") or {}
    text = script_text(state, rec)
    if text is None:
        return False, "this fix has no prepared script, or the script changed since Forge prepared it"
    if not isinstance(sha, str) or not secrets.compare_digest(sha, str(kit.get("script_sha256"))):
        return False, "the script you saw is not the one on disk; reload the page"
    d = fixruns_dir(state_root)
    with _run_lock:
        st = read_json(d / f"{bid}.json", {})
        if bid in _running or (st and st.get("finished_at") is None and st.get("pid") == os.getpid()):
            return False, "this fix is already running"
        _running.add(bid)
        start = {"id": bid, "lane": lane, "sha": sha, "started_at": datetime.now(timezone.utc).isoformat(),
                 "finished_at": None, "exit": None, "pid": os.getpid()}
        _atomic(d / f"{bid}.json", json.dumps(start, indent=2).encode("utf-8"))
    script = state / str(kit["script_file"])  # named for the record; the real run never re-reads it
    run = runner or powershell_text_runner(text, cwd=Path(state_root).parent)  # the validated bytes, in the repo

    def work() -> None:
        code = None
        try:
            code = run(script, d / f"{bid}.log", FIX_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - recorded, never raised into the page server
            try:
                with open(d / f"{bid}.log", "ab") as f:
                    f.write(f"\n[Forge] the fix failed to run: {type(e).__name__}: {e}\n".encode())
            except OSError:
                pass
            code = -1
        finally:
            done = dict(start, finished_at=datetime.now(timezone.utc).isoformat(),
                        exit=code if isinstance(code, int) else None)
            try:
                _atomic(d / f"{bid}.json", json.dumps(done, indent=2).encode("utf-8"))
            finally:
                with _run_lock:
                    _running.discard(bid)

    threading.Thread(target=work, daemon=True).start()
    return True, "started"


def snapshot_items(state_root, lanes: list[str], now: datetime, keep_fixed_s: float = 86400) -> list[dict]:
    """The dashboard's Blockers panel data (R66i): every lane's open records plus those fixed in the last 24 hours."""
    out = []
    for lane in lanes:
        state = lane_state(state_root, lane)
        try:
            items = Blockers(state, lane).all()
        except Exception:  # noqa: BLE001 - the dashboard never breaks on one lane's file
            continue
        for r in items.values():
            def age(key: str, r=r):
                try:
                    return max(0.0, (now - datetime.fromisoformat(str(r.get(key)))).total_seconds())
                except (TypeError, ValueError):
                    return None
            if r.get("status") == "fixed":
                a = age("fixed_at")
                if a is None or a > keep_fixed_s:
                    continue
            kit = dict(r["kit"]) if isinstance(r.get("kit"), dict) else None
            if kit is not None:
                kit["script_text"] = script_text(state, r)
            out.append({"id": r.get("id"), "lane": r.get("lane", lane), "kind": r.get("kind"),
                        "summary": r.get("summary"), "status": r.get("status"), "age_s": age("created_at"),
                        "attempts": r.get("attempts"), "category": r.get("category"), "kit": kit,
                        "fixed_at": r.get("fixed_at"), "run": run_status(state_root, str(r.get("id")))})
    order = {"ready_for_ben": 0, "fixing": 1, "fixed": 2}
    out.sort(key=lambda x: (order.get(str(x.get("status")), 3), -(x.get("age_s") or 0)))
    return out
