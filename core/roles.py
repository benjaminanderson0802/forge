"""Role instructions: each agent role's standing rules live in a protected file agents/<role>.md.

The conductor reads them from the main checkout only (D-027), never from the layer worktree or a task
worktree, so an agent that edits agents/ on its branch cannot change its own instructions. A file that is
missing or unusable falls back to the built-in default below; reading never raises for file problems.
"""
from __future__ import annotations

from pathlib import Path

ROLE_NAMES = ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper", "planner")
MAX_CHARS = 20000

DEFAULTS: dict[str, str] = {
    "test_writer": (
        "You are the TEST WRITER.\n"
        "You write only your task's test files. You never change any other file.\n"
        "The tests must fail until the feature exists.\n"
        "Answer with JSON matching the schema given in the prompt."),
    "builder": (
        "You are the BUILDER.\n"
        "You change only the task's files_in_scope. You never change tests, and you cannot mark yourself done: "
        "the judges and the reviewer decide.\n"
        "A blocked answer needs evidence: at least 2 different routes you actually tried, and the real error "
        "output.\n"
        "Answer with JSON matching the schema given in the prompt."),
    "reviewer": (
        "You are the REVIEWER (read-only).\n"
        "You change nothing. Reject shortcuts, bare-minimum work, drift from the task, and anything that "
        "weakens tests.\n"
        "Answer with JSON matching the schema given in the prompt."),
    "troubleshooter": (
        "You are the TROUBLESHOOTER.\n"
        "You write only in your own scratch worktree; your edits are discarded. Diagnose the cause and give "
        "concrete notes the next builder attempt can follow, or name a dead end and its alternative.\n"
        "Answer with JSON matching the schema given in the prompt."),
    "drift_keeper": (
        "You are the DRIFT KEEPER (read-only).\n"
        "You change nothing. Say replan only if the work is drifting from the design.\n"
        "Answer with JSON matching the schema given in the prompt."),
    "planner": (
        "You are the PLANNER.\n"
        "You write only the plan file named in the prompt, and no other file.\n"
        "Answer with JSON matching the schema given in the prompt."),
}


def role_text(repo: Path, role: str) -> str:
    """The standing instructions for `role`: <repo>/agents/<role>.md, or the built-in default."""
    if role not in ROLE_NAMES:
        raise ValueError(f"unknown role {role!r}")
    default = DEFAULTS[role]
    path = Path(repo) / "agents" / f"{role}.md"
    try:
        if not path.is_file():
            return default
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return default
    text = text.rstrip()
    if not text or len(text) > MAX_CHARS:
        return default
    return text
