"""Spec coverage map (D-026 "spec coverage must rise", layer-1 design §3.7). Plain code, no AI.

Requirements are the independently checkable items of the spec: inside every `## N. Title` section, each
top-level list item or table body row is requirement `N.k` (k counts from 1 in document order); a numbered
section with no items is the single requirement `N`. Nested items, code blocks, sub-headings and unnumbered
sections are not requirements.

Tasks name what they serve in `covers`. A task counts for coverage only when it is verified done: the queue
says `done` AND the ledger's hash-chained event log holds a `pass` for its contract. A requirement is covered
only when every task claiming it is verified done; its credit is (verified claimants / claimants), an exact
fraction, so partial work raises the score without ever marking the requirement covered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from core.ledger import Ledger, Rejected

TEXT_CAP = 300
REQ_ID_RE = re.compile(r"^\d{1,3}(\.\d{1,3})?$")
_SECTION = re.compile(r"^##\s+(\d{1,3})\.\s+(.*\S)\s*$")
_ITEM = re.compile(r"^(?:[-*+]\s+|\d{1,3}[.)]\s+)(.*\S)\s*$")
_SEP_CELL = re.compile(r"^:?-{3,}:?$")


def _clean(text: str) -> str:
    text = re.sub(r"[*`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:TEXT_CAP]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(_SEP_CELL.match(c) for c in cells if c) and any(cells)


def parse_requirements(text: str) -> dict[str, str]:
    """{requirement id: short text} in document order. Raises ValueError on a repeated section number."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    reqs: dict[str, str] = {}
    seen_sections: set[str] = set()
    sec, title, items = None, "", 0
    in_code = False

    def close() -> None:
        if sec is not None and items == 0:
            reqs[sec] = _clean(title)

    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("#"):
            m = _SECTION.match(line)
            if m or line.startswith("## ") or line.startswith("# "):  # a new top or second-level heading
                close()
                sec, items = None, 0
                if m:
                    if m.group(1) in seen_sections:
                        raise ValueError(f"section {m.group(1)} appears twice in the spec")
                    seen_sections.add(m.group(1))
                    sec, title = m.group(1), m.group(2)
            continue  # deeper headings (###) belong to their section and are not items
        if sec is None:
            continue
        if line.startswith("|"):
            if _is_separator(line):
                continue
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.startswith("|") and _is_separator(nxt):
                continue  # a table header row
            items += 1
            reqs[f"{sec}.{items}"] = _clean(" | ".join(c for c in _cells(line) if c))
            continue
        m = _ITEM.match(line)  # column 0 only: indented items are part of their parent
        if m:
            items += 1
            reqs[f"{sec}.{items}"] = _clean(m.group(1))
    close()
    return reqs


def claims(task: dict) -> list[str]:
    """The requirement ids a task says it serves: a list of strings, else nothing. Order kept, duplicates dropped."""
    cov = task.get("covers") if isinstance(task, dict) else None
    if not isinstance(cov, list) or not all(isinstance(x, str) for x in cov):
        return []
    out: list[str] = []
    for x in cov:
        if x not in out:
            out.append(x)
    return out


def ledger_completed(ledger: Ledger) -> set[str]:
    """Contract ids with a `pass` event in the ledger's event log. A broken hash chain raises Rejected."""
    if not ledger.events_path.exists():
        return set()
    ledger.repair_tail()
    if not ledger.verify_chain():
        raise Rejected("event log hash chain is broken")
    return {e.get("contract_id") for e in ledger.events() if e.get("action") == "pass"}


def verified_done(tasks: list[dict], ledger: Ledger) -> set[str]:
    """Build tasks the queue marks done AND the ledger has passed. Neither source is trusted alone."""
    passed = ledger_completed(ledger)
    return {t["id"] for t in tasks if isinstance(t, dict) and t.get("kind", "build") == "build"
            and t.get("status") == "done" and t.get("id") in passed}


@dataclass
class Coverage:
    requirements: dict[str, str]
    claimants: dict[str, list[str]] = field(default_factory=dict)
    done: dict[str, list[str]] = field(default_factory=dict)

    def status(self, rid: str) -> str:
        c, d = self.claimants.get(rid, []), self.done.get(rid, [])
        if not c:
            return "unclaimed"
        if len(d) == len(c):
            return "covered"
        return "partial" if d else "open"

    @property
    def score(self) -> Fraction:
        return sum((Fraction(len(self.done.get(r, [])), len(self.claimants[r])) for r in self.requirements
                    if self.claimants.get(r)), Fraction(0))

    @property
    def covered(self) -> int:
        return sum(1 for r in self.requirements if self.status(r) == "covered")

    @property
    def total(self) -> int:
        return len(self.requirements)

    def report(self, limit: int = 30000) -> str:
        lines = [f"Spec coverage: covered {self.covered} of {self.total} requirements, score {float(self.score):.2f}"]
        for rid, text in self.requirements.items():
            st = self.status(rid)
            c, d = self.claimants.get(rid, []), set(self.done.get(rid, []))
            label = f"{st} {len(d)}/{len(c)}" if st == "partial" else st
            who = f" (tasks: {', '.join(f'{x} ' + ('done' if x in d else 'open') for x in c)})" if c else ""
            lines.append(f"- {rid} [{label}] {text}{who}")
        out = "\n".join(lines)
        return out if len(out) <= limit else out[:limit - 4].rstrip() + "\n..."


def compute(requirements: dict[str, str], tasks: list[dict], verified: set[str]) -> Coverage:
    cov = Coverage(dict(requirements))
    for t in tasks:
        if not isinstance(t, dict) or t.get("kind", "build") != "build":
            continue
        for rid in claims(t):
            if rid in requirements:
                cov.claimants.setdefault(rid, []).append(t["id"])
                if t["id"] in verified:
                    cov.done.setdefault(rid, []).append(t["id"])
    return cov
