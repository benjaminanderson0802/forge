"""Protected paths: the parts of Forge that Forge itself may never change.

CI runs this on every pull request. If an agent's change touches any
protected path, the check fails and branch protection blocks the merge.
Only a pull request carrying the 'human-approved' label (which only the
repo owner can apply) passes.
"""
from __future__ import annotations

import fnmatch

PROTECTED = [
    "core/*", "core/**",
    "drills/*", "drills/**",
    "tests/acceptance/*", "tests/acceptance/**",
    "charter/*", "charter/**",
    "spec/*", "spec/**",
    ".github/*", ".github/**",
    "roles.json", "CODEOWNERS",
    "docs/PURPOSE.md", "docs/DECISIONS.md",
]


def normalize(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def violations(changed_files: list[str], labels: list[str] | None = None) -> list[str]:
    if labels and "human-approved" in labels:
        return []
    bad = []
    for f in changed_files:
        f = normalize(f)
        if any(fnmatch.fnmatch(f, pat) for pat in PROTECTED):
            bad.append(f)
    return bad
