"""Forge trusted core: the ledger.

No AI runs here. Agents never edit ledger files directly; they submit
proposals, and this module decides whether a proposal is valid. Every
accepted proposal becomes an event in an append-only, hash-chained log.

Rules enforced:
  * schema: every contract and proposal has the required fields and types
  * roles: each action may only be taken by specific roles (role comes from
    the committing identity via roles.json, never from the proposal itself)
  * transitions: contracts move only along allowed state edges
  * evidence: a contract can reach "done" only if CI recorded a passing
    test run for the exact commit the executor submitted
  * budgets: tokens over budget or attempts over max park the contract
  * idempotency: a proposal id is applied at most once
  * atomicity: every write is temp-file + rename, so a crash never leaves a
    half-written ledger
  * frozen spec: once you approve spec/spec.md, agents are refused while the
    file differs from what you approved; only you can re-approve
  * clean runs only: "pass" also needs the core's run report for this
    attempt, showing no protected-file changes and no out-of-scope edits
  * false claims: an executor that said "done" and then fails its audit is
    recorded, so the Learner gets clean data
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

STATUSES = {"open", "claimed", "submitted", "done", "failed", "parked"}

# action -> (allowed roles, allowed from-statuses, to-status or None)
ACTIONS = {
    "create":   ({"manager", "human"}, None, "open"),
    "claim":    ({"executor"}, {"open"}, "claimed"),
    "submit":   ({"executor"}, {"claimed"}, "submitted"),
    "pass":     ({"auditor"}, {"submitted"}, "done"),
    "fail":     ({"auditor"}, {"submitted"}, "failed"),
    "reopen":   ({"manager"}, {"failed"}, "open"),
    "park":     ({"manager", "human", "core"}, {"open", "claimed", "submitted", "failed"}, "parked"),
    "unpark":   ({"human"}, {"parked"}, "open"),
    "usage":    ({"executor", "core"}, {"claimed", "submitted"}, None),
    "test_run": ({"ci"}, None, None),
    # written by the runner (plain code), never by an agent
    "run_report": ({"core"}, {"claimed", "submitted"}, None),
    # a run that died: back to open, and the lost attempt still counts
    "release":  ({"core", "manager"}, {"claimed"}, "open"),
    # only you can freeze (or re-freeze) the spec
    "approve_spec": ({"human"}, None, None),
}

SPEC_FILE = "spec/spec.md"

CONTRACT_FIELDS = {
    "id": str, "title": str, "spec_ref": str, "acceptance": str,
    "files_in_scope": list, "status": str, "attempts": int,
    "max_attempts": int, "token_budget": int, "tokens_used": int,
    "commit": (str, type(None)),
}


class Rejected(Exception):
    """A proposal was refused. The ledger is unchanged."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Ledger:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.contracts_path = self.root / "ledger" / "contracts.json"
        self.events_path = self.root / "ledger" / "events.jsonl"
        self.runs_path = self.root / "ledger" / "test_runs.json"
        self.roles_path = self.root / "roles.json"
        self.kill_path = self.root / "ledger" / "KILL"
        self.spec_state_path = self.root / "ledger" / "spec.json"
        self.reports_path = self.root / "ledger" / "run_reports.json"
        self.spec_file = self.root / SPEC_FILE
        self._replaying = False  # replay re-checks rules, not today's files on disk

    # ---------- reading ----------
    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def contracts(self) -> dict:
        return self._read_json(self.contracts_path, {})

    def test_runs(self) -> dict:
        return self._read_json(self.runs_path, {})

    def events(self) -> list:
        if not self.events_path.exists():
            return []
        return [json.loads(l) for l in self.events_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def reports(self) -> dict:
        return self._read_json(self.reports_path, {})

    def approved_spec(self) -> str | None:
        return self._read_json(self.spec_state_path, {}).get("hash")

    def spec_hash_on_disk(self) -> str | None:
        if not self.spec_file.exists():
            return None
        text = self.spec_file.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(text).hexdigest()

    def spec_drift(self) -> str | None:
        """None if no spec is approved yet or the file matches; else what is wrong."""
        approved = self.approved_spec()
        if approved is None:
            return None
        if self.spec_hash_on_disk() != approved:
            return f"{SPEC_FILE} changed since you approved it; re-approve it or restore it"
        return None

    def false_claims(self, contract_id: str | None = None) -> int:
        return sum(1 for e in self.events() if e.get("note") == "false_claim"
                   and (contract_id is None or e["contract_id"] == contract_id))

    def role_of(self, identity: str) -> str:
        roles = self._read_json(self.roles_path, {})
        if identity not in roles:
            raise Rejected(f"unknown identity {identity!r}: not in roles.json")
        return roles[identity]

    def snapshot(self) -> str:
        """Hash of every ledger file, used to prove 'unchanged'."""
        h = hashlib.sha256()
        for p in (self.contracts_path, self.events_path, self.runs_path,
                  self.spec_state_path, self.reports_path):
            h.update(p.read_bytes() if p.exists() else b"")
        return h.hexdigest()

    # ---------- validation ----------
    @staticmethod
    def _check_contract(c: dict) -> None:
        for field, typ in CONTRACT_FIELDS.items():
            if field not in c:
                raise Rejected(f"contract missing field {field!r}")
            if not isinstance(c[field], typ):
                raise Rejected(f"contract field {field!r} has wrong type")
        extra = set(c) - set(CONTRACT_FIELDS)
        if extra:
            raise Rejected(f"contract has unknown fields {sorted(extra)}")
        if c["status"] not in STATUSES:
            raise Rejected(f"bad status {c['status']!r}")
        if c["max_attempts"] < 1 or c["token_budget"] < 1:
            raise Rejected("budgets must be positive")

    def verify_chain(self) -> bool:
        prev = "genesis"
        for e in self.events():
            body = {k: v for k, v in e.items() if k != "hash"}
            if body.get("prev") != prev or _hash(body) != e.get("hash"):
                return False
            prev = e["hash"]
        return True

    # ---------- applying ----------
    def apply(self, proposal: dict, identity: str) -> dict:
        """Validate and apply one proposal. Raises Rejected on any problem."""
        if self.kill_path.exists():
            raise Rejected("kill switch is on: no changes accepted")
        if not isinstance(proposal, dict):
            raise Rejected("proposal must be an object")
        for f in ("proposal_id", "action", "contract_id"):
            if not isinstance(proposal.get(f), str) or not proposal[f]:
                raise Rejected(f"proposal missing {f!r}")
        if not self.verify_chain():
            raise Rejected("event log hash chain is broken")

        pid, action, cid = proposal["proposal_id"], proposal["action"], proposal["contract_id"]
        payload = proposal.get("payload", {})
        if not isinstance(payload, dict):
            raise Rejected("payload must be an object")
        if any(e["proposal_id"] == pid for e in self.events()):
            return {"status": "duplicate", "proposal_id": pid}  # idempotent
        if action not in ACTIONS:
            raise Rejected(f"unknown action {action!r}")

        role = self.role_of(identity)
        roles, from_states, to_state = ACTIONS[action]
        if role not in roles:
            raise Rejected(f"role {role!r} may not {action}")

        drift = None if self._replaying else self.spec_drift()
        if drift and role not in {"human", "ci"}:
            raise Rejected(f"spec is frozen: {drift}")

        contracts = self.contracts()
        runs = self.test_runs()
        reports = self.reports()
        spec_state = self._read_json(self.spec_state_path, {})
        note = None

        if action == "approve_spec":
            h = payload.get("spec_hash")
            if not isinstance(h, str) or len(h) != 64 or (not self._replaying and h != self.spec_hash_on_disk()):
                raise Rejected("approve_spec needs the sha256 of the current spec/spec.md")
            spec_state = {"hash": h}
        elif action == "create":
            if cid in contracts:
                raise Rejected(f"contract {cid} already exists")
            preset = {"id", "status", "attempts", "tokens_used", "commit"} & set(payload)
            if preset:
                raise Rejected(f"create may not preset {sorted(preset)}")
            c = dict(payload, id=cid, status="open", attempts=0, tokens_used=0, commit=None)
            self._check_contract(c)
            contracts[cid] = c
        elif action == "test_run":
            if cid not in contracts:
                raise Rejected(f"no contract {cid}")
            run_id, commit, passed = payload.get("run_id"), payload.get("commit"), payload.get("passed")
            if not isinstance(run_id, str) or not isinstance(commit, str) or not isinstance(passed, bool):
                raise Rejected("test_run needs run_id, commit, passed")
            runs[run_id] = {"contract_id": cid, "commit": commit, "passed": passed}
        elif action == "run_report":
            if cid not in contracts:
                raise Rejected(f"no contract {cid}")
            c = contracts[cid]
            if c["status"] not in from_states:
                raise Rejected(f"cannot {action} a contract that is {c['status']}")
            lists = ("changed", "violations", "out_of_scope")
            if not isinstance(payload.get("run_id"), str) or any(
                    not isinstance(payload.get(k), list) or not all(isinstance(x, str) for x in payload[k]) for k in lists):
                raise Rejected("run_report needs run_id and lists changed, violations, out_of_scope")
            if payload.get("claim") is not None and not isinstance(payload["claim"], str):
                raise Rejected("run_report claim must be text or null")
            if payload.get("commit") is not None and not isinstance(payload["commit"], str):
                raise Rejected("run_report commit must be text or null")
            reports[cid] = {"attempt": c["attempts"], "run_id": payload["run_id"], "claim": payload.get("claim"),
                            "commit": payload.get("commit"), "changed": payload["changed"],
                            "violations": payload["violations"], "out_of_scope": payload["out_of_scope"]}
        else:
            if cid not in contracts:
                raise Rejected(f"no contract {cid}")
            c = dict(contracts[cid])
            if from_states and c["status"] not in from_states:
                raise Rejected(f"cannot {action} a contract that is {c['status']}")
            if action == "submit":
                if not isinstance(payload.get("commit"), str):
                    raise Rejected("submit needs the commit sha")
                c["commit"] = payload["commit"]
            if action == "pass":
                run = runs.get(payload.get("run_id", ""))
                if not run or run["contract_id"] != cid or run["commit"] != c["commit"] or not run["passed"]:
                    raise Rejected("pass needs a CI-recorded passing test run for the submitted commit")
                rep = reports.get(cid)
                if not rep or rep["attempt"] != c["attempts"]:
                    raise Rejected("pass needs the core's run report for this attempt")
                if rep["violations"] or rep["out_of_scope"]:
                    raise Rejected("pass refused: this attempt changed protected or out-of-scope files")
            if action == "fail":
                rep = reports.get(cid)
                if rep and rep["attempt"] == c["attempts"] and rep.get("claim") == "done":
                    note = "false_claim"
                c["attempts"] += 1
            if action == "release":
                c["attempts"] += 1
            if action == "usage":
                t = payload.get("tokens")
                if not isinstance(t, int) or t < 0:
                    raise Rejected("usage needs non-negative int tokens")
                c["tokens_used"] += t
            if to_state:
                c["status"] = to_state
            # budget guard: runs on every change, cannot be skipped
            if c["status"] in {"claimed", "submitted", "failed", "open"} and (
                c["tokens_used"] > c["token_budget"] or c["attempts"] >= c["max_attempts"]
            ):
                c["status"] = "parked"
            self._check_contract(c)
            contracts[cid] = c

        prev = self.events()[-1]["hash"] if self.events() else "genesis"
        body = {"proposal_id": pid, "action": action, "contract_id": cid,
                "identity": identity, "role": role, "payload": payload,
                "result_status": contracts[cid]["status"] if cid in contracts else None, "prev": prev}
        if note:
            body["note"] = note
        event = dict(body, hash=_hash(body))

        # write order: state files first, event last. If we crash before the
        # event lands, replay() rebuilds state from events and the proposal is
        # simply applied again (idempotency key not yet recorded).
        _atomic_write(self.contracts_path, json.dumps(contracts, indent=2, sort_keys=True))
        _atomic_write(self.runs_path, json.dumps(runs, indent=2, sort_keys=True))
        _atomic_write(self.reports_path, json.dumps(reports, indent=2, sort_keys=True))
        _atomic_write(self.spec_state_path, json.dumps(spec_state, indent=2, sort_keys=True))
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return {"status": "applied", "contract": contracts.get(cid), "note": note}

    def replay(self) -> None:
        """Rebuild contracts and test runs purely from the event log.

        Used after any crash: the event log is the source of truth, the state
        files are a cache of it.
        """
        events = self.events()
        _atomic_write(self.contracts_path, "{}")
        _atomic_write(self.runs_path, "{}")
        _atomic_write(self.reports_path, "{}")
        _atomic_write(self.spec_state_path, "{}")
        self.events_path.write_text("", encoding="utf-8")
        self._replaying = True
        try:
            for e in events:
                self.apply({"proposal_id": e["proposal_id"], "action": e["action"],
                            "contract_id": e["contract_id"], "payload": e["payload"]}, e["identity"])
        finally:
            self._replaying = False
