"""Forge's local status page (Layer 1E; design §6, D-022, D-023, D-024).

    python -m core.status_page            # http://127.0.0.1:8765

Served on this PC only: it binds 127.0.0.1 and nothing else, refuses any request whose Host isn't 127.0.0.1 or
localhost on its port (DNS rebinding), refuses cross-site POSTs, and forbids framing. It reads the conductor's state
files and never writes them, with one exception: the Stop button creates `state/KILL` (D-024). Answers are not
applied here; they go to the conductor through the answer drop folder (core.channel), which checks each one exactly
like an email reply. Standard library only.

R60 lanes: with the shared folder (state/shared) the usage bars are the shared meter's and mail budget's (every lane
together), Stop also writes the global KILL that stops every lane, and a "Lanes" section shows each other lane's
queue and current task. Answers on this page are for the main lane's questions; other lanes are answered by email.

R64 live dashboard: the page opens with a live section (core.dashboard.snapshot: who is doing what in every lane, ETAs
with their basis, checkpoint and project progress, tokens and burn rate, a 12-hour Gantt of runs, the spec coverage
grid). GET /api/live returns the snapshot as JSON and /api/live?format=html the live section, which a small inline
script (allowed by a per-response CSP nonce) fetches every 3 s. Without JavaScript the page reloads every 30 s.
"""
from __future__ import annotations

import argparse
import html
import json
import secrets
import time
import threading
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from core import channel, dashboard, lanes, readiness
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
.bar{height:14px;background:var(--line);border-radius:7px;overflow:hidden;margin:4px 0 2px}.fill{height:100%;background:var(--ok);border-radius:7px}.fill.warn{background:var(--warn)}.fill.bad{background:var(--bad)}
.meter{margin:10px 0}.meter .lbl{display:flex;justify-content:space-between;gap:8px}
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
    folder = Path(state) / "mail"  # R60: with lanes, every lane's own mail file in state/shared/mail/
    logs = [_read(folder, f.name, {}) for f in sorted(folder.glob("*.json")) if not f.name.startswith(".")] \
        if folder.is_dir() else [_read(state, "mail_log.json", {})]
    for x in [x for log in logs for x in log.get("sent", [])]:
        try:
            age = (now - datetime.fromisoformat(x)).total_seconds()
        except (TypeError, ValueError):
            continue
        hour += age < 3600
        day += age < 86400
    return hour, day


def _bar(label: str, used: float, total: float, detail: str, usage: bool = False) -> str:
    """One labelled bar. Progress bars are green; usage bars turn amber at 70% and red at 90%."""
    frac = 0.0 if not total else max(0.0, min(1.0, used / total))
    cls = "fill" + ((" bad" if frac >= 0.9 else " warn" if frac >= 0.7 else "") if usage else "")
    return (f"<div class=meter><div class=lbl><span>{_e(label)}</span><span class=muted>{_e(detail)}</span></div>"
            f"<div class=bar role=progressbar aria-label=\"{_e(label)}\" aria-valuenow=\"{round(frac * 100)}\" "
            f"aria-valuemin=0 aria-valuemax=100><div class=\"{cls}\" style=\"width:{frac * 100:.1f}%\"></div></div></div>")


def progress(forge_root: Path, tasks: list[dict]) -> dict:
    """Roadmap progress: finished phases plus the fraction of the current phase's queue that is done."""
    doc = _read(Path(forge_root) / "docs", "progress.json", {})
    phases = [p for p in doc.get("phases", []) if isinstance(p, dict)] if isinstance(doc, dict) else []
    done = sum(1 for p in phases if p.get("done"))
    live = [t for t in tasks if t.get("status") not in ("superseded",)]
    tdone = sum(1 for t in live if t.get("status") == "done")
    frac = (tdone / len(live)) if live and done < len(phases) else 0.0
    current = next((p.get("name", "") for p in phases if not p.get("done")), "")
    return {"phases": len(phases), "done": done, "current": current, "tasks": len(live), "tasks_done": tdone,
            "overall": ((done + frac) / len(phases)) if phases else 0.0}


def _lanes_html(state_root: Path) -> str:
    """R60: each lane other than main: its layer, current task, flags and queue."""
    out = []
    for name in lanes.listed(state_root)[1:]:
        st = lanes.state_dir(state_root, name)
        q = _read(st, "queue.json", {})
        tasks = [t for t in q.get("tasks", []) if isinstance(t, dict)]
        current = next((t for t in tasks if t.get("status") in ("todo", "tests_ok")), None)
        flags = [f for f in ("KILL", "PAUSED") if (st / f).exists()]
        done = sum(1 for t in tasks if t.get("status") == "done")
        open_q = len(channel.queue_items(_read(st, "questions.json", {})))
        head = (f"<div class=card><div><strong>Lane {_e(name)}</strong> <span class=muted>"
                f"{_e(q.get('layer') or 'no queue')} &middot; {done} of {len(tasks)} done"
                f"{' &middot; ' + _e(', '.join(flags)) + ' set' if flags else ''}"
                f"{f' &middot; {open_q} question(s) for you (answer by email)' if open_q else ''}</span></div>")
        now_line = (f"<p>Now: <strong>{_e(current.get('id'))}</strong> {_e(current.get('title', ''))} "
                    f"<span class=muted>({_e(current.get('status'))})</span></p>") if current else ""
        rows = "".join(f"<tr><td>{_e(t.get('id'))}</td><td>{_e(t.get('title', ''))}</td>"
                       f"<td>{_e(t.get('status'))}</td></tr>" for t in tasks)
        table = (f"<div class=row><table><tr><th>Task</th><th>Title</th><th>Status</th></tr>{rows}</table></div>"
                 if tasks else "<p class=muted>No tasks queued.</p>")
        out.append(head + now_line + table + "</div>")
    return ("<h2>Lanes</h2>" + "".join(out)) if out else ""


# ------------------------------------------------------------------ R64 live dashboard
_LIVE_CSS = """
:root{--r-planner:#6d4ed8;--r-test_writer:#0f7f81;--r-builder:#c25e00;--r-judge:#8a6d00;--r-reviewer:#2a63c9;
--r-troubleshooter:#b0289f;--r-drift_keeper:#5f6670;--r-manager:#4d7a12;--r-merge:#1e6b34;--r-other:#7a7a74;
--cov-covered:#1e6b34;--cov-partial:#c79100;--cov-open:#d9d7cf;--cov-unclaimed:transparent}
@media (prefers-color-scheme:dark){:root{--r-planner:#a48cff;--r-test_writer:#3cc2c4;--r-builder:#ff9a3d;
--r-judge:#e0c04a;--r-reviewer:#6ea0ff;--r-troubleshooter:#e86ad6;--r-drift_keeper:#a3a9b2;--r-manager:#9bd25a;
--r-merge:#6fcf8a;--r-other:#a3a29c;--cov-covered:#4fbf72;--cov-partial:#e7b35a;--cov-open:#45443f}}
#live .lanes{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr))}
.lanehead{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}.pill{border-radius:999px;padding:1px 9px;
font-size:13px;font-weight:600;border:1px solid currentColor}.st-running{color:var(--ok)}.st-stopped{color:var(--bad)}
.st-paused,.st-capped{color:var(--warn)}.st-idle,.st-unknown{color:var(--muted)}
.who{display:flex;gap:10px;align-items:flex-start;margin:10px 0 4px}.badge{flex:none;width:34px;height:34px;
border-radius:50%;background:var(--rc,var(--r-other));color:#fff;display:grid;place-items:center;font-weight:700;
font-size:15px}.rolename{color:var(--rc,var(--r-other));font-weight:700}.small{font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}.chip{font-size:12px;padding:1px 8px;border-radius:999px;
border:1px solid var(--line);color:var(--muted)}.chip.past{background:var(--line)}.chip.now{border-color:var(--rc);
color:var(--rc);font-weight:700}.fill.live{background:var(--rc,var(--ok))}
.ganttwrap{overflow-x:auto}.gantt{width:100%;min-width:640px;height:auto;display:block}.gantt text{fill:var(--muted);font:11px system-ui,sans-serif}
.gantt .grid{stroke:var(--line)}.gantt .nowline{stroke:var(--bad);stroke-width:1.5}
.gantt .run.live{stroke:var(--fg);stroke-width:1}.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.covgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(42px,1fr));gap:3px;margin:6px 0}
.cell{font-size:11px;text-align:center;padding:5px 0;border-radius:4px;border:1px solid var(--line);
background:var(--cov-unclaimed)}.cell.cov-covered{background:var(--cov-covered);color:#fff;border-color:transparent}
.cell.cov-partial{background:var(--cov-partial);color:#1d1d1b;border-color:transparent}
.cell.cov-open{background:var(--cov-open)}
"""
ROLE_LETTER = {"planner": "P", "test_writer": "T", "builder": "B", "judge": "J", "reviewer": "R",
               "troubleshooter": "S", "drift_keeper": "D", "manager": "M", "merge": "G"}
ROLE_NAME = {"test_writer": "Test writer", "drift_keeper": "Drift keeper", "judge": "Judges"}


def _role_var(role) -> str:
    r = str(role or "")
    return f"var(--r-{r})" if r in ROLE_LETTER else "var(--r-other)"


def _role_label(role) -> str:
    r = str(role or "unknown")
    if r in ROLE_NAME:
        return ROLE_NAME[r]
    return r.replace("_", " ").capitalize() if r in ROLE_LETTER else r


def _dur(s) -> str:
    from core.dashboard import fmt_dur
    return fmt_dur(s)


def _elapsed(s: float) -> str:
    """A ticking timer's text: seconds shown under an hour (the page's script uses the same format)."""
    s = max(0, int(s))
    return f"{s // 60}m {s % 60:02d}s" if s < 3600 else _dur(s)


def _clock(iso, local_tz, fmt: str = "%H:%M") -> str:
    try:
        return channel.to_local(datetime.fromisoformat(iso), local_tz).strftime(fmt)
    except (TypeError, ValueError):
        return "?"


def _live_lane(lane: dict, med: dict, local_tz) -> str:
    st = str(lane.get("state") or "unknown")
    cond = lane.get("conductor") or {}
    age = cond.get("heartbeat_age_s")
    hb = f"heartbeat {_dur(age)} ago" if age is not None else "no heartbeat"
    out = [f"<div class='card lane'><div class=lanehead><strong>Lane {_e(lane.get('name'))}</strong>"
           f"<span class='pill st-{_e(st)}'>{_e(st)}</span><span class='muted small'>"
           f"{_e(lane.get('layer') or 'no queue')} &middot; {_e(hb)}"
           + (f" &middot; {_e(lane['open_questions'])} question(s) for you" if lane.get("open_questions") else "")
           + "</span></div>"]
    if lane.get("error"):
        out.append(f"<p class=bad>Could not read this lane: {_e(lane['error'])}</p>")
    cur = lane.get("current")
    tasks = {t.get("id"): t for t in lane.get("tasks") or []}
    if cur:
        role, rv = cur.get("role"), _role_var(cur.get("role"))
        t = tasks.get(cur.get("task_id")) or {}
        what = (f"<strong>{_e(cur['task_id'])}</strong> {_e(cur.get('task_title') or '')}" if cur.get("task_id")
                else "<span class=muted>Forge's own upkeep (no queue task)</span>")
        m = (med.get(role) or {}) if isinstance(med, dict) else {}
        el = float(cur.get("elapsed_s") or 0)
        total = float(m.get("median_s") or 0)
        frac = min(1.0, el / total) if total else 0.0
        over = bool(total) and el > total
        stage_txt = (f"running longer than its median ({_dur(total)}): no estimate left for this stage" if over
                     else f"about {_dur(total - el)} left in this stage") if total else "no stage estimate"
        src = (f"median of {m['n']} {role} runs" if m.get("n") else "default, no history yet") if total else ""
        out.append(
            f"<div class=who style='--rc:{rv}'><span class=badge aria-hidden=true>"
            f"{_e(ROLE_LETTER.get(str(role), '?'))}</span><div><div><span class=rolename>{_e(_role_label(role))}"
            f"</span> {'are' if role == 'judge' else 'is'} working on {what}</div>"
            f"<div class='muted small'>started {_e(_clock(cur.get('started'), local_tz))} &middot; "
            f"<span data-since=\"{_e(cur.get('started'))}\">{_e(_elapsed(el))}</span> elapsed</div></div></div>"
            f"<div class=meter><div class=lbl><span class=small>Stage: {_e(stage_txt)}</span>"
            f"<span class='muted small'>estimate{': ' + _e(src) if src else ''}</span></div>"
            f"<div class=bar role=progressbar aria-label=\"stage progress\" aria-valuenow=\"{round(frac * 100)}\" "
            f"aria-valuemin=0 aria-valuemax=100 style='--rc:{rv}'><div class=\"fill {'warn' if over else 'live'}\" "
            f"data-since=\"{_e(cur.get('started'))}\" data-total=\"{total:.0f}\" style=\"width:{frac * 100:.1f}%\">"
            "</div></div></div>")
        if t:
            stages = ("planner", "reviewer") if t.get("kind") == "plan" else \
                ("test_writer", "builder", "judge", "reviewer", "merge")
            now_i = stages.index(t["stage"]) if t.get("stage") in stages else -1
            chips = "".join(f"<span class='chip{' now' if i == now_i else ' past' if i < now_i else ''}' "
                            f"style='--rc:{_role_var(s)}'>{_e(_role_label(s))}</span>" for i, s in enumerate(stages))
            out.append(f"<div class=chips aria-label=\"task stages\">{chips}</div>")
            if t.get("eta_s") is not None:
                when = (f"done in about <strong>{_e(_dur(t['eta_s']))}</strong>" if t["eta_s"] >= 60 else
                        "<strong>past its estimate</strong>: by the medians it would be done by now")
                out.append(f"<div class=small>Task {_e(t['id'])} {when} "
                           f"<span class=muted>({_e(t.get('eta_basis'))})</span></div>")
    else:
        why = {"stopped": "Nothing runs: a stop is set or the conductor is not running.",
               "paused": "Nothing runs while the lane is paused.",
               "capped": "Waiting for a token cap or limit to reset.", "idle": "Nobody is working right now."}
        out.append(f"<p class=muted>{_e(why.get(st, 'Unknown.'))}</p>")
    out.append(_live_checkpoint(lane.get("checkpoint") or {}))
    rows = "".join(
        f"<tr><td>{_e(t.get('id'))}</td><td>{_e(t.get('title'))}</td><td>{_e(t.get('status'))}</td>"
        f"<td>{_e(_role_label(t['stage']) if t.get('stage') else '-')}</td>"
        f"<td title=\"{_e(t.get('eta_basis'))}\">{_e(_task_eta_cell(t))}"
        "</td></tr>" for t in lane.get("tasks") or [])
    if rows:
        out.append("<details><summary class=small>Tasks</summary><div class=row><table><tr><th>Task</th>"
                   f"<th>Title</th><th>Status</th><th>Stage</th><th>ETA (estimate)</th></tr>{rows}</table></div>"
                   "</details>")
    return "".join(out) + "</div>"


def _eta_line(eta, basis) -> str:
    return (f"<div class=small>Done in about <strong>{_e(_dur(eta))}</strong> of agent and judge time "
            f"<span class=muted>({_e(basis or 'estimate')})</span></div>") if eta is not None else \
        "<div class='small muted'>ETA unknown</div>"


def _task_eta_cell(t: dict) -> str:
    if t.get("status") == "done":
        return "-"
    eta = t.get("eta_s")
    return "unknown" if eta is None else "past estimate" if eta < 60 else _dur(eta)


def _live_checkpoint(cp: dict) -> str:
    out = ["<h3 class=small>Next checkpoint: " + _e(cp.get("layer") or "unknown") + " complete</h3>"]
    td, tt = cp.get("tasks_done"), cp.get("tasks_total")
    if tt is not None:
        out.append(_bar("Tasks done", td or 0, tt or 1, f"{td} of {tt}"))
    else:
        out.append("<div class='small bad'>Tasks unknown: the queue can't be read right now.</div>")
    if cp.get("requirements_total"):
        out.append(_bar("Spec requirements covered", cp.get("covered") or 0, cp["requirements_total"],
                        f"{cp.get('covered')} of {cp['requirements_total']} covered, {cp.get('partial') or 0} partial"
                        f" ({cp.get('spec_file')})"))
    else:
        out.append("<div class='small muted'>Spec coverage unknown (no usable spec).</div>")
    out.append(_eta_line(cp.get("eta_s"), cp.get("eta_basis")))
    return "".join(out)


def _live_project(pr: dict) -> str:
    phases = pr.get("phases") or []
    if not phases:
        return "<p class=muted>Project phases unknown (docs/progress.json is missing or can't be read).</p>"
    n = len(phases)
    segs = []
    for i, p in enumerate(phases):
        f = 1.0 if p.get("done") else (pr.get("current_fraction") or 0.0) if p.get("name") == pr.get("current_phase") \
            else 0.0
        segs.append(f"<div title=\"{_e(p.get('name'))}: {round(f * 100)}%\" style=\"flex:1;height:100%;"
                    f"border-right:{'2px solid var(--bg)' if i < n - 1 else '0'};background:linear-gradient(90deg,"
                    f"var(--ok) {f * 100:.1f}%,transparent {f * 100:.1f}%)\"></div>")
    if pr.get("overall") is None:
        return ("<div class=meter><div class=lbl><span>Whole project: unknown</span><span class=muted>"
                f"{_e(pr.get('phases_done'))} of {_e(pr.get('phases_total'))} phases done; the current phase's progress "
                "can't be read right now</span></div><div class=bar></div></div>")
    overall = pr.get("overall") or 0.0
    return (f"<div class=meter><div class=lbl><span>Whole project: {round(overall * 100)}%</span><span class=muted>"
            f"{_e(pr.get('phases_done'))} of {_e(pr.get('phases_total'))} phases done; now "
            f"{_e(pr.get('current_phase') or 'all done')} ({round((pr.get('current_fraction') or 0) * 100)}% by spec "
            f"coverage)</span></div><div class=bar role=progressbar aria-label=\"whole project\" "
            f"aria-valuenow=\"{round(overall * 100)}\" aria-valuemin=0 aria-valuemax=100 style=\"display:flex\">"
            + "".join(segs) + "</div></div>"
            + (f"<div class=small>Current phase done in about <strong>{_e(_dur(pr.get('eta_s')))}</strong> "
               f"<span class=muted>({_e(pr.get('eta_basis'))})</span></div>" if pr.get("eta_s") is not None else ""))


def _live_tokens(tok: dict, now_iso, local_tz) -> str:
    out = []
    reset = _clock(tok.get("resets_at"), local_tz)
    for p, v in (tok.get("providers") or {}).items():
        used, cap = v.get("used"), v.get("cap")
        burn, ttc = v.get("burn_per_h") or 0, v.get("time_to_cap_s")
        if used is None:
            detail = "usage unknown (meter unreadable)"
        else:
            detail = f"{used / 1e6:.2f}M of {(cap or 0) / 1e6:.0f}M today; {burn / 1e3:.0f}k/h in the last hour"
        if v.get("held_until"):
            rate = f"on hold until {_clock(v['held_until'], local_tz)} (provider limit)"
        elif ttc == 0:
            rate = "at the cap until the reset"
        elif ttc is None:
            rate = "no burn in the last hour"
        else:
            try:
                left = (datetime.fromisoformat(tok["resets_at"]) - datetime.fromisoformat(now_iso)).total_seconds()
            except (KeyError, TypeError, ValueError):
                left = None
            rate = (f"cap in about {_dur(ttc)} at this rate (estimate)" if left is None or ttc < left
                    else f"won't reach the cap before the reset at {reset} at this rate (estimate)")
        lanes_txt = ", ".join(f"{k} {(x or 0) / 1e6:.2f}M" for k, x in (v.get("by_lane") or {}).items()
                              if x is not None)
        out.append(_bar(f"{p.capitalize()} tokens", used or 0, cap or 1, detail, usage=True)
                   + f"<div class='small muted'>{_e(rate)}{' &middot; by lane: ' + _e(lanes_txt) if lanes_txt else ''}"
                   f" &middot; resets {_e(reset)}</div>")
    if tok.get("runs_cap"):
        out.append(_bar("Agent runs today", tok.get("runs_today") or 0, tok["runs_cap"],
                        f"{tok.get('runs_today')} of {tok['runs_cap']}", usage=True))
    return "".join(out)



def _live_gantt(tl: dict, local_tz) -> str:
    try:
        t0, t1 = datetime.fromisoformat(tl["from"]), datetime.fromisoformat(tl["to"])
    except (KeyError, TypeError, ValueError):
        return "<p class=muted>Timeline unknown.</p>"
    span = max(1.0, (t1 - t0).total_seconds())
    lanes_ = tl.get("lanes") or {}
    left, width, row, top = 70, 920, 32, 18
    h = top + row * max(1, len(lanes_)) + 6
    x = lambda t: left + width * max(0.0, min(1.0, (t - t0).total_seconds() / span))  # noqa: E731
    out = [f"<svg class=gantt viewBox=\"0 0 1000 {h}\" role=img aria-label=\"Agent runs in the last 12 hours, "
           f"per lane\">"]
    tick = t0.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while tick < t1:
        if tick.hour % 2 == 0:
            xx = x(tick)
            out.append(f"<line class=grid x1=\"{xx:.1f}\" x2=\"{xx:.1f}\" y1=\"{top - 4}\" y2=\"{h - 4}\"/>"
                       f"<text x=\"{xx:.1f}\" y=\"11\" text-anchor=middle>{_e(_clock(tick.isoformat(), local_tz))}"
                       "</text>")
        tick += timedelta(hours=1)
    seen = set()
    for i, (name, runs) in enumerate(lanes_.items()):
        y = top + i * row
        out.append(f"<text x=\"4\" y=\"{y + row / 2 + 4:.1f}\">{_e(name)}</text>")
        for r in runs or []:
            try:
                s = datetime.fromisoformat(r["start"])
                e = datetime.fromisoformat(r["end"]) if r.get("end") else (t1 if r.get("running") else None)
            except (KeyError, TypeError, ValueError):
                continue
            if e is None:
                continue
            seen.add(r.get("role") if r.get("role") in ROLE_LETTER else "other")
            x1, x2 = x(s), x(e)
            dur = (e - s).total_seconds()
            tip = (f"{_role_label(r.get('role'))} {r.get('task_id') or ''} {_clock(r['start'], local_tz)}"
                   f"-{_clock(r['end'], local_tz) if r.get('end') else 'now'} ({_dur(dur)})"
                   + (f", {r['tokens']:,} {r.get('provider') or ''} tokens" if r.get("tokens") else "")
                   + (", failed" if r.get("ok") is False else ""))
            out.append(f"<rect class=\"run{' live' if r.get('running') else ''}\" x=\"{x1:.1f}\" y=\"{y + 3}\" "
                       f"width=\"{max(2.0, x2 - x1):.1f}\" height=\"{row - 8}\" rx=2 "
                       f"style=\"fill:{_role_var(r.get('role'))}\"><title>{_e(tip)}</title></rect>")
    out.append(f"<line class=nowline x1=\"{left + width}\" x2=\"{left + width}\" y1=\"{top - 4}\" y2=\"{h - 2}\"/>"
               "</svg>")
    legend = "".join(f"<span><i style=\"background:{_role_var(r)}\"></i>{_e(_role_label(r))}</span>"
                     for r in list(ROLE_LETTER) + ["other"] if r in seen)
    return "<div class=ganttwrap>" + "".join(out) + "</div>" + (f"<div class=legend>{legend}<span class=muted>red line: now</span></div>" if legend else
                           "<p class='muted small'>No agent runs in the last 12 hours.</p>")


def _live_coverage(lanes_: list[dict]) -> str:
    out, done = [], set()
    for lane in lanes_:
        cp = lane.get("checkpoint") or {}
        reqs = cp.get("requirements") or []
        if not reqs or cp.get("spec_file") in done:
            continue
        done.add(cp.get("spec_file"))
        cells = "".join(f"<span class=\"cell cov-{_e(r.get('status'))}\" title=\"{_e(r.get('id'))} "
                        f"{_e(r.get('status'))}: {_e(r.get('text'))}\">{_e(r.get('id'))}</span>" for r in reqs)
        out.append(f"<div class=small>{_e(cp.get('spec_file'))}: {_e(cp.get('covered'))} of {len(reqs)} covered"
                   f"</div><div class=covgrid role=list aria-label=\"spec requirements\">{cells}</div>")
    if not out:
        return "<p class=muted>No spec coverage to show.</p>"
    return "".join(out) + ("<div class=legend><span><i style=\"background:var(--cov-covered)\"></i>covered</span>"
                           "<span><i style=\"background:var(--cov-partial)\"></i>partial</span>"
                           "<span><i style=\"background:var(--cov-open)\"></i>claimed, not done</span>"
                           "<span><i style=\"border:1px solid var(--line)\"></i>no task yet</span></div>")


def live_section(snap: dict, local_tz=None) -> str:
    """The live dashboard (R64) as one <section id=live>: the page embeds it and the script swaps it in place."""
    try:
        lanes_ = snap.get("lanes") or []
        med = snap.get("stage_medians") or {}
        upd = _clock(snap.get("generated_at"), local_tz, "%H:%M:%S")
        body = (f"<h2>Live</h2><p class='muted small'>Updated {_e(upd)} &middot; every ETA is an estimate from this "
                "PC's own run history</p><div class=lanes>" + "".join(_live_lane(x, med, local_tz) for x in lanes_)
                + "</div><h2>Whole project</h2>" + _live_project(snap.get("project") or {})
                + "<h2>Tokens today</h2>" + _live_tokens(snap.get("tokens") or {}, snap.get("generated_at"), local_tz)
                + "<h2>Who did what (last 12 hours)</h2>" + _live_gantt(snap.get("timeline") or {}, local_tz)
                + "<h2>Spec coverage</h2>" + _live_coverage(lanes_))
    except Exception as e:  # noqa: BLE001 - the dashboard never takes the page down
        body = f"<h2>Live</h2><p class=bad>Could not build the live view: {_e(type(e).__name__)}: {_e(e)}</p>"
    main = (snap.get("lanes") or [{}])[0] if isinstance(snap, dict) else {}
    qsig = ",".join(main.get("open_question_ids") or []) if isinstance(main, dict) else ""
    return f"<section id=live aria-live=polite data-questions=\"{_e(qsig)}\">{body}</section>"


_LIVE_JS = """(function(){
function fmt(s){s=Math.max(0,Math.floor(s));if(s<3600)return Math.floor(s/60)+'m '+(s%60<10?'0':'')+s%60+'s';
if(s<86400){var m=Math.floor(s%3600/60);return Math.floor(s/3600)+'h '+(m<10?'0':'')+m+'m';}
return Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h';}
function tick(){var now=Date.now();document.querySelectorAll('[data-since]').forEach(function(el){
var t=Date.parse(el.getAttribute('data-since'));if(isNaN(t))return;var s=(now-t)/1000,tot=+el.getAttribute('data-total');
if(el.hasAttribute('data-total')){if(tot>0)el.style.width=Math.min(100,100*s/tot).toFixed(1)+'%';}
else el.textContent=fmt(s);});}
var busy=false,lost=document.getElementById('live-lost'),first=document.getElementById('live'),
q0=first?first.getAttribute('data-questions'):null,qnote=document.getElementById('live-questions');
function typing(){var a=document.activeElement;if(a&&(a.tagName==='TEXTAREA'||a.tagName==='INPUT'))return true;
return Array.prototype.some.call(document.querySelectorAll('textarea'),function(t){return t.value.trim()!=='';});}
function poll(){if(busy)return;busy=true;fetch('/api/live?format=html',{cache:'no-store',credentials:'same-origin'})
.then(function(r){if(!r.ok)throw new Error(r.status);return r.text();}).then(function(t){
var el=document.getElementById('live');if(el&&t.indexOf('<section id=live')===0)el.outerHTML=t;
if(lost)lost.hidden=true;tick();var n=document.getElementById('live');
if(n&&q0!==null&&n.getAttribute('data-questions')!==q0){if(!typing())location.reload();else if(qnote)qnote.hidden=false;}}).catch(function(){if(lost)lost.hidden=false;})
.then(function(){busy=false;});}
setInterval(poll,3000);setInterval(tick,1000);tick();})();"""


def render(state: Path, limits: dict, now: datetime | None = None, *, local_tz=None,
           shared: Path | None = None, forge_root: Path | None = None, nonce: str | None = None) -> str:
    """The whole page as HTML. Pure apart from reading state files; every value is escaped.
    R60: with `shared`, usage is every lane's together and the other lanes are listed."""
    state = Path(state)
    mail_state = Path(shared) if shared is not None else state
    now = now or datetime.now(timezone.utc)
    local = channel.to_local(now, local_tz)
    out = [f"<h1>Forge status</h1><p class=muted>{_e(local.strftime('%A %d %B %Y, %H:%M'))} "
           "&middot; live (without JavaScript: refreshes every 30 seconds) "
           "<span id=live-lost class=bad hidden>&middot; lost contact with the page server, retrying</span>"
           "<span id=live-questions class=warn hidden>&middot; your questions changed: reload the page when you "
           "finish typing</span></p>"]
    try:  # R64: the live dashboard first; it never takes the page down
        snap = dashboard.snapshot(Path(forge_root) if forge_root is not None else state.parent.parent, now)
        out.append(live_section(snap, local_tz))
    except Exception as e:  # noqa: BLE001
        out.append(f"<section id=live><p class=bad>Live view unavailable: {_e(type(e).__name__)}</p></section>")
    tasks_all = [t for t in _read(state, "queue.json", {}).get("tasks", []) if isinstance(t, dict)]
    pr = progress(state.parent.parent, tasks_all)
    out.append("<h2>Progress</h2>")
    if pr["phases"]:
        out.append(_bar("Whole build (roadmap)", pr["overall"], 1.0,
                        f"{pr['done']} of {pr['phases']} phases done; now: {pr['current'] or 'all done'}"))
    out.append(_bar("Current phase's queue", pr["tasks_done"], pr["tasks"] or 1,
                    f"{pr['tasks_done']} of {pr['tasks']} tasks done"))
    if shared is not None:
        from core.service import shared_meter
        meter0 = shared_meter(Path(shared), lambda: now)
    else:
        meter0 = Meter(state, clock=lambda: now)
    reset = channel.to_local(now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1), local_tz)
    out.append("<h2>Usage today</h2>")
    for p in PROVIDERS:
        cap = limits.get(f"{p}_daily_token_cap")
        used = meter0.used_today(p)
        out.append(_bar(f"{p.capitalize()} tokens", used, cap or 1,
                        f"{used / 1e6:.1f}M of {(cap or 0) / 1e6:.0f}M (resets {reset.strftime('%H:%M')})", usage=True))
    rcap = limits.get("agent_runs_per_day")
    if rcap:
        try:
            runs = meter0.runs_today()
        except Exception:  # noqa: BLE001 - the page never breaks on a counter
            runs = 0
        out.append(_bar("Agent runs", runs, rcap, f"{runs} of {rcap}", usage=True))
    mh0, md0 = _mail_used(mail_state, now)
    out.append(_bar("Email to you", md0, limits.get("mail_per_day", 30),
                    f"{md0} of {limits.get('mail_per_day', 30)} today, {mh0} this hour", usage=True))
    if (state / "KILL").exists() or (shared is not None and (Path(shared) / "KILL").exists()):
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

    meter = meter0
    rows = []
    for p in PROVIDERS:
        cap = limits.get(f"{p}_daily_token_cap")
        rows.append(f"<tr><td>{p} tokens today</td><td>{meter.used_today(p)} of {_e(cap if cap is not None else '-')}"
                    "</td></tr>")
    mh, md = _mail_used(mail_state, now)
    rows.append(f"<tr><td>email</td><td>{mh} of {_e(limits.get('mail_per_hour', 6))} this hour, "
                f"{md} of {_e(limits.get('mail_per_day', 30))} today</td></tr>")
    out.append("<h2>Caps</h2><div class=row><table>" + "".join(rows) + "</table></div>")

    if shared is not None:
        out.append(_lanes_html(Path(shared).parent))
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
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<noscript><meta http-equiv=refresh content=30></noscript>"
            "<title>Forge status</title>"
            f"<style>{_CSS}{_LIVE_CSS}</style></head><body><main>" + "".join(out) + "</main>"
            + (f"<script nonce=\"{_e(nonce)}\">{_LIVE_JS}</script>" if nonce else "") + "</body></html>")


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
                local_tz=None, outbox: "AnswerOutbox | None" = None,
                shared: Path | None = None, forge_root: Path | None = None) -> ThreadingHTTPServer:
    """A server bound to the loopback address only (D-022). port=0 picks a free port (tests)."""
    if host not in LOOPBACK:
        raise ValueError(f"the status page binds 127.0.0.1 only, not {host!r}")
    state, drop = Path(state), Path(channel_dir) / "in"
    root = Path(forge_root) if forge_root is not None else state.parent.parent  # R64: what the dashboard reads
    outbox = outbox or AnswerOutbox(drop, state.parent / "service")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ForgeStatus"
        sys_version = ""

        def log_message(self, *_a) -> None:  # quiet: no console output from a background page
            pass

        def _allowed_hosts(self) -> set[str]:
            p = self.server.server_address[1]
            return {f"127.0.0.1:{p}", f"localhost:{p}"} | ({"127.0.0.1", "localhost"} if p == 80 else set())

        def _send(self, code: int, body: str = "", location: str | None = None, *,
                  ctype: str = "text/html; charset=utf-8", nonce: str | None = None) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            script = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if nonce else ""
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; " + script + "form-action 'self'; "
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
            path, _, query = self.path.partition("?")
            if path == "/api/live":  # R64: the live snapshot (JSON), or the live section for the page's script
                try:
                    snap = dashboard.snapshot(root)
                    if parse_qs(query).get("format") == ["html"]:
                        return self._send(200, live_section(snap, local_tz))
                    return self._send(200, json.dumps(snap), ctype="application/json; charset=utf-8")
                except Exception as e:  # noqa: BLE001 - never crash the server
                    return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}),
                                      ctype="application/json; charset=utf-8")
            if path != "/":
                return self._send(404, "Not found")
            nonce = secrets.token_urlsafe(18)
            try:
                page = render(state, limits, local_tz=local_tz, shared=shared, forge_root=root, nonce=nonce)
            except Exception as e:  # noqa: BLE001 - a broken file never takes the page down
                page = f"<!doctype html><title>Forge status</title><p>Could not read Forge's state: {_e(e)}</p>"
            self._send(200, page, nonce=nonce)

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
                left = min(max(n, 0), 1_000_000)  # drain (bounded) so the client reads the 413, not a reset (Windows)
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                self.close_connection = True
                return self._send(413, "Too large")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            if path == "/stop":  # D-024: one of the three equal ways to stop Forge
                (state / "KILL").write_text("stopped from the status page\n", encoding="utf-8")
                if shared is not None:  # R60: the global KILL stops every lane
                    (Path(shared) / "KILL").write_text("stopped from the status page\n", encoding="utf-8")
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
    srv = make_server(state, state.parent / "channel", load_limits(forge), port=a.port,
                      shared=lanes.shared_dir(state.parent), forge_root=forge)
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
