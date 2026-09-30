"""Sabotage drills for the trusted core (drills 1-10 from the Forge plan).

Each drill plants a failure in a fresh, throwaway ledger and checks that
the core catches it. Run:  python drills/run_drills.py
Exit code 0 only if every drill passes. Results also go to drills/results.json.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.ledger import Ledger, Rejected  # noqa: E402
from core import ledger as ledger_mod  # noqa: E402
from core.protect import normalize, violations  # noqa: E402
from core import runner  # noqa: E402

ROLES = {"mgr": "manager", "exe": "executor", "aud": "auditor", "ci": "ci", "me": "human",
         "forge-executor": "executor", "forge-core": "core"}


def fresh() -> tuple[Ledger, Path]:
    d = Path(tempfile.mkdtemp(prefix="forge-drill-"))
    (d / "roles.json").write_text(json.dumps(ROLES))
    return Ledger(d), d


def contract(led: Ledger, cid="C1", acceptance="python -c \"import sys; sys.exit(1)\"", budget=1000, max_attempts=3):
    led.apply({"proposal_id": f"create-{cid}", "action": "create", "contract_id": cid, "payload": {
        "title": "demo", "spec_ref": "spec.md#1", "acceptance": acceptance,
        "files_in_scope": ["src/demo.py"], "max_attempts": max_attempts, "token_budget": budget}}, "mgr")


def expect_reject(led: Ledger, proposal: dict, identity: str, label: str) -> None:
    before = led.snapshot()
    try:
        led.apply(proposal, identity)
    except Rejected:
        assert led.snapshot() == before, f"{label}: ledger changed despite rejection"
        return
    raise AssertionError(f"{label}: was accepted but should have been rejected")


# ---------------------------------------------------------------- drills
def drill_1():
    """Executor claims done but the acceptance test fails -> contract cannot reach done."""
    led, _ = fresh()
    contract(led)
    led.apply({"proposal_id": "p1", "action": "claim", "contract_id": "C1"}, "exe")
    led.apply({"proposal_id": "p2", "action": "submit", "contract_id": "C1", "payload": {"commit": "abc123"}}, "exe")
    # executor tries to mark itself done
    expect_reject(led, {"proposal_id": "p3", "action": "pass", "contract_id": "C1", "payload": {}}, "exe", "executor self-pass")
    # auditor tries to pass with no evidence
    expect_reject(led, {"proposal_id": "p4", "action": "pass", "contract_id": "C1", "payload": {}}, "aud", "pass without run")
    # CI records a FAILING run; auditor tries to pass anyway
    led.apply({"proposal_id": "p5", "action": "test_run", "contract_id": "C1",
               "payload": {"run_id": "r1", "commit": "abc123", "passed": False}}, "ci")
    expect_reject(led, {"proposal_id": "p6", "action": "pass", "contract_id": "C1", "payload": {"run_id": "r1"}}, "aud", "pass on failing run")
    # a passing run for a DIFFERENT commit doesn't count either
    led.apply({"proposal_id": "p7", "action": "test_run", "contract_id": "C1",
               "payload": {"run_id": "r2", "commit": "zzz999", "passed": True}}, "ci")
    expect_reject(led, {"proposal_id": "p8", "action": "pass", "contract_id": "C1", "payload": {"run_id": "r2"}}, "aud", "pass on other commit")
    # an executor can't forge a CI run
    expect_reject(led, {"proposal_id": "p9", "action": "test_run", "contract_id": "C1",
                        "payload": {"run_id": "r3", "commit": "abc123", "passed": True}}, "exe", "forged CI run")
    assert led.contracts()["C1"]["status"] == "submitted"
    # control: the genuine path still works (clean run report from the core + passing CI run)
    led.apply({"proposal_id": "p10", "action": "test_run", "contract_id": "C1",
               "payload": {"run_id": "r4", "commit": "abc123", "passed": True}}, "ci")
    # green CI alone is not enough: the core must have watched the attempt
    expect_reject(led, {"proposal_id": "p10b", "action": "pass", "contract_id": "C1", "payload": {"run_id": "r4"}}, "aud", "pass without run report")
    led.apply({"proposal_id": "p9b", "action": "run_report", "contract_id": "C1", "payload": {
        "run_id": "x", "claim": "done", "commit": "abc123", "changed": ["src/demo.py"], "violations": [], "out_of_scope": []}}, "forge-core")
    led.apply({"proposal_id": "p11", "action": "pass", "contract_id": "C1", "payload": {"run_id": "r4"}}, "aud")
    assert led.contracts()["C1"]["status"] == "done"
    return "self-pass, no-evidence, failing-run, wrong-commit, forged-run and unwatched attempts refused; genuine pass accepted"


def drill_2():
    """Executor edits the acceptance test (or any protected file) -> rejected unless human-approved."""
    for f in ["tests/acceptance/test_c1.py", "core/ledger.py", "drills/run_drills.py", "spec/spec.md",
              ".github/workflows/core-checks.yml", "roles.json", "charter/authority.md", "./core/protect.py",
              "docs/PURPOSE.md", "docs/DECISIONS.md", "docs\\DECISIONS.md"]:
        assert violations(["src/demo.py", f]) == [normalize(f)], f"{f} not protected"
    assert violations(["src/demo.py", "skills/clip.md", "docs/STATUS.md"]) == [], "normal files wrongly blocked"
    assert violations(["core/ledger.py"], ["human-approved"]) == [], "human approval not honored"
    return "11 protected paths blocked, normal files allowed, human-approved label honored"


def drill_3():
    """Malformed or invented ledger updates -> rejected, ledger byte-identical."""
    led, d = fresh()
    contract(led)
    cases = [
        ({"action": "claim", "contract_id": "C1"}, "exe", "missing proposal_id"),
        ({"proposal_id": "x1", "action": "claim", "contract_id": "NOPE"}, "exe", "invented contract"),
        ({"proposal_id": "x2", "action": "teleport", "contract_id": "C1"}, "exe", "invented action"),
        ({"proposal_id": "x3", "action": "claim", "contract_id": "C1"}, "stranger", "unknown identity"),
        ({"proposal_id": "x4", "action": "create", "contract_id": "C2", "payload": {"title": "x"}}, "mgr", "incomplete contract"),
        ({"proposal_id": "x5", "action": "create", "contract_id": "C3", "payload": {
            "title": "x", "spec_ref": "s", "acceptance": "true", "files_in_scope": [], "max_attempts": 3,
            "token_budget": 10, "status": "done"}}, "mgr", "create straight into done"),
        ({"proposal_id": "x6", "action": "create", "contract_id": "C4", "payload": {
            "title": "x", "spec_ref": "s", "acceptance": "true", "files_in_scope": [], "max_attempts": 3,
            "token_budget": 10, "sneaky": True}}, "mgr", "unknown field"),
        ({"proposal_id": "x7", "action": "create", "contract_id": "C5", "payload": {}}, "exe", "executor creating contracts"),
        ({"proposal_id": "x8", "action": "submit", "contract_id": "C1", "payload": {"commit": "a"}}, "exe", "skip claim"),
        ({"proposal_id": "x9", "action": "usage", "contract_id": "C1", "payload": {"tokens": -500}}, "exe", "negative usage"),
        ("not a dict", "exe", "non-object proposal"),
    ]
    for prop, ident, label in cases:
        expect_reject(led, prop, ident, label)
    # tampering with history breaks the hash chain and freezes the ledger
    lines = led.events_path.read_text().splitlines()
    ev = json.loads(lines[0]); ev["payload"]["token_budget"] = 999999
    led.events_path.write_text(json.dumps(ev, sort_keys=True) + "\n")
    assert not led.verify_chain(), "tampered log not detected"
    try:
        led.apply({"proposal_id": "y1", "action": "claim", "contract_id": "C1"}, "exe")
        raise AssertionError("apply accepted on a tampered log")
    except Rejected:
        pass
    return f"{len(cases)} malformed/invented proposals refused with ledger unchanged; tampered history detected and frozen"


def drill_4():
    """Contract exceeds its token budget or attempt limit -> parked automatically."""
    led, _ = fresh()
    contract(led, "T", budget=1000)
    led.apply({"proposal_id": "a1", "action": "claim", "contract_id": "T"}, "exe")
    led.apply({"proposal_id": "a2", "action": "usage", "contract_id": "T", "payload": {"tokens": 600}}, "exe")
    assert led.contracts()["T"]["status"] == "claimed"
    led.apply({"proposal_id": "a3", "action": "usage", "contract_id": "T", "payload": {"tokens": 500}}, "exe")
    assert led.contracts()["T"]["status"] == "parked", "over-budget contract not parked"
    expect_reject(led, {"proposal_id": "a4", "action": "submit", "contract_id": "T", "payload": {"commit": "c"}}, "exe", "work on parked")
    expect_reject(led, {"proposal_id": "a5", "action": "unpark", "contract_id": "T"}, "mgr", "manager unparking")

    contract(led, "A", max_attempts=3)
    n = 0
    for i in range(3):
        led.apply({"proposal_id": f"b{i}c", "action": "claim", "contract_id": "A"}, "exe")
        led.apply({"proposal_id": f"b{i}s", "action": "submit", "contract_id": "A", "payload": {"commit": f"k{i}"}}, "exe")
        led.apply({"proposal_id": f"b{i}f", "action": "fail", "contract_id": "A"}, "aud")
        n += 1
        if led.contracts()["A"]["status"] == "parked":
            break
        led.apply({"proposal_id": f"b{i}r", "action": "reopen", "contract_id": "A"}, "mgr")
    assert led.contracts()["A"]["status"] == "parked" and n == 3, "3 failures did not park"
    # kill switch stops everything
    led.kill_path.write_text("stop")
    expect_reject(led, {"proposal_id": "k1", "action": "unpark", "contract_id": "T"}, "me", "change while killed")
    led.kill_path.unlink()
    led.apply({"proposal_id": "k2", "action": "unpark", "contract_id": "T"}, "me")
    return "over-budget parked at 1,100/1,000 tokens; 3 failed audits parked; only you can unpark; kill switch blocks all changes"


def drill_5():
    """Manager killed mid-write, then restarted -> resumes with nothing lost or repeated."""
    led, d = fresh()
    contract(led)
    led.apply({"proposal_id": "m1", "action": "claim", "contract_id": "C1"}, "exe")

    # crash between writing state and writing the event
    real_open = Path.open
    def crashing_open(self, *a, **k):
        if self.name == "events.jsonl" and a and a[0] == "a":
            raise KeyboardInterrupt("simulated crash")
        return real_open(self, *a, **k)
    Path.open = crashing_open
    try:
        led.apply({"proposal_id": "m2", "action": "submit", "contract_id": "C1", "payload": {"commit": "sha1"}}, "exe")
        raise AssertionError("crash not simulated")
    except KeyboardInterrupt:
        pass
    finally:
        Path.open = real_open

    # the state cache now disagrees with the log; restart = replay from log
    Ledger(d).replay()
    led = Ledger(d)
    assert led.contracts()["C1"]["status"] == "claimed", "replay did not restore last committed state"
    # the manager retries the interrupted proposal; it lands exactly once
    led.apply({"proposal_id": "m2", "action": "submit", "contract_id": "C1", "payload": {"commit": "sha1"}}, "exe")
    again = led.apply({"proposal_id": "m2", "action": "submit", "contract_id": "C1", "payload": {"commit": "sha1"}}, "exe")
    assert again["status"] == "duplicate", "retry applied twice"
    ids = [e["proposal_id"] for e in led.events()]
    assert ids.count("m2") == 1 and led.verify_chain()
    assert led.contracts()["C1"]["status"] == "submitted"

    # a wiped state cache is rebuilt fully from the log
    led.contracts_path.unlink()
    Ledger(d).replay()
    assert Ledger(d).contracts()["C1"]["commit"] == "sha1"
    return "crash mid-write recovered by replay; retried proposal applied exactly once; wiped state rebuilt from log"


def _project(acceptance="python -c \"import sys; sys.exit(0)\"", scope=("src/*",), max_attempts=3):
    led, d = fresh()
    for rel, text in {"src/app.py": "x = 1\n", "core/ledger.py": "# core\n", "tests/acceptance/test_c1.py": "# test\n",
                      "spec/spec.md": "# Spec\nBuild the app.\n"}.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(text.encode())  # exact LF bytes on every OS
    led.apply({"proposal_id": "create-C1", "action": "create", "contract_id": "C1", "payload": {
        "title": "demo", "spec_ref": "spec/spec.md#1", "acceptance": acceptance, "files_in_scope": list(scope),
        "max_attempts": max_attempts, "token_budget": 1000}}, "mgr")
    return led, d


def _ci_pass(led, commit, rid):
    led.apply({"proposal_id": f"ci-{rid}", "action": "test_run", "contract_id": "C1",
               "payload": {"run_id": rid, "commit": commit, "passed": True}}, "ci")


def drill_6():
    """You approve the spec, then it is edited -> agents are stopped until you re-approve."""
    led, d = _project()
    spec = d / "spec" / "spec.md"
    h = lambda: led.spec_hash_on_disk()  # noqa: E731
    expect_reject(led, {"proposal_id": "s0", "action": "approve_spec", "contract_id": "spec",
                        "payload": {"spec_hash": h()}}, "mgr", "manager approving spec")
    expect_reject(led, {"proposal_id": "s1", "action": "approve_spec", "contract_id": "spec",
                        "payload": {"spec_hash": "0" * 64}}, "me", "approving a spec that isn't on disk")
    led.apply({"proposal_id": "s2", "action": "approve_spec", "contract_id": "spec", "payload": {"spec_hash": h()}}, "me")
    spec.write_bytes(spec.read_bytes().replace(b"\n", b"\r\n"))  # Windows line endings are not a change
    led.apply({"proposal_id": "s3", "action": "claim", "contract_id": "C1"}, "exe")
    spec.write_text("# Spec\nBuild a different app.\n")
    assert led.spec_drift(), "edited spec not detected"
    expect_reject(led, {"proposal_id": "s4", "action": "usage", "contract_id": "C1", "payload": {"tokens": 1}}, "exe", "work on drifted spec")
    expect_reject(led, {"proposal_id": "s5", "action": "create", "contract_id": "C9", "payload": {}}, "mgr", "plan on drifted spec")
    led.apply({"proposal_id": "s6", "action": "approve_spec", "contract_id": "spec", "payload": {"spec_hash": h()}}, "me")
    led.apply({"proposal_id": "s7", "action": "usage", "contract_id": "C1", "payload": {"tokens": 1}}, "exe")
    Ledger(d).replay()  # history replays even though the spec on disk has moved on
    assert Ledger(d).approved_spec() == h() and Ledger(d).verify_chain()
    return "edited spec stops manager and executors; only you can approve; CRLF-only change ignored; re-approval resumes"


def drill_7():
    """Agent edits core, tests or the ledger during a run -> files put back, attempt cannot pass."""
    led, d = _project()
    core_before = (d / "core/ledger.py").read_bytes()
    test_before = (d / "tests/acceptance/test_c1.py").read_bytes()

    def cheater(ctx):
        (d / "src/app.py").write_text("x = 2\n")
        (d / "core/ledger.py").write_text("# disabled all checks\n")
        (d / "tests/acceptance/test_c1.py").unlink()
        (d / "tests/acceptance/test_fake.py").write_text("# always passes\n")
        cj = d / "ledger/contracts.json"
        cj.write_text(cj.read_text().replace('"open"', '"done"').replace('"claimed"', '"done"'))
        return {"claim": "done", "commit": "c1"}

    rep = runner.run_contract(d, "C1", cheater, run_id="r1")
    assert (d / "core/ledger.py").read_bytes() == core_before, "core file not restored"
    assert (d / "tests/acceptance/test_c1.py").read_bytes() == test_before, "deleted test not restored"
    assert not (d / "tests/acceptance/test_fake.py").exists(), "planted test not removed"
    assert (d / "src/app.py").read_text() == "x = 2\n", "legitimate work was undone"
    assert set(rep["violations"]) == {"core/ledger.py", "tests/acceptance/test_c1.py", "tests/acceptance/test_fake.py",
                                      "ledger/contracts.json"}, rep["violations"]
    led = Ledger(d)
    assert led.verify_chain() and led.contracts()["C1"]["status"] == "submitted"
    _ci_pass(led, "c1", "ci1")
    expect_reject(led, {"proposal_id": "a1", "action": "pass", "contract_id": "C1", "payload": {"run_id": "ci1"}}, "aud",
                  "pass after protected changes")
    expect_reject(led, {"proposal_id": "a2", "action": "run_report", "contract_id": "C1", "payload": {
        "run_id": "fake", "claim": "done", "commit": "c1", "changed": [], "violations": [], "out_of_scope": []}},
        "exe", "executor forging a clean report")
    return "core, test and ledger edits undone, planted test removed, in-scope work kept; pass refused; forged report refused"


def drill_8():
    """Agent edits files outside its contract's scope -> attempt cannot pass, even with a passing test."""
    led, d = _project(scope=("src/*",))

    def wanderer(ctx):
        (d / "src/app.py").write_text("x = 3\n")
        (d / "README.md").write_text("rewritten by an agent\n")
        return {"claim": "done", "commit": "w1"}

    rep = runner.run_contract(d, "C1", wanderer, run_id="r1")
    assert rep["out_of_scope"] == ["README.md"] and rep["violations"] == [], rep
    led = Ledger(d)
    _ci_pass(led, "w1", "ci1")
    expect_reject(led, {"proposal_id": "a1", "action": "pass", "contract_id": "C1", "payload": {"run_id": "ci1"}}, "aud",
                  "pass with out-of-scope edits")
    led.apply({"proposal_id": "a2", "action": "fail", "contract_id": "C1"}, "aud")
    led.apply({"proposal_id": "a3", "action": "reopen", "contract_id": "C1"}, "mgr")
    (d / "README.md").unlink()

    def focused(ctx):
        (d / "src/app.py").write_text("x = 4\n")
        return {"claim": "done", "commit": "w2"}

    rep = runner.run_contract(d, "C1", focused, run_id="r2")
    assert rep["out_of_scope"] == [] and rep["violations"] == []
    led = Ledger(d)
    _ci_pass(led, "w2", "ci2")
    led.apply({"proposal_id": "a4", "action": "pass", "contract_id": "C1", "payload": {"run_id": "ci2"}}, "aud")
    assert led.contracts()["C1"]["status"] == "done"
    return "out-of-scope edit blocks the pass despite a green test; the same contract passes once it stays in scope"


def drill_9():
    """Executor says "done" but the audit fails -> recorded as a false claim for the Learner."""
    led, d = _project()
    runner.run_contract(d, "C1", lambda ctx: {"claim": "done", "commit": "f1"}, run_id="r1")
    led = Ledger(d)
    res = led.apply({"proposal_id": "a1", "action": "fail", "contract_id": "C1"}, "aud")
    assert res["note"] == "false_claim" and led.false_claims("C1") == 1
    led.apply({"proposal_id": "a2", "action": "reopen", "contract_id": "C1"}, "mgr")
    runner.run_contract(d, "C1", lambda ctx: {"claim": "blocked: missing API key", "commit": "f2"}, run_id="r2")
    Ledger(d).apply({"proposal_id": "a3", "action": "fail", "contract_id": "C1"}, "aud")
    assert Ledger(d).false_claims("C1") == 1, "an honest 'blocked' was counted as a false claim"
    Ledger(d).replay()
    assert Ledger(d).false_claims("C1") == 1 and Ledger(d).verify_chain()
    return "false 'done' recorded (survives replay); an honest 'blocked' report is not counted"


def drill_10():
    """Runner dies at each point of an attempt -> finished work is kept, dead claims released, nothing redone."""
    # a) died after the clean report, before submitting: the work is submitted, not redone
    led, d = _project()
    real_apply = Ledger.apply
    def dies_on_submit(self, proposal, identity):
        if proposal.get("action") == "submit":
            raise KeyboardInterrupt("runner killed")
        return real_apply(self, proposal, identity)
    Ledger.apply = dies_on_submit
    try:
        runner.run_contract(d, "C1", lambda ctx: {"claim": "done", "commit": "k1"}, run_id="r1")
        raise AssertionError("crash not simulated")
    except KeyboardInterrupt:
        pass
    finally:
        Ledger.apply = real_apply
    out = runner.resume(d)
    led = Ledger(d)
    assert out["submitted"] == ["C1"] and led.contracts()["C1"]["status"] == "submitted"
    assert led.contracts()["C1"]["attempts"] == 0, "finished work was charged a retry"
    # b) submitted with a green CI run: left for the auditor, not reopened
    _ci_pass(led, "k1", "ci1")
    assert runner.resume(d)["ready_for_audit"] == ["C1"] and Ledger(d).contracts()["C1"]["status"] == "submitted"
    assert runner.resume(d) == {"submitted": [], "released": [], "ready_for_audit": ["C1"]}, "resume not repeatable"

    # c) died mid-work: released, the attempt counts, and the limit still parks it
    led, d = _project(max_attempts=2)
    for i in range(2):
        led.apply({"proposal_id": f"c{i}", "action": "claim", "contract_id": "C1"}, "forge-executor")
        out = runner.resume(d)
        assert out["released"] == ["C1"]
        led = Ledger(d)
    assert led.contracts()["C1"]["status"] == "parked", "endless crash loop not parked"
    return "finished work submitted without a retry; green work left for audit; dead claims released and counted; crash loop parked"


# ---------------------------------------------------------------------- 1E: Ben's channel (all mail faked)
def _channel_conductor(limits=None):
    """A conductor with fake agents, fake mail and a fake inbox; returns (conductor, sent mails, inbox, agent calls)."""
    from core.agents import FakeAgent
    from core.bootstrap import Conductor, Team
    root = Path(tempfile.mkdtemp(prefix="forge-drill-1e-"))
    calls, mails, inbox = [], [], []

    def agent_fn(prompt, cwd):
        calls.append(prompt)
        return '{"status":"ok"}', 1
    team = Team(**{r: FakeAgent(agent_fn, provider="claude") for r in
                   ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper", "planner")})
    lim = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9, "mail_per_hour": 3, "mail_per_day": 10}
    lim.update(limits or {})
    c = Conductor(root / "repo", root / "work", root / "state", team, lim, owner_email="ben@example.com",
                  mailer=lambda s, b: mails.append((s, b)), inbox=lambda: list(inbox), gh=lambda a: (0, ""),
                  checks={}, probes={}, push=False)
    return c, mails, inbox, calls


def _qs(c):
    return json.loads((c.state / "questions.json").read_text(encoding="utf-8"))


def drill_11():
    """STOP by email halts Forge: KILL is set, no agent runs, no email goes out."""
    c, mails, inbox, calls = _channel_conductor()
    c._ask("gate", "layer-1 is ready", "report", pr="7")
    sent = len(mails)
    inbox.append({"from": "Ben <ben@example.com>", "subject": "Re: [Forge] conductor started", "body": "STOP\n"})
    assert c.step() == "killed", "STOP did not halt"
    assert (c.state / "KILL").exists()
    inbox.clear()
    assert c.step() == "killed" and not calls, "work ran after STOP"
    assert c._send("[Forge] x", "y") is False and len(mails) == sent, "mail sent after STOP"
    return "an owner STOP set KILL; later steps stay killed, no agent ran, no email went out"


def drill_12():
    """A reply answers the right question only: its qid and its code, from the owner."""
    c, mails, inbox, _ = _channel_conductor()
    c._ask("replan", "Forge paused", "reasons")
    c._ask("blocked", "Task T1 is blocked", "details")
    qs = _qs(c)
    a, b = qs["replan-1"]["code"], qs["blocked-2"]["code"]
    inbox.append({"from": "ben@example.com", "subject": f"Re: [Forge Q-replan-1 {b}] x", "body": "go on"})
    c._handle_inbox()
    inbox.clear()
    assert all(v["status"] == "open" for v in _qs(c).values()), "a mismatched code answered a question"
    inbox.append({"from": "ben@example.com", "subject": f"Re: [Forge Q-blocked-2 {b}] x", "body": "try plan B"})
    c._handle_inbox()
    qs = _qs(c)
    assert qs["blocked-2"]["status"] == "answered" and qs["blocked-2"]["answer"] == "try plan B"
    assert qs["replan-1"]["status"] == "open", "the wrong question was answered"
    return "a reply with another question's code was ignored; the matching reply answered only its own question"


def drill_13():
    """A foreign sender is ignored: no STOP, no answer, even with the right code or Ben's address as a name."""
    c, _, inbox, _ = _channel_conductor()
    c._ask("gate", "layer-1 is ready", "report", pr="7")
    code = _qs(c)["gate-1"]["code"]
    for frm in ("mallory@evil.example", '"ben@example.com" <mallory@evil.example>',
                "mallory@evil.example, ben@example.com"):
        inbox[:] = [{"from": frm, "subject": f"Re: [Forge Q-gate-1 {code}] y", "body": "y"},
                    {"from": frm, "subject": "STOP", "body": "STOP"}]
        c._handle_inbox()
        assert not (c.state / "KILL").exists(), f"foreign STOP obeyed: {frm}"
        assert _qs(c)["gate-1"]["status"] == "open", f"foreign answer accepted: {frm}"
    return "replies and STOP from other senders (including Ben's address as a display name) changed nothing"


def drill_14():
    """The status page's Stop button halts Forge; a cross-site POST cannot."""
    import http.client
    import threading
    from core import status_page
    c, _, _, calls = _channel_conductor()
    srv = status_page.make_server(c.state, c.channel_in.parent, c.limits, port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(headers):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/stop", body=b"", headers={"Host": f"127.0.0.1:{port}", **headers})
            status = conn.getresponse().status
            conn.close()
            return status
        assert post({"Origin": "https://evil.example"}) == 403 and not (c.state / "KILL").exists()
        assert post({"Origin": f"http://127.0.0.1:{port}"}) == 303
    finally:
        srv.shutdown()
        srv.server_close()
    assert c.step() == "killed" and not calls, "Stop button did not halt"
    return "a cross-site Stop was refused; the page's Stop set KILL and the next step was killed"


def drill_15():
    """The digest policy cannot flood: 3 days, a new question every 10 minutes, nothing but the digest."""
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=-5))
    c, mails, _, _ = _channel_conductor({"digest_hour": 8, "quiet_start": 23, "quiet_end": 7})
    c.local_tz = tz
    c._save_queue({"layer": "layer-1", "tasks": [{"id": "T1", "title": "t", "status": "blocked", "notes": [],
                                                  "trouble_notes": [], "fail_signatures": []}]})
    now = [datetime(2026, 10, 1, 0, 0, tzinfo=tz)]
    c.clock = lambda: now[0]
    per_day: dict = {}
    while now[0] < datetime(2026, 10, 4, tzinfo=tz):
        c._ask("blocked", f"blocked {now[0].isoformat()}", "details")
        before = len(mails)
        c._handle_inbox()
        c._channel_tick()
        per_day[now[0].day] = per_day.get(now[0].day, 0) + len(mails) - before
        quiet = now[0].hour >= 23 or now[0].hour < 7
        assert not (quiet and len(mails) > before), "mail in quiet hours"
        now[0] += timedelta(minutes=10)
    assert all(n <= 3 for n in per_day.values()), f"too many emails: {per_day}"
    assert all(s.startswith("[Forge] Daily digest") for s, _ in mails), "a question was mailed on its own"
    return f"432 questions over 3 days produced {len(mails)} emails ({per_day}), all digests, none at night"


DRILLS = [
    (1, "False 'done' claim with failing test", drill_1),
    (2, "Agent edits tests or core files", drill_2),
    (3, "Malformed or invented ledger updates", drill_3),
    (4, "Budget and attempt limits", drill_4),
    (5, "Crash and restart mid-project", drill_5),
    (6, "Spec edited after approval", drill_6),
    (7, "Agent edits protected files during a run", drill_7),
    (8, "Agent edits files outside its contract", drill_8),
    (9, "False 'done' claim is recorded", drill_9),
    (10, "Runner crash at each step of an attempt", drill_10),
    (11, "1E: STOP by email halts", drill_11),
    (12, "1E: A reply answers the right question", drill_12),
    (13, "1E: A foreign sender is ignored", drill_13),
    (14, "1E: The status page Stop button halts", drill_14),
    (15, "1E: The digest policy cannot flood", drill_15),
]


def main() -> int:
    results, ok = [], True
    for num, name, fn in DRILLS:
        try:
            detail = fn()
            results.append({"drill": num, "name": name, "status": "Passing", "detail": detail})
            print(f"PASS  drill {num}: {name}\n      {detail}")
        except Exception as e:  # noqa: BLE001
            ok = False
            results.append({"drill": num, "name": name, "status": "Failing", "detail": repr(e)})
            print(f"FAIL  drill {num}: {name}\n      {e!r}")
            traceback.print_exc()
    (ROOT / "drills" / "results.json").write_text(json.dumps(results, indent=2))
    print("\nALL DRILLS PASS" if ok else "\nDRILLS FAILING")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
