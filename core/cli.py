"""Command-line entry points for the trusted core.

  python -m core.cli verify                       check ledger schema + hash chain
  python -m core.cli apply PROPOSAL.json IDENTITY apply one proposal
  python -m core.cli protect FILE...  [--labels a,b]  fail if protected paths changed
  python -m core.cli run-acceptance CONTRACT_ID COMMIT  run a contract's test, record result as CI
  python -m core.cli status                       print contracts by status
  python -m core.cli approve-spec [IDENTITY]       you: freeze spec/spec.md as approved
  python -m core.cli resume                       after a crash: keep finished work, release dead claims
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from core.ledger import Ledger, Rejected
from core.protect import violations

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    led = Ledger(ROOT)

    if cmd == "verify":
        if not led.verify_chain():
            print("FAIL: event log hash chain broken")
            return 1
        for c in led.contracts().values():
            try:
                Ledger._check_contract(c)
            except Rejected as e:
                print(f"FAIL: {c.get('id')}: {e}")
                return 1
        drift = led.spec_drift()
        if drift:
            print(f"FAIL: {drift}")
            return 1
        print(f"OK: {len(led.events())} events, {len(led.contracts())} contracts, "
              f"spec {'approved' if led.approved_spec() else 'not approved yet'}")
        return 0

    if cmd == "apply":
        proposal = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        try:
            print(json.dumps(led.apply(proposal, args[1]), indent=2))
            return 0
        except Rejected as e:
            print(f"REJECTED: {e}")
            return 1

    if cmd == "protect":
        labels = []
        if "--labels" in args:
            i = args.index("--labels")
            labels = args[i + 1].split(",")
            args = args[:i] + args[i + 2:]
        bad = violations(args, labels)
        if bad:
            print("FAIL: protected paths changed without human approval:")
            for b in bad:
                print("  " + b)
            return 1
        print("OK: no protected paths changed")
        return 0

    if cmd == "run-acceptance":
        cid, commit = args
        c = led.contracts().get(cid)
        if not c:
            print(f"no contract {cid}")
            return 1
        r = subprocess.run(c["acceptance"], shell=True, cwd=ROOT)
        passed = r.returncode == 0
        led.apply({"proposal_id": f"ci-{uuid.uuid4().hex}", "action": "test_run",
                   "contract_id": cid,
                   "payload": {"run_id": f"run-{commit[:12]}-{uuid.uuid4().hex[:6]}",
                               "commit": commit, "passed": passed}}, "ci")
        print("PASS" if passed else "FAIL")
        return 0 if passed else 1

    if cmd == "approve-spec":
        identity = args[0] if args else "benjamin"
        h = led.spec_hash_on_disk()
        if not h:
            print("no spec/spec.md to approve")
            return 1
        try:
            led.apply({"proposal_id": f"approve-{h[:16]}-{uuid.uuid4().hex[:6]}", "action": "approve_spec",
                       "contract_id": "spec", "payload": {"spec_hash": h}}, identity)
        except Rejected as e:
            print(f"REJECTED: {e}")
            return 1
        print(f"OK: spec approved ({h[:12]})")
        return 0

    if cmd == "resume":
        from core.runner import resume
        print(json.dumps(resume(ROOT), indent=2))
        return 0

    if cmd == "status":
        by = {}
        for c in led.contracts().values():
            by.setdefault(c["status"], []).append(c["id"])
        print(json.dumps(by, indent=2))
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
