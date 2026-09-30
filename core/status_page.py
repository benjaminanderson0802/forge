"""Forge's local status page (Layer 1E; design §6, D-022, D-023, D-024).

    python -m core.status_page            # http://127.0.0.1:8765

Served on this PC only: it binds 127.0.0.1 and nothing else, refuses any request whose Host isn't 127.0.0.1 or
localhost on its port (DNS rebinding), refuses cross-site POSTs, and forbids framing. It reads the conductor's state
files and never writes them, with one exception: the Stop button creates `state/KILL` (D-024). Answers are not
applied here; they go to the conductor through the answer drop folder (core.channel), which checks each one exactly
like an email reply. Standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import time
import threading
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from core import channel, readiness
from core.usage import Meter

PORT = 8765
LOOPBACK = ("127.0.0.1",)
MAX_POST = 20000
PROVIDERS = ("claude", "codex")

_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--bad:#b3261e;--ok:#1e6b34;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#34332f;--card:#1f1f1d;
--bad:#f2766b;--ok:#6fcf8a;--warn:#e7b35a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:920px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0 4px}h2{font-size:17px;margin:24px 0 8px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;
margin:8px 0}.banner{border-radius:8px;padding:10px 12px;margin:10px 0;font-weight:600;border:1px solid var(--line)}
.stop{color:var(--bad)}.warn{color:var(--warn)}.ok{color:var(--ok)}.bad{color:var(--bad)}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);
vertical-align:top}textarea{width:100%;min-height:3em;font:inherit;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:6px}button{font:inherit;padding:6px 12px;border-radius:6px;
border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;margin:4px 4px 0 0}
button.danger{border-color:var(--bad);color:var(--bad);font-weight:600}.row{overflow-x:auto}
"""


def _read(state: Path, name: str, default):
    try:
        data = json.loads((Path(state) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _e(x) -> str:
    return html.escape(str(x), quote=True)


def _mail_used(state: Path, now: datetime) -> tuple[int, int]:
    hour = day = 0
    for x in _read(state, "mail_log.json", {}).get("sent", []):
        try:
            age = (now - datetime.fromisoformat(x)).total_seconds()
        except (TypeError, ValueError):
            continue
        hour += age < 3600
        day += age < 86400
    return hour, day


def render(state: Path, limits: dict, now: datetime | None = None, *, local_tz=None) -> str:
    """The whole page as HTML. Pure apart from reading state files; every value is escaped."""
    state = Path(state)
    now = now or datetime.now(timezone.utc)
    local = channel.to_local(now, local_tz)
    out = [f"<h1>Forge status</h1><p class=muted>{_e(local.strftime('%A %d %B %Y, %H:%M'))} "
           "&middot; this page does not refresh itself: reload it to update</p>"]
    if (state / "KILL").exists():
        out.append("<div class='banner stop'>Forge is stopped (KILL is set). It restarts only when you clear the "
                   "stop yourself.</div>")
    if (state / "PAUSED").exists():
        out.append("<div class='banner warn'>Forge is paused: it is waiting for your answer to a re-plan "
                   "question.</div>")
    if limits.get("digest_hour") is not None and channel.is_quiet(
            local, int(limits.get("quiet_start", 23)), int(limits.get("quiet_end", 7))):
        out.append("<div class='banner'>Quiet hours: no email until morning except a stop alert.</div>")
    out.append("<form method=post action=\"/stop\"><button class=danger type=submit>Stop Forge</button> "
               "<span class=muted>Creates the stop file at once. Forge halts before its next step.</span></form>")

    items = channel.queue_items(_read(state, "questions.json", {}))
    out.append(f"<h2>Waiting on you ({len(items)})</h2>")
    if not items:
        out.append("<p class=muted>Nothing is waiting on you.</p>")
    for i in items:
        how = {"email": "emailed" if i["delivered"] else "email not sent yet", "digest": "in the daily digest",
               "held": "held until only you can unblock it"}[i["via"]]
        approve = ("<button type=submit name=approve value=y>Approve</button>" if i["kind"] == "gate" else "")
        out.append(
            f"<div class=card><div><strong>{_e(i['question'])}</strong> <span class=muted>({_e(i['kind'])}, "
            f"Q-{_e(i['id'])}, {_e(how)})</span></div>"
            f"<div class=muted>If you don't answer: {_e(i['default'])}</div>"
            f"<form method=post action=\"/answer\"><input type=hidden name=qid value=\"{_e(i['id'])}\">"
            f"<input type=hidden name=code value=\"{_e(i['code'])}\">"
            "<textarea name=answer placeholder='Your answer (y, n, or free text)'></textarea>"
            f"{approve}<button type=submit>Answer</button></form></div>")

    q = _read(state, "queue.json", {})
    tasks = [t for t in q.get("tasks", []) if isinstance(t, dict)]
    current = next((t for t in tasks if t.get("status") in ("todo", "tests_ok")), None)
    out.append(f"<h2>Work{(' on ' + _e(q.get('layer'))) if q.get('layer') else ''}</h2>")
    if current:
        out.append(f"<p>Now: <strong>{_e(current.get('id'))}</strong> {_e(current.get('title', ''))} "
                   f"<span class=muted>({_e(current.get('status'))})</span></p>")
    hb = (state / "conductor.heartbeat")
    if hb.exists():
        try:
            age = now.timestamp() - float(hb.read_text(encoding="utf-8").split()[1])
            out.append(f"<p class=muted>Conductor heartbeat {int(age // 60)} min ago.</p>")
        except (OSError, ValueError, IndexError):
            pass
    if tasks:
        rows = "".join(f"<tr><td>{_e(t.get('id'))}</td><td>{_e(t.get('title', ''))}</td>"
                       f"<td>{_e(t.get('status'))}</td></tr>" for t in tasks)
        out.append(f"<div class=row><table><tr><th>Task</th><th>Title</th><th>Status</th></tr>{rows}</table></div>")
    else:
        out.append("<p class=muted>No tasks queued.</p>")

    meter = Meter(state, clock=lambda: now)
    rows = []
    for p in PROVIDERS:
        cap = limits.get(f"{p}_daily_token_cap")
        rows.append(f"<tr><td>{p} tokens today</td><td>{meter.used_today(p)} of {_e(cap if cap is not None else '-')}"
                    "</td></tr>")
    mh, md = _mail_used(state, now)
    rows.append(f"<tr><td>email</td><td>{mh} of {_e(limits.get('mail_per_hour', 6))} this hour, "
                f"{md} of {_e(limits.get('mail_per_day', 30))} today</td></tr>")
    out.append("<h2>Caps</h2><div class=row><table>" + "".join(rows) + "</table></div>")

    caps = _read(state, "capabilities.json", {})
    out.append("<h2>Capabilities</h2>")
    if not caps:
        out.append("<p class=muted>No readiness evidence yet.</p>")
    else:
        rows = []
        for name in sorted(caps):
            e = caps[name]
            why = readiness.broken(e, now, readiness.max_age_for(name, limits))
            st = "<span class=ok>OK</span>" if why is None else f"<span class=bad>BROKEN</span> {_e(why)}"
            detail = e.get("detail", "") if isinstance(e, dict) else ""
            at = e.get("checked_at", "?") if isinstance(e, dict) else "?"
            rows.append(f"<tr><td>{_e(name)}</td><td>{st}</td><td>{_e(detail)}</td><td class=muted>{_e(at)}</td></tr>")
        out.append("<div class=row><table>" + "".join(rows) + "</table></div>")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>Forge status</title>"
            f"<style>{_CSS}</style></head><body><main>" + "".join(out) + "</main></body></html>")


def conductor_between_runs(service_root: Path, now: float | None = None, stale_s: float = 180.0) -> bool:
    """Only while the conductor sleeps (or isn't running) may an answer file appear: a file that appears during an
    agent run is tampering by design (the drop folder is fingerprinted), so the page must never write one then."""
    hb = _read(Path(service_root), "heartbeat.json", {})
    at = hb.get("at") if isinstance(hb, dict) else None
    now = time.time() if now is None else now
    if not isinstance(at, (int, float)) or now - at > stale_s or hb.get("phase") == "exited":
        return True  # not running: nothing can mistake the file for an agent's write
    return hb.get("phase") == "sleep"


class AnswerOutbox:
    """Holds answers from the page until the conductor is between runs, then drops them (in order)."""

    def __init__(self, drop: Path, service_root: Path, idle=conductor_between_runs):
        self.drop, self.service_root, self.idle = Path(drop), Path(service_root), idle
        self.pending: list[tuple[str, str, str]] = []
        self.lock = threading.Lock()

    def add(self, qid: str, code: str, answer: str) -> None:
        with self.lock:
            self.pending.append((qid, code, answer))
        self.flush()

    def flush(self) -> int:
        with self.lock:
            if not self.pending or not self.idle(self.service_root):
                return 0
            n = 0
            while self.pending:
                qid, code, answer = self.pending[0]
                try:
                    channel.drop_answer(self.drop, qid, code, answer, "status page")
                except ValueError:
                    pass
                self.pending.pop(0)
                n += 1
            return n


def make_server(state: Path, channel_dir: Path, limits: dict, host: str = "127.0.0.1", port: int = PORT,
                local_tz=None, outbox: "AnswerOutbox | None" = None) -> ThreadingHTTPServer:
    """A server bound to the loopback address only (D-022). port=0 picks a free port (tests)."""
    if host not in LOOPBACK:
        raise ValueError(f"the status page binds 127.0.0.1 only, not {host!r}")
    state, drop = Path(state), Path(channel_dir) / "in"
    outbox = outbox or AnswerOutbox(drop, state.parent / "service")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ForgeStatus"
        sys_version = ""

        def log_message(self, *_a) -> None:  # quiet: no console output from a background page
            pass

        def _allowed_hosts(self) -> set[str]:
            p = self.server.server_address[1]
            return {f"127.0.0.1:{p}", f"localhost:{p}"} | ({"127.0.0.1", "localhost"} if p == 80 else set())

        def _send(self, code: int, body: str = "", location: str | None = None) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            if raw and self.command != "HEAD":
                self.wfile.write(raw)

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").strip().lower() in self._allowed_hosts()

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is not None and origin.strip().lower() not in {"http://" + h for h in self._allowed_hosts()}:
                return False
            site = self.headers.get("Sec-Fetch-Site")
            return site is None or site.strip().lower() in ("same-origin", "none")

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if not self._host_ok():
                return self._send(403, "Forbidden")
            if self.path.split("?", 1)[0] != "/":
                return self._send(404, "Not found")
            try:
                page = render(state, limits, local_tz=local_tz)
            except Exception as e:  # noqa: BLE001 - a broken file never takes the page down
                page = f"<!doctype html><title>Forge status</title><p>Could not read Forge's state: {_e(e)}</p>"
            self._send(200, page)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or not self._same_origin():
                return self._send(403, "Forbidden")
            path = self.path.split("?", 1)[0]
            if path not in ("/stop", "/answer"):
                return self._send(404, "Not found")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, "Bad request")
            if n < 0 or n > MAX_POST:
                return self._send(413, "Too large")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            if path == "/stop":  # D-024: one of the three equal ways to stop Forge
                (state / "KILL").write_text("stopped from the status page\n", encoding="utf-8")
                return self._send(303, "", location="/")
            qid, code, answer = form.get("qid", ""), form.get("code", ""), form.get("answer", "").strip()
            if form.get("approve") == "y":  # the gate's Approve button: the same "y" an email reply would carry
                answer = "y"
            open_q = {i["id"]: i for i in channel.queue_items(_read(state, "questions.json", {}))}
            if not answer or qid not in open_q or open_q[qid]["code"] != code:
                return self._send(400, "That question is not open, or the answer is empty. Reload the page.")
            if not channel._QID_RE.match(qid) or not channel._CODE_RE.match(code):
                return self._send(400, "Bad request")
            outbox.add(qid, code, answer)  # delivered now if the conductor is between runs, else when it next is
            return self._send(303, "", location="/")

    server = ThreadingHTTPServer((host, port), Handler)
    server.outbox = outbox

    def _pump() -> None:  # deliver held answers as soon as the conductor is between runs
        while True:
            time.sleep(2)
            try:
                outbox.flush()
            except Exception:  # noqa: BLE001 - never let the pump die
                pass

    threading.Thread(target=_pump, daemon=True).start()
    return server


def main(argv: list[str]) -> int:
    from core.agents import load_limits
    forge = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Forge's local status page (127.0.0.1 only)")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)
    state = forge / "state" / "bootstrap"  # the conductor's state (core.bootstrap main)
    srv = make_server(state, state.parent / "channel", load_limits(forge), port=a.port)
    print(f"Forge status page: http://127.0.0.1:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
