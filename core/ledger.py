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
  * crash recovery: the event log is the truth and the other files are a
    cache of it. apply() writes ledger/head.json (the hash of the event it
    is about to append) before any cache file and appends the event last,
    so a crash at any point leaves a head marker that disagrees with the
    log; the next apply() (or reconcile()) then rebuilds the cache from the
    log before deciding anything. A torn last log line is dropped; a bad
    line anywhere else freezes the ledger.
  * completion is event-backed: completion(cid) reads the "pass" event from
    the log, never the contract cache
  * merge evidence: a "pass" may carry task_commit, final_sha and merges,
    each merge backed by a CI run recorded for its exact sha
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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


_SHA40 = re.compile(r"[0-9a-f]{40}")
MERGE_KINDS = {"local", "divergence"}


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
        self.head_path = self.root / "ledger" / "head.json"
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
        if not self._replaying:
            # self-heal before any decision: a cache that got ahead of the log
            # (crash before the event landed) is rebuilt from the log first
            self.repair_tail()
            if self._read_head() != self._last_hash():
                self.rebuild()
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
                self._check_merge_evidence(cid, c, payload, runs)
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

        # write order: head marker first, then the state files, event last.
        # The marker names an event that is not in the log yet, so a crash
        # anywhere before the append leaves marker != log head, and the next
        # apply() rebuilds the cache from the log before deciding anything;
        # the proposal is then simply applied again (its id never landed).
        self._write_cache(contracts, runs, reports, spec_state, event["hash"])
        self._append_event(event)
        return {"status": "applied", "contract": contracts.get(cid), "note": note}

    def _write_cache(self, contracts: dict, runs: dict, reports: dict, spec_state: dict, head: str) -> None:
        _atomic_write(self.head_path, json.dumps({"hash": head}, indent=2, sort_keys=True))
        _atomic_write(self.contracts_path, json.dumps(contracts, indent=2, sort_keys=True))
        _atomic_write(self.runs_path, json.dumps(runs, indent=2, sort_keys=True))
        _atomic_write(self.reports_path, json.dumps(reports, indent=2, sort_keys=True))
        _atomic_write(self.spec_state_path, json.dumps(spec_state, indent=2, sort_keys=True))

    def _append_event(self, event: dict) -> None:
        """Append one event line to the log and fsync it. The only writer of events.jsonl."""
        with self.events_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _check_merge_evidence(cid: str, c: dict, payload: dict, runs: dict) -> None:
        """Optional merge evidence on a pass. Absent keys are not checked; a key
        that is present must hold a valid value (null is invalid, not absent)."""
        def bad(why: str):
            raise Rejected(f"pass merge evidence incomplete: {why}")

        def sha40(v) -> bool:
            return isinstance(v, str) and _SHA40.fullmatch(v) is not None

        def strs(v) -> bool:
            return isinstance(v, list) and all(isinstance(x, str) for x in v)

        if "task_commit" in payload:
            task_commit = payload["task_commit"]
            if not isinstance(task_commit, str) or task_commit != c["commit"]:
                bad("task_commit is not the submitted commit")
        has_final = "final_sha" in payload
        if has_final and not sha40(payload["final_sha"]):
            bad("final_sha must be a 40-character lowercase hex sha")
        if "merges" not in payload:
            return
        merges = payload["merges"]
        if not isinstance(merges, list):
            bad("merges must be a list")
        if merges and not has_final:
            bad("merges need final_sha")
        for i, m in enumerate(merges):
            if not isinstance(m, dict):
                bad(f"merge {i} must be an object")
            if not sha40(m.get("sha")):
                bad(f"merge {i} sha must be a 40-character lowercase hex sha")
            if not strs(m.get("parents")):
                bad(f"merge {i} parents must be a list of text")
            if m.get("kind") not in MERGE_KINDS:
                bad(f"merge {i} kind must be one of {sorted(MERGE_KINDS)}")
            if not isinstance(m.get("run_id"), str):
                bad(f"merge {i} needs run_id")
            if m.get("verdict") != "pass":
                bad(f"merge {i} verdict is not pass")
            if not strs(m.get("reasons")):
                bad(f"merge {i} reasons must be a list of text")
            run = runs.get(m["run_id"])
            if not run or run["contract_id"] != cid or run["commit"] != m["sha"] or run["passed"] is not True:
                bad(f"merge {i} needs a CI-recorded passing test run for {m['sha']}")

    # ---------- recovery ----------
    def _read_head(self):
        """The hash in head.json, or None if it is missing or unreadable."""
        try:
            data = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data.get("hash") if isinstance(data, dict) else None

    def _last_hash(self) -> str:
        evs = self.events()
        return evs[-1]["hash"] if evs else "genesis"

    def repair_tail(self) -> bool:
        """Drop a torn last line of events.jsonl (an append that never completed).

        Returns True if something was dropped. A bad line anywhere before the
        last raises Rejected: nothing is dropped silently.
        """
        if not self.events_path.exists():
            return False
        data = self.events_path.read_bytes()
        if not data:
            return False
        lines = data.split(b"\n")  # the piece after the final newline is b"" when terminated
        def ok(raw: bytes) -> bool:
            if not raw.strip():
                return True
            try:
                json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return False
            return True
        complete, tail = lines[:-1], lines[-1]
        last_bad = False
        if tail:
            last_bad = True  # not newline-terminated: the append never completed
        else:
            # the last non-blank complete line must parse
            idx = max((i for i, l in enumerate(complete) if l.strip()), default=None)
            if idx is not None and not ok(complete[idx]):
                last_bad = True
                complete = complete[:idx]  # only blank lines followed it
        if not all(ok(l) for l in complete):
            raise Rejected("event log corrupt")
        if not last_bad:
            return False
        keep = sum(len(l) + 1 for l in complete)
        with self.events_path.open("r+b") as f:
            f.truncate(keep)
            f.flush()
            os.fsync(f.fileno())
        return True

    def derive(self) -> dict:
        """Replay every event into a fresh ledger in a temporary folder.

        Returns what the cache files should hold. Never writes to this ledger.
        """
        if not self.verify_chain():
            raise Rejected("event log hash chain is broken")
        events = self.events()
        tmp = Path(tempfile.mkdtemp(prefix="forge-derive-"))
        try:
            if self.roles_path.exists():
                shutil.copyfile(self.roles_path, tmp / "roles.json")
            led = Ledger(tmp)
            led._replaying = True
            for e in events:
                led.apply({"proposal_id": e["proposal_id"], "action": e["action"],
                           "contract_id": e["contract_id"], "payload": e["payload"]}, e["identity"])
                if led._last_hash() != e["hash"]:
                    raise Rejected(f"event log does not replay: event {e['proposal_id']!r} came out different")
            return {"contracts": led.contracts(), "test_runs": led.test_runs(), "reports": led.reports(),
                    "spec_state": led._read_json(led.spec_state_path, {}), "head": led._last_hash()}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def rebuild(self) -> None:
        """Rewrite every cache file (and head.json) from the event log.

        Never truncates or rewrites events.jsonl beyond repair_tail, so a crash
        at any point leaves the events intact. The head marker is removed
        first and written last, so a crash inside rebuild is noticed later.
        """
        self.repair_tail()
        d = self.derive()
        self.head_path.unlink(missing_ok=True)
        _atomic_write(self.contracts_path, json.dumps(d["contracts"], indent=2, sort_keys=True))
        _atomic_write(self.runs_path, json.dumps(d["test_runs"], indent=2, sort_keys=True))
        _atomic_write(self.reports_path, json.dumps(d["reports"], indent=2, sort_keys=True))
        _atomic_write(self.spec_state_path, json.dumps(d["spec_state"], indent=2, sort_keys=True))
        _atomic_write(self.head_path, json.dumps({"hash": d["head"]}, indent=2, sort_keys=True))

    def reconcile(self) -> bool:
        """Check the cache against the log; rebuild it if anything differs. True if rebuilt."""
        self.repair_tail()
        if not self.verify_chain():
            raise Rejected("event log hash chain is broken")
        d = self.derive()
        stale = self._read_head() != d["head"]
        for path, key in ((self.contracts_path, "contracts"), (self.runs_path, "test_runs"),
                          (self.reports_path, "reports"), (self.spec_state_path, "spec_state")):
            try:
                on_disk = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                on_disk = None
            if on_disk != d[key]:
                stale = True
        if stale:
            self.rebuild()
        return stale

    def completion(self, cid: str) -> dict | None:
        """The "pass" event for contract cid, read from the event log, or None.

        This is the only way to decide that a contract is complete; the
        contract cache can be ahead of the log after a crash.
        """
        self.repair_tail()
        if not self.verify_chain():
            raise Rejected("event log hash chain is broken")
        for e in reversed(self.events()):
            if e.get("action") == "pass" and e.get("contract_id") == cid:
                return e
        return None

    def replay(self) -> None:
        """Rebuild the cache files purely from the event log (see rebuild())."""
        self.rebuild()
