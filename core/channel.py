"""Ben's channel (Layer 1E): who hears what, when, and how answers come back.

Spec: docs/specs/layer-1-design.md §6 and D-021 to D-024. Plain code, standard library only. Nothing here sends
mail: the conductor's `_send` stays the only way out (budget, KILL, Message-IDs: R20-R26). This module decides
routing (instant, digest, quiet hours), writes the queue view `queue.jsonl`, builds the digest text, and runs the
answer drop folder that every non-email channel (the status page now, a Dot bridge later) uses to reach Forge.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, tzinfo
from pathlib import Path
from urllib.parse import quote

BODY_CAP, SUBJECT_CAP, ANSWER_CAP = 20000, 300, 2000  # R19 / R28 caps
EXCERPT_CAP = 1000
DROP_FILE_CAP = 20000  # bytes; anything larger in the drop folder is junk
TAKE_LIMIT = 50

# D-023: instant email only when Ben alone can unblock all progress (gate, replan, capability once nothing else can
# run), a spend or subscription needs approval, a safety stop fires, or a customer call needs him.
INSTANT_KINDS = frozenset({"gate", "replan", "capability", "spend", "customer", "tamper"})

# D-023: every question comes with the default Forge uses while Ben hasn't answered.
DEFAULTS = {
    "gate": "Nothing is merged into main. The layer waits for your approval.",
    "replan": "Forge stays paused.",
    "blocked": "The task stays blocked. Other tasks carry on.",
    "merge": "The merge stays blocked. Other tasks carry on, but the layer can't finish.",
    "capability": "Tasks that need it wait. Forge keeps re-checking it.",
    "tamper": "Forge stays stopped until you clear the stop.",
    "spend": "Nothing is bought.",
    "customer": "Nobody answers the customer.",
}
DEFAULT_OTHER = "Forge waits for your answer. Other work carries on."

_QID_RE = re.compile(r"^[\w-]{1,60}$")
_CODE_RE = re.compile(r"^[\w-]{1,40}$")


def is_instant(kind: str) -> bool:
    return kind in INSTANT_KINDS


def default_for(kind: str) -> str:
    return DEFAULTS.get(kind, DEFAULT_OTHER)


def to_local(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Ben's local time. None means the computer's own zone (Ben's PC); tests pass a fixed zone."""
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def is_quiet(local: datetime, start: int = 23, end: int = 7) -> bool:
    """Quiet hours [start, end) in local hours; the window may wrap past midnight. start == end: never quiet."""
    h = local.hour
    if start == end:
        return False
    return start <= h < end if start < end else (h >= start or h < end)


def answer_tag(qid: str, code: str) -> str:
    return f"[Forge Q-{qid} {code}]"


# ---------------------------------------------------------------------- queue view
def _via(q: dict) -> str:
    if q.get("hold"):
        return "digest" if q.get("digest") else "held"
    return "email"


def queue_items(questions: dict) -> list[dict]:
    """The design's queue: one item per open question (id, kind, question, default, deadline, status), plus how it
    reaches Ben (`via`) and its reply `code` (a non-email channel needs it to answer). questions.json stays the
    single source of truth; this is a view of it."""
    out = []
    for qid, q in (questions.items() if isinstance(questions, dict) else []):
        if not isinstance(q, dict) or q.get("status") != "open":
            continue
        kind = str(q.get("kind", ""))
        out.append({"id": str(qid), "kind": kind, "question": str(q.get("subject", ""))[:SUBJECT_CAP],
                    "default": str(q.get("default") or default_for(kind)), "deadline": q.get("deadline"),
                    "status": "open", "via": _via(q), "code": str(q.get("code", "")),
                    "delivered": bool(q.get("delivered")), "halt": bool(q.get("halt"))})
    return out


def write_queue(path: Path, questions: dict) -> None:
    path = Path(path)
    text = "".join(json.dumps(i, sort_keys=True) + "\n" for i in queue_items(questions))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


# ---------------------------------------------------------------------- digest
def build_digest(questions: dict, tasks: list[dict], *, owner: str, local_now: datetime,
                 mail_used: tuple[int, int], mail_caps: tuple[int, int], page_url: str) -> tuple[str, str]:
    """The daily digest (D-023): every open question with its default and how to answer, then a short status.
    Never carries a reply code in its subject, so it can't itself be answered by accident."""
    items = queue_items(questions)
    n = len(items)
    day = local_now.strftime("%Y-%m-%d")
    what = "no open questions" if n == 0 else f"{n} open question{'s' if n != 1 else ''}"
    subject = f"[Forge] Daily digest {day}: {what}"[:SUBJECT_CAP]
    lines = [f"Forge daily digest for {local_now.strftime('%A %d %B %Y, %H:%M')}.", ""]
    if not items:
        lines += ["Nothing is waiting on you.", ""]
    else:
        lines += [f"Waiting on you ({n}):", ""]
        texts = {k: v for k, v in questions.items() if isinstance(v, dict)}
        for i in items:
            tag = answer_tag(i["id"], i["code"])
            body = str(texts.get(i["id"], {}).get("body", "")).strip()
            excerpt = body[:EXCERPT_CAP] + (" ..." if len(body) > EXCERPT_CAP else "")
            mailto = f"mailto:{owner}?subject={quote(tag + ' answer', safe='')}"
            block = [f"* {i['question']}  ({i['kind']})"]
            if excerpt:
                block += ["  " + ln for ln in excerpt.splitlines()]
            block += [f"  If you don't answer: {i['default']}",
                      f"  To answer: send an email with the subject {tag} and your answer in the body",
                      f"  ({mailto}), or use the status page."]
            lines += block + [""]
    counts: dict[str, int] = {}
    for t in tasks or []:
        if isinstance(t, dict):
            counts[str(t.get("status", "?"))] = counts.get(str(t.get("status", "?")), 0) + 1
    if counts:
        lines.append("Tasks: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ".")
        blocked = [f"{t.get('id')} {t.get('title', '')}" for t in tasks if isinstance(t, dict)
                   and t.get("status") == "blocked"]
        if blocked:
            lines.append("Blocked: " + "; ".join(blocked)[:1000])
    lines += [f"Email used: {mail_used[0]} of {mail_caps[0]} this hour, {mail_used[1]} of {mail_caps[1]} today.",
              f"Status page (on your PC): {page_url}",
              "To stop everything: reply STOP to any Forge email."]
    tail = "\n\n" + "\n".join(lines[-3:])
    body = "\n".join(lines)
    if len(body) > BODY_CAP:  # keep the stop line and the page link whatever happens
        body = body[:BODY_CAP - len(tail) - 5].rstrip() + "\n..." + tail
    return subject, body


# ---------------------------------------------------------------------- answer drop folder
def drop_answer(folder: Path, qid: str, code: str, answer: str, source: str) -> Path:
    """Hand Forge an answer from a non-email channel. Written atomically; Forge checks qid and code itself."""
    if not isinstance(qid, str) or not _QID_RE.match(qid):
        raise ValueError("bad question id")
    if not isinstance(code, str) or not _CODE_RE.match(code):
        raise ValueError("bad code")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.json"
    raw = json.dumps({"qid": qid, "code": code, "answer": str(answer)[:ANSWER_CAP],
                      "source": str(source)[:40]}).encode("utf-8")
    tmp = folder / (name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, folder / name)
    return folder / name


def take_answers(folder: Path, limit: int = TAKE_LIMIT) -> list[dict]:
    """Read and remove up to `limit` dropped answers, oldest first. Junk is removed and never returned; a file that
    can't be removed is not returned either (so it can never be applied twice)."""
    folder = Path(folder)
    try:
        names = sorted(p for p in folder.iterdir() if p.is_file() and p.name.endswith(".json"))
    except OSError:
        return []
    out = []
    for p in names[:limit]:
        item = None
        try:
            if p.stat().st_size <= DROP_FILE_CAP:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and all(isinstance(data.get(k), str) for k in ("qid", "code", "answer")):
                    item = {"qid": data["qid"], "code": data["code"], "answer": data["answer"][:ANSWER_CAP],
                            "source": str(data.get("source") or "unknown")[:40]}
        except (OSError, ValueError):
            item = None
        try:
            p.unlink()
        except OSError:
            continue
        if item is not None:
            out.append(item)
    return out
