"""The runner: plain code that takes an agent through one contract attempt.

  run_contract  claim -> run the executor -> undo protected changes ->
                report what changed -> submit if the executor gave a commit
  resume        after a crash: keep finished work, release dead claims

No AI here. The executor is whatever launches the agent; the runner never
trusts what it says. It only records the claim, so a false "done" can be
caught at audit.
"""
from __future__ import annotations

import fnmatch
import hashlib
import uuid
from pathlib import Path
from typing import Callable

from core.ledger import Ledger
from core.protect import normalize, violations

EXECUTOR = "forge-executor"
CORE = "forge-core"
SKIP_DIRS = {".git", "__pycache__", "ledger", ".venv", "node_modules"}
LEDGER_FILES = ("contracts.json", "events.jsonl", "test_runs.json", "run_reports.json", "spec.json")


def snapshot(root: Path) -> dict[str, str]:
    """sha256 of every project file, by forward-slash relative path."""
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS or "__pycache__" in rel.parts:
            continue
        out[normalize(str(rel))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _ledger_bytes(root: Path) -> dict[str, bytes | None]:
    d = root / "ledger"
    return {n: (d / n).read_bytes() if (d / n).exists() else None for n in LEDGER_FILES}


def _restore(root: Path, rels: list[str], backup: dict[str, bytes]) -> None:
    for rel in rels:
        path = root / rel
        if rel in backup:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(backup[rel])
        elif path.exists():
            path.unlink()


def run_contract(root: str | Path, cid: str, executor: Callable[[dict], object],
                 run_id: str | None = None) -> dict:
    """One attempt at one contract. Returns the run report.

    executor(ctx) may return "done", or {"claim": "done", "commit": "<sha>"}.
    """
    root = Path(root)
    run_id = run_id or uuid.uuid4().hex
    led = Ledger(root)
    led.apply({"proposal_id": f"claim-{run_id}", "action": "claim", "contract_id": cid}, EXECUTOR)
    contract = led.contracts()[cid]

    before = snapshot(root)
    backup = {rel: (root / rel).read_bytes() for rel in violations(list(before))}
    ledger_before = _ledger_bytes(root)

    claim, commit, error = None, None, None
    try:
        out = executor({"root": root, "contract": contract, "run_id": run_id})
        if isinstance(out, dict):
            claim, commit = out.get("claim"), out.get("commit")
        elif out is not None:
            claim = str(out)
    except Exception as e:  # noqa: BLE001 - an agent crash is a failed attempt, not a runner crash
        error = repr(e)

    # Anything the agent wrote into the ledger directly is thrown away.
    ledger_touched = [f"ledger/{n}" for n, b in _ledger_bytes(root).items() if b != ledger_before[n]]
    for n, b in ledger_before.items():
        path = root / "ledger" / n
        if b is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(b)

    after = snapshot(root)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    bad = violations(changed)  # no label: agents can never approve themselves
    _restore(root, bad, backup)
    scope = contract["files_in_scope"]
    out_of_scope = [f for f in changed if f not in bad and not any(fnmatch.fnmatch(f, pat) for pat in scope)]

    report = {"run_id": run_id, "claim": claim, "commit": commit, "changed": changed,
              "violations": sorted(bad + ledger_touched), "out_of_scope": out_of_scope}
    led = Ledger(root)
    led.apply({"proposal_id": f"report-{run_id}", "action": "run_report", "contract_id": cid, "payload": report}, CORE)
    if commit and not error:
        led.apply({"proposal_id": f"submit-{run_id}", "action": "submit", "contract_id": cid,
                   "payload": {"commit": commit}}, EXECUTOR)
    return dict(report, error=error)


def resume(root: str | Path) -> dict:
    """After a crash. Assumes one runner at a time; call it before starting new work.

    - claimed, with a clean report and a commit: the work finished, so submit it
    - claimed, anything else: the run died; release it (the attempt counts)
    - submitted with a passing CI run: left for the auditor, never redone
    """
    root = Path(root)
    led = Ledger(root)
    out = {"submitted": [], "released": [], "ready_for_audit": []}
    reports, runs = led.reports(), led.test_runs()
    for cid, c in sorted(led.contracts().items()):
        tag = f"{cid}-{c['attempts']}"
        if c["status"] == "claimed":
            rep = reports.get(cid)
            clean = rep and rep["attempt"] == c["attempts"] and not rep["violations"] and not rep["out_of_scope"]
            if clean and rep.get("commit"):
                led.apply({"proposal_id": f"resume-submit-{tag}", "action": "submit", "contract_id": cid,
                           "payload": {"commit": rep["commit"]}}, EXECUTOR)
                out["submitted"].append(cid)
            else:
                led.apply({"proposal_id": f"resume-release-{tag}", "action": "release", "contract_id": cid}, CORE)
                out["released"].append(cid)
        elif c["status"] == "submitted" and any(
                r["contract_id"] == cid and r["commit"] == c["commit"] and r["passed"] for r in runs.values()):
            out["ready_for_audit"].append(cid)
    return out
