"""The Manager (layer-1 design §2): plans from the ledger and the spec only. Plain-code side: its answer schema,
its standing rules, the prompt built from exactly its allowed inputs, and complete validation of its proposals.

The Manager is Claude, read-only (`permission_mode="plan"`): it writes nothing and returns proposed tasks. What
it sees is limited by construction: `build_prompt` takes the spec text, the coverage map, the ledger's contract
rows and the re-plan reasons, and nothing else (no notes, no transcripts, no questions, no emails).

No imports from core.bootstrap (which imports this module).
"""
from __future__ import annotations

import re

from core import readiness
from core.coverage import Coverage, ledger_completed
from core.ledger import Ledger

REQUIRED = ("id", "title", "section", "covers", "files_in_scope", "test_files", "test_cmd")
OPTIONAL = ("needs",)
MAX_TASKS = 12
TITLE_CAP, SECTION_CAP = 200, 20000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TEST_FILE_RE = re.compile(r"^tests/core/test[A-Za-z0-9_]*\.py$")

_STR, _STRS = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
S_MANAGER = {"type": "object", "properties": {
    "tasks": {"type": "array", "items": {"type": "object", "properties": {
        "id": _STR, "title": _STR, "section": _STR, "covers": _STRS, "files_in_scope": _STRS, "test_files": _STRS,
        "test_cmd": _STR, "needs": _STRS}, "required": list(REQUIRED)}},
    "reasons": _STRS}, "required": ["tasks"]}

MANAGER_TEXT = (
    "You are the MANAGER (read-only).\n"
    "Standing rules:\n"
    "- You may write nothing: change no file and make no commit. You only answer with proposed tasks.\n"
    "- Plan only from the SPEC, the COVERAGE map and the LEDGER below. Nothing else is evidence of what is done.\n"
    "- Propose the smallest set of new build tasks that makes spec coverage rise, starting with requirements "
    "that are unclaimed, open or partial. Do not repeat work the ledger shows as completed.\n"
    "- Every task: a new unique id (letters, digits, - or _), a title, a section (the full instructions the test "
    "writer and builder will see), covers (the requirement ids it serves), files_in_scope (only the files it may "
    "change; never tests), test_files (under tests/core/, named test_*.py) and test_cmd exactly "
    "`python -m unittest <its test files>`. Optional needs: capability names beyond git and its own AI.\n"
    "- Each task must be small enough for one agent session.\n"
    "- Answer with JSON matching the schema given in the prompt.")


def _norm(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _is_abs(p: str) -> bool:
    return bool(re.match(r"^([A-Za-z]:|/|\\)", p)) or p.startswith("//")


def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _task_problems(t: dict, requirements: dict, taken: set[str]) -> list[str]:
    tid = t.get("id")
    who = f"task {tid!r}" if isinstance(tid, str) else "a task"
    out = []
    missing = [k for k in REQUIRED if k not in t]
    if missing:
        return [f"{who}: missing {', '.join(missing)}"]
    unknown = sorted(set(t) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        out.append(f"{who}: unknown fields {', '.join(unknown)}")
    if not isinstance(tid, str) or not ID_RE.match(tid):
        out.append(f"{who}: bad id")
    elif tid in taken:
        out.append(f"{who}: id already used")
    title, section = t.get("title"), t.get("section")
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_CAP:
        out.append(f"{who}: title must be non-empty text of at most {TITLE_CAP} characters")
    if not isinstance(section, str) or not section.strip() or len(section) > SECTION_CAP:
        out.append(f"{who}: section must be non-empty text of at most {SECTION_CAP} characters")
    cov = t.get("covers")
    if not _strs(cov) or not cov:
        out.append(f"{who}: covers must be a non-empty list of requirement ids")
    else:
        bad = [x for x in cov if x not in requirements]
        if bad:
            out.append(f"{who}: covers unknown requirements {', '.join(bad)}")
        if len(set(cov)) != len(cov):
            out.append(f"{who}: covers lists a requirement twice")
    tf = t.get("test_files")
    tests: list[str] = []
    if not _strs(tf) or not tf:
        out.append(f"{who}: test_files must be a non-empty list of paths")
    else:
        tests = [_norm(x) for x in tf]
        for x in tests:
            if ".." in x or not TEST_FILE_RE.match(x):
                out.append(f"{who}: test file {x!r} must be tests/core/test_*.py")
        if len(set(tests)) != len(tests):
            out.append(f"{who}: a test file is listed twice")
    scope = t.get("files_in_scope")
    if not _strs(scope) or not scope:
        out.append(f"{who}: files_in_scope must be a non-empty list of paths")
    else:
        for x in scope:
            n = _norm(x)
            if not n.strip() or ".." in n or _is_abs(x) or _is_abs(n):
                out.append(f"{who}: bad scope entry {x!r}")
            elif n.startswith("tests/") or n in tests:
                out.append(f"{who}: scope entry {x!r} is a test path (builders never change tests)")
    cmd = t.get("test_cmd")
    if not isinstance(cmd, str) or not cmd.startswith("python -m unittest "):
        out.append(f"{who}: test_cmd must be `python -m unittest <its test files>`")
    elif tests:
        paths = [_norm(x) for x in cmd[len("python -m unittest "):].split(" ")]
        if any(not p for p in paths) or sorted(paths) != sorted(tests):
            out.append(f"{who}: test_cmd must run exactly its test files")
    if "needs" in t:
        nd = t["needs"]
        if not _strs(nd) or len(nd) > 10 or not all(readiness.NAME_RE.match(x) for x in nd):
            out.append(f"{who}: bad needs")
    return out


def validate_proposal(data, requirements: dict, existing_ids: set[str]) -> tuple[list[dict], list[str]]:
    """(clean tasks, problems). Tasks are usable only when problems is empty. Checks the whole schema with types,
    every path rule, known requirement ids, and ids new to both the queue and the ledger (`existing_ids`)."""
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        return [], ["proposal must be an object with a non-empty tasks list"]
    tasks = data["tasks"]
    if len(tasks) > MAX_TASKS:
        return [], [f"proposal has {len(tasks)} tasks; at most {MAX_TASKS}"]
    problems: list[str] = []
    taken = set(existing_ids)
    seen_tests: set[str] = set()
    clean = []
    for t in tasks:
        if not isinstance(t, dict):
            problems.append("every task must be an object")
            continue
        p = _task_problems(t, requirements, taken)
        if not p:
            tests = {_norm(x) for x in t["test_files"]}
            if tests & seen_tests:
                p.append(f"task {t['id']!r}: shares a test file with another new task")
            seen_tests |= tests
        problems += p
        if isinstance(t.get("id"), str):
            taken.add(t["id"])
        if not p:
            c = {k: t[k] for k in REQUIRED}
            c["files_in_scope"] = [_norm(x) for x in t["files_in_scope"]]
            c["test_files"] = [_norm(x) for x in t["test_files"]]
            c["covers"] = list(t["covers"])
            if "needs" in t:
                c["needs"] = list(t["needs"])
            clean.append(c)
    return (clean, []) if not problems else ([], problems[:30])


def ledger_rows(ledger: Ledger) -> list[dict]:
    """The ledger's contracts, as the Manager sees them. Completion is read from the event log."""
    done = ledger_completed(ledger)
    rows = []
    for cid, c in sorted(ledger.contracts().items()):
        rows.append({"id": cid, "title": str(c.get("title", "")), "spec_ref": str(c.get("spec_ref", "")),
                     "status": str(c.get("status", "")), "attempts": c.get("attempts", 0), "completed": cid in done})
    return rows


def build_prompt(spec_text: str, cov: Coverage, rows: list[dict], reasons: list[str]) -> str:
    """The whole Manager prompt. Its only inputs are its arguments: spec, coverage map, ledger rows, reasons."""
    ledger = "\n".join(f"- {r['id']} [{r['status']}{', completed' if r['completed'] else ''}] "
                       f"{str(r['title'])[:TITLE_CAP]} (spec_ref {str(r['spec_ref'])[:80]}, attempts {r['attempts']})"
                       for r in rows[:400]) or "(no contracts yet)"
    why = "\n".join(f"- {str(x)[:2000]}" for x in list(reasons)[:20]) or "- (none given)"
    return (MANAGER_TEXT +
            "\n\nWHY A RE-PLAN IS NEEDED:\n" + why +
            "\n\nCOVERAGE (requirement id [status] text (tasks claiming it)):\n" + cov.report(30000) +
            "\n\nLEDGER (every contract; completed means the ledger holds its pass):\n" + ledger[:40000] +
            "\n\nSPEC:\n" + str(spec_text)[:60000] +
            "\n\nAnswer with JSON: {\"tasks\": [{\"id\", \"title\", \"section\", \"covers\", \"files_in_scope\", "
            "\"test_files\", \"test_cmd\", \"needs\"?}], \"reasons\": [...]}. test_files under tests/core/; "
            "test_cmd exactly `python -m unittest <its test files>`.")
