"""T1B3c: ledger crash recovery, event-backed completion and merge evidence.

Temporary ledgers are built the way drills/run_drills.py builds them: a
throwaway folder with a roles.json, and every change made through apply().
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import ledger as ledger_mod
from core.ledger import Ledger, Rejected

ROLES = {"mgr": "manager", "exe": "executor", "aud": "auditor", "ci": "ci", "me": "human",
         "forge-core": "core"}

SHA_TASK = "1" * 40
SHA_MERGE = "a" * 40
SHA_FINAL = "b" * 40
SHA_OTHER = "c" * 40


class SimulatedCrash(BaseException):
    """Raised to simulate a process dying; not an Exception, like KeyboardInterrupt."""


def create_prop(cid, pid=None):
    return {"proposal_id": pid or f"create-{cid}", "action": "create", "contract_id": cid, "payload": {
        "title": "demo", "spec_ref": "spec.md#1", "acceptance": "python -c pass",
        "files_in_scope": ["src/demo.py"], "max_attempts": 3, "token_budget": 1000}}


def crash_once(real):
    """A replacement for Ledger._append_event that dies on its first call only."""
    state = {"n": 0}

    def fake(self, event):
        state["n"] += 1
        if state["n"] == 1:
            raise SimulatedCrash("died before the event landed")
        return real(self, event)
    return fake


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="forge-recovery-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
        self.led = Ledger(self.dir)

    # helpers -----------------------------------------------------------
    def events_bytes(self) -> bytes:
        return self.led.events_path.read_bytes()

    def head(self):
        p = self.led.root / "ledger" / "head.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def caches(self) -> dict:
        return {"contracts": self.led.contracts(), "test_runs": self.led.test_runs(),
                "reports": self.led.reports(), "spec_state": self.led._read_json(self.led.spec_state_path, {})}

    def assert_consistent(self):
        """Cache files match what the event log derives, head matches, chain verifies."""
        led = Ledger(self.dir)
        self.assertTrue(led.verify_chain())
        d = led.derive()
        self.assertEqual(self.caches(), {k: d[k] for k in ("contracts", "test_runs", "reports", "spec_state")})
        last = led.events()[-1]["hash"] if led.events() else "genesis"
        self.assertEqual(d["head"], last)
        self.assertEqual(self.head(), {"hash": last})

    def to_submitted(self, cid="C1", commit=SHA_TASK):
        self.led.apply(create_prop(cid), "mgr")
        self.led.apply({"proposal_id": f"claim-{cid}", "action": "claim", "contract_id": cid}, "exe")
        self.led.apply({"proposal_id": f"report-{cid}", "action": "run_report", "contract_id": cid, "payload": {
            "run_id": "x", "claim": "done", "commit": commit, "changed": ["src/demo.py"],
            "violations": [], "out_of_scope": []}}, "forge-core")
        self.led.apply({"proposal_id": f"submit-{cid}", "action": "submit", "contract_id": cid,
                        "payload": {"commit": commit}}, "exe")
        self.led.apply({"proposal_id": f"ci-{cid}", "action": "test_run", "contract_id": cid,
                        "payload": {"run_id": f"r-{cid}", "commit": commit, "passed": True}}, "ci")

    def pass_prop(self, cid="C1", pid="pass-1", **extra):
        return {"proposal_id": pid, "action": "pass", "contract_id": cid,
                "payload": dict({"run_id": f"r-{cid}"}, **extra)}

    def pass_events(self, cid="C1"):
        return [e for e in Ledger(self.dir).events() if e["action"] == "pass" and e["contract_id"] == cid]


# ---------------------------------------------------------------- head marker and _append_event
class HeadMarkerTests(LedgerTestBase):
    def test_apply_writes_head_marker_of_the_appended_event(self):
        self.led.apply(create_prop("C1"), "mgr")
        self.assertEqual(self.head(), {"hash": self.led.events()[-1]["hash"]})
        self.led.apply({"proposal_id": "c", "action": "claim", "contract_id": "C1"}, "exe")
        self.assertEqual(self.head(), {"hash": self.led.events()[-1]["hash"]})
        self.assertEqual(len(self.led.events()), 2)

    def test_append_event_is_the_only_place_the_log_grows(self):
        with mock.patch.object(Ledger, "_append_event", autospec=True) as app:
            self.led.apply(create_prop("C1"), "mgr")
        self.assertEqual(app.call_count, 1)
        ev = app.call_args[0][1]
        self.assertEqual(ev["proposal_id"], "create-C1")
        self.assertEqual(ev["prev"], "genesis")
        self.assertFalse(self.led.events_path.exists() and self.led.events_path.read_bytes())

    def test_snapshot_ignores_head_marker(self):
        self.led.apply(create_prop("C1"), "mgr")
        before = self.led.snapshot()
        (self.dir / "ledger" / "head.json").write_text('{"hash": "zzz"}', encoding="utf-8")
        self.assertEqual(self.led.snapshot(), before)

    def test_legacy_ledger_without_head_marker_is_rebuilt_on_apply(self):
        self.to_submitted()
        (self.dir / "ledger" / "head.json").unlink()
        self.led.apply(self.pass_prop(), "aud")
        self.assertEqual(self.led.contracts()["C1"]["status"], "done")
        self.assert_consistent()


# ---------------------------------------------------------------- crash before the event append
class CrashBeforeAppendTests(LedgerTestBase):
    def test_pass_crash_then_reconcile_and_retry(self):
        self.to_submitted()
        with mock.patch.object(Ledger, "_append_event", crash_once(Ledger._append_event)):
            with self.assertRaises(SimulatedCrash):
                Ledger(self.dir).apply(self.pass_prop(), "aud")
        led = Ledger(self.dir)
        # the cache got ahead of the log...
        on_disk = json.loads(led.contracts_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["C1"]["status"], "done")
        # ...but completion is event-backed, so the contract is not complete
        self.assertIsNone(led.completion("C1"))
        self.assertTrue(led.reconcile())
        self.assertEqual(led.contracts()["C1"]["status"], "submitted")
        self.assertFalse(led.reconcile(), "a second reconcile found more to fix")
        res = led.apply(self.pass_prop(), "aud")
        self.assertEqual(res["status"], "applied")
        comp = led.completion("C1")
        self.assertIsNotNone(comp)
        self.assertEqual((comp["action"], comp["contract_id"], comp["proposal_id"]), ("pass", "C1", "pass-1"))
        self.assertEqual(comp["hash"], led.events()[-1]["hash"])
        self.assertEqual(len(self.pass_events()), 1)
        self.assertEqual(led.apply(self.pass_prop(), "aud")["status"], "duplicate")
        self.assertEqual(len(self.pass_events()), 1)
        self.assert_consistent()

    def test_pass_crash_healed_by_next_apply_without_reconcile(self):
        self.to_submitted()
        with mock.patch.object(Ledger, "_append_event", crash_once(Ledger._append_event)):
            with self.assertRaises(SimulatedCrash):
                Ledger(self.dir).apply(self.pass_prop(), "aud")
        # a fail decision would be refused against the phantom "done"; after healing it is allowed
        res = Ledger(self.dir).apply({"proposal_id": "f1", "action": "fail", "contract_id": "C1"}, "aud")
        self.assertEqual(res["status"], "applied")
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "failed")
        self.assertIsNone(Ledger(self.dir).completion("C1"))
        self.assert_consistent()

    def _crash(self, proposal, identity):
        with mock.patch.object(Ledger, "_append_event", crash_once(Ledger._append_event)):
            with self.assertRaises(SimulatedCrash):
                Ledger(self.dir).apply(proposal, identity)

    def test_create_crash_phantom_contract_disappears(self):
        self.led.apply(create_prop("C1"), "mgr")
        self._crash(create_prop("C2"), "mgr")
        self.assertIn("C2", Ledger(self.dir).contracts())  # phantom in the cache
        self.led.apply(create_prop("C3"), "mgr")  # any next proposal heals first
        c = Ledger(self.dir).contracts()
        self.assertNotIn("C2", c)
        self.assertEqual(sorted(c), ["C1", "C3"])
        self.assertTrue(Ledger(self.dir).verify_chain())
        # the interrupted proposal applies cleanly under its own id
        self.assertEqual(self.led.apply(create_prop("C2"), "mgr")["status"], "applied")
        self.assertEqual(sorted(Ledger(self.dir).contracts()), ["C1", "C2", "C3"])
        self.assert_consistent()

    def test_create_crash_then_same_create_is_not_refused_as_existing(self):
        self._crash(create_prop("C1"), "mgr")
        # without healing this would be Rejected("contract C1 already exists")
        self.assertEqual(Ledger(self.dir).apply(create_prop("C1"), "mgr")["status"], "applied")
        self.assertEqual([e["proposal_id"] for e in Ledger(self.dir).events()], ["create-C1"])
        self.assert_consistent()

    def test_claim_crash(self):
        self.led.apply(create_prop("C1"), "mgr")
        self.led.apply(create_prop("C2"), "mgr")
        claim = {"proposal_id": "cl1", "action": "claim", "contract_id": "C1"}
        self._crash(claim, "exe")
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "claimed")
        self.led.apply({"proposal_id": "cl2", "action": "claim", "contract_id": "C2"}, "exe")
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "open")
        self.assertTrue(Ledger(self.dir).verify_chain())
        self.assertEqual(self.led.apply(claim, "exe")["status"], "applied")
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "claimed")
        self.assert_consistent()

    def test_submit_crash(self):
        self.led.apply(create_prop("C1"), "mgr")
        self.led.apply({"proposal_id": "cl", "action": "claim", "contract_id": "C1"}, "exe")
        sub = {"proposal_id": "s1", "action": "submit", "contract_id": "C1", "payload": {"commit": SHA_TASK}}
        self._crash(sub, "exe")
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["commit"], SHA_TASK)
        self.led.apply(create_prop("C9"), "mgr")
        c = Ledger(self.dir).contracts()["C1"]
        self.assertEqual((c["status"], c["commit"]), ("claimed", None))
        self.assertTrue(Ledger(self.dir).verify_chain())
        self.assertEqual(self.led.apply(sub, "exe")["status"], "applied")
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "submitted")
        self.assert_consistent()

    def test_test_run_crash_phantom_run_cannot_back_a_pass(self):
        self.led.apply(create_prop("C1"), "mgr")
        self.led.apply({"proposal_id": "cl", "action": "claim", "contract_id": "C1"}, "exe")
        self.led.apply({"proposal_id": "rep", "action": "run_report", "contract_id": "C1", "payload": {
            "run_id": "x", "claim": "done", "commit": SHA_TASK, "changed": [], "violations": [],
            "out_of_scope": []}}, "forge-core")
        self.led.apply({"proposal_id": "s", "action": "submit", "contract_id": "C1",
                        "payload": {"commit": SHA_TASK}}, "exe")
        run = {"proposal_id": "ci1", "action": "test_run", "contract_id": "C1",
               "payload": {"run_id": "r1", "commit": SHA_TASK, "passed": True}}
        self._crash(run, "ci")
        self.assertIn("r1", Ledger(self.dir).test_runs())  # phantom run in the cache
        # a pass relying on the phantom run must be refused: the run never landed
        with self.assertRaises(Rejected):
            Ledger(self.dir).apply({"proposal_id": "p", "action": "pass", "contract_id": "C1",
                                    "payload": {"run_id": "r1"}}, "aud")
        self.assertNotIn("r1", Ledger(self.dir).test_runs())
        self.assertTrue(Ledger(self.dir).verify_chain())
        self.assertEqual(self.led.apply(run, "ci")["status"], "applied")
        self.assertEqual(self.led.apply({"proposal_id": "p", "action": "pass", "contract_id": "C1",
                                         "payload": {"run_id": "r1"}}, "aud")["status"], "applied")
        self.assert_consistent()

    def test_crash_between_each_individual_cache_write(self):
        """Reviewer note: a crash after any single cache write must still be detected."""
        real = ledger_mod._atomic_write
        for n in range(1, 6):
            with self.subTest(crash_on_write=n):
                self.setUp()
                self.to_submitted()
                calls = {"n": 0}

                def dying(path, text, _n=n):
                    calls["n"] += 1
                    if calls["n"] == _n:
                        raise SimulatedCrash(f"died on cache write {_n}")
                    return real(path, text)
                with mock.patch.object(ledger_mod, "_atomic_write", dying):
                    with self.assertRaises(SimulatedCrash):
                        Ledger(self.dir).apply(self.pass_prop(), "aud")
                self.assertIsNone(Ledger(self.dir).completion("C1"))
                # against a phantom "done", fail would be refused
                res = Ledger(self.dir).apply({"proposal_id": "f", "action": "fail", "contract_id": "C1"}, "aud")
                self.assertEqual(res["status"], "applied")
                self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "failed")
                self.assertEqual(len(self.pass_events()), 0)
                self.assert_consistent()

    def test_crash_after_every_cache_write_is_seen_by_reconcile(self):
        real = ledger_mod._atomic_write
        for n in range(1, 6):
            with self.subTest(crash_on_write=n):
                self.setUp()
                self.to_submitted()
                good = self.caches()
                calls = {"n": 0}

                def dying(path, text, _n=n):
                    calls["n"] += 1
                    if calls["n"] == _n:
                        raise SimulatedCrash("died")
                    return real(path, text)
                with mock.patch.object(ledger_mod, "_atomic_write", dying):
                    with self.assertRaises(SimulatedCrash):
                        Ledger(self.dir).apply(self.pass_prop(), "aud")
                led = Ledger(self.dir)
                # write 1 is the head marker: dying there changed nothing; any later crash left it ahead
                self.assertEqual(led.reconcile(), n > 1)
                self.assertEqual(self.caches(), good)
                self.assertFalse(led.reconcile())
                self.assertEqual(led.apply(self.pass_prop(), "aud")["status"], "applied")
                self.assertEqual(len(self.pass_events()), 1)
                self.assert_consistent()

    def test_torn_append_is_recovered(self):
        """The append itself died half way: a partial line and a head marker ahead of the log."""
        self.to_submitted()
        good_bytes = self.events_bytes()

        def torn(self_, event):
            with self_.events_path.open("ab") as f:
                f.write(json.dumps(event, sort_keys=True).encode("utf-8")[:40])
            raise SimulatedCrash("died mid-append")
        with mock.patch.object(Ledger, "_append_event", torn):
            with self.assertRaises(SimulatedCrash):
                Ledger(self.dir).apply(self.pass_prop(), "aud")
        led = Ledger(self.dir)
        self.assertIsNone(led.completion("C1"))
        self.assertEqual(self.events_bytes(), good_bytes)  # completion's repair_tail dropped the torn line
        self.assertEqual(led.apply(self.pass_prop(), "aud")["status"], "applied")
        self.assertIsNotNone(led.completion("C1"))
        self.assert_consistent()


# ---------------------------------------------------------------- repair_tail
class RepairTailTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.led.apply(create_prop("C1"), "mgr")
        self.led.apply({"proposal_id": "cl", "action": "claim", "contract_id": "C1"}, "exe")
        self.good = self.events_bytes()

    def _append_raw(self, raw: bytes):
        with self.led.events_path.open("ab") as f:
            f.write(raw)

    def test_clean_log_is_left_alone(self):
        self.assertFalse(self.led.repair_tail())
        self.assertEqual(self.events_bytes(), self.good)

    def test_torn_last_line_is_truncated(self):
        self._append_raw(b'{"proposal_id": "half", "act')
        self.assertTrue(self.led.repair_tail())
        self.assertEqual(self.events_bytes(), self.good)
        self.assertTrue(self.led.verify_chain())
        self.assertFalse(self.led.repair_tail())

    def test_unterminated_but_parseable_last_line_is_truncated(self):
        ev = dict(self.led.events()[-1], proposal_id="zz")
        self._append_raw(json.dumps(ev, sort_keys=True).encode("utf-8"))
        self.assertTrue(self.led.repair_tail())
        self.assertEqual(self.events_bytes(), self.good)

    def test_terminated_garbage_last_line_is_truncated(self):
        self._append_raw(b"not json at all\n")
        self.assertTrue(self.led.repair_tail())
        self.assertEqual(self.events_bytes(), self.good)
        self.assertTrue(self.led.verify_chain())

    def test_torn_multibyte_utf8_is_truncated(self):
        self._append_raw('{"title": "café'.encode("utf-8")[:-1])
        self.assertTrue(self.led.repair_tail())
        self.assertEqual(self.events_bytes(), self.good)

    def test_torn_only_line_empties_the_log(self):
        self.led.events_path.write_bytes(b'{"proposal_id": "x"')
        self.assertTrue(self.led.repair_tail())
        self.assertEqual(self.events_bytes(), b"")
        self.assertTrue(self.led.verify_chain())

    def test_missing_log_is_fine(self):
        led = Ledger(Path(tempfile.mkdtemp(prefix="forge-empty-")))
        self.addCleanup(shutil.rmtree, led.root, True)
        self.assertFalse(led.repair_tail())

    def test_corrupt_middle_line_raises_and_drops_nothing(self):
        lines = self.good.split(b"\n")
        corrupt = lines[0] + b"\n" + b"{broken\n" + lines[1] + b"\n"
        self.led.events_path.write_bytes(corrupt)
        with self.assertRaises(Rejected) as ctx:
            self.led.repair_tail()
        self.assertIn("event log corrupt", str(ctx.exception))
        self.assertEqual(self.events_bytes(), corrupt)
        # apply refuses too, and still drops nothing
        with self.assertRaises(Rejected):
            self.led.apply(create_prop("C2"), "mgr")
        self.assertEqual(self.events_bytes(), corrupt)
        with self.assertRaises(Rejected):
            self.led.reconcile()
        self.assertEqual(self.events_bytes(), corrupt)

    def test_corrupt_middle_line_with_torn_tail_still_raises(self):
        lines = self.good.split(b"\n")
        corrupt = lines[0] + b"\n" + b"{broken\n" + lines[1] + b"\n" + b'{"half'
        self.led.events_path.write_bytes(corrupt)
        with self.assertRaises(Rejected):
            self.led.repair_tail()
        self.assertEqual(self.events_bytes(), corrupt)

    def test_apply_repairs_torn_tail_then_proceeds(self):
        self._append_raw(b'{"torn')
        self.led.apply({"proposal_id": "s", "action": "submit", "contract_id": "C1",
                        "payload": {"commit": SHA_TASK}}, "exe")
        self.assertEqual(len(self.led.events()), 3)
        self.assert_consistent()


# ---------------------------------------------------------------- derive / rebuild / replay / reconcile
class DeriveRebuildTests(LedgerTestBase):
    def test_derive_empty_ledger(self):
        d = self.led.derive()
        self.assertEqual(d, {"contracts": {}, "test_runs": {}, "reports": {}, "spec_state": {}, "head": "genesis"})
        self.assertFalse((self.dir / "ledger").exists() and any((self.dir / "ledger").iterdir()))

    def test_derive_matches_caches_and_never_writes(self):
        self.to_submitted()
        (self.dir / "spec").mkdir()
        (self.dir / "spec" / "spec.md").write_text("# Spec\n", encoding="utf-8")
        self.led.apply({"proposal_id": "ap", "action": "approve_spec", "contract_id": "spec",
                        "payload": {"spec_hash": self.led.spec_hash_on_disk()}}, "me")
        files_before = {p.name: p.read_bytes() for p in (self.dir / "ledger").iterdir()}
        d = self.led.derive()
        self.assertEqual({p.name: p.read_bytes() for p in (self.dir / "ledger").iterdir()}, files_before)
        self.assertEqual(set(d), {"contracts", "test_runs", "reports", "spec_state", "head"})
        self.assertEqual(d["contracts"]["C1"]["status"], "submitted")
        self.assertEqual(d["test_runs"]["r-C1"]["commit"], SHA_TASK)
        self.assertEqual(d["reports"]["C1"]["run_id"], "x")
        self.assertEqual(d["spec_state"], {"hash": self.led.spec_hash_on_disk()})
        self.assertEqual(d["head"], self.led.events()[-1]["hash"])
        self.assert_consistent()

    def test_derive_ignores_stale_caches(self):
        self.to_submitted()
        self.led.contracts_path.write_text('{"C1": {"status": "done"}}', encoding="utf-8")
        self.assertEqual(self.led.derive()["contracts"]["C1"]["status"], "submitted")

    def test_derive_refuses_a_tampered_log(self):
        self.to_submitted()
        lines = self.led.events_path.read_text(encoding="utf-8").splitlines()
        ev = json.loads(lines[0]); ev["payload"]["token_budget"] = 999999
        lines[0] = json.dumps(ev, sort_keys=True)
        self.led.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(Rejected):
            self.led.derive()

    def test_replay_on_normal_ledger(self):
        self.to_submitted()
        self.led.apply(self.pass_prop(), "aud")
        contracts, runs, reports = self.led.contracts(), self.led.test_runs(), self.led.reports()
        events = self.events_bytes()
        Ledger(self.dir).replay()
        led = Ledger(self.dir)
        self.assertEqual(led.contracts(), contracts)
        self.assertEqual(led.test_runs(), runs)
        self.assertEqual(led.reports(), reports)
        self.assertEqual(self.events_bytes(), events)
        self.assert_consistent()

    def test_rebuild_restores_wiped_and_tampered_caches(self):
        self.to_submitted()
        good = self.caches()
        self.led.contracts_path.unlink()
        self.led.runs_path.write_text('{"fake": {"contract_id": "C1", "commit": "x", "passed": true}}', encoding="utf-8")
        (self.dir / "ledger" / "head.json").unlink()
        events = self.events_bytes()
        self.led.rebuild()
        self.assertEqual(self.caches(), good)
        self.assertEqual(self.events_bytes(), events)
        self.assert_consistent()

    def test_crash_inside_rebuild_leaves_log_intact_and_is_recoverable(self):
        real = ledger_mod._atomic_write
        # count every write (the first ones land in derive's scratch ledger), or only this ledger's writes
        for only_own in (False, True):
            with self.subTest(only_own_writes=only_own):
                self.setUp()
                self.to_submitted()
                self.led.apply(self.pass_prop(), "aud")
                good = self.caches()
                # stale caches that rebuild must fix
                self.led.contracts_path.write_text("{}", encoding="utf-8")
                self.led.reports_path.write_text("{}", encoding="utf-8")
                events = self.events_bytes()
                own = (self.dir / "ledger").resolve()
                calls = {"n": 0}

                def dying(path, text):
                    if only_own and Path(path).resolve().parent != own:
                        return real(path, text)
                    calls["n"] += 1
                    if calls["n"] == 2:
                        raise SimulatedCrash("died inside rebuild")
                    return real(path, text)
                with mock.patch.object(ledger_mod, "_atomic_write", dying):
                    with self.assertRaises(SimulatedCrash):
                        Ledger(self.dir).rebuild()
                self.assertEqual(self.events_bytes(), events)
                led = Ledger(self.dir)
                self.assertTrue(led.reconcile())
                self.assertEqual(self.caches(), good)
                self.assertEqual(self.events_bytes(), events)
                self.assertFalse(led.reconcile())
                self.assert_consistent()

    def test_crash_inside_rebuild_is_healed_by_next_apply(self):
        """Even when the head marker matched before, a half-done rebuild is noticed by apply."""
        self.to_submitted()
        self.led.contracts_path.write_text(
            self.led.contracts_path.read_text(encoding="utf-8").replace('"submitted"', '"done"'),
            encoding="utf-8")  # a stale cache with a matching head marker
        real = ledger_mod._atomic_write
        own = (self.dir / "ledger").resolve()
        for n in (1, 2, 3, 4, 5):
            calls = {"n": 0}

            def dying(path, text, _n=n):
                if Path(path).resolve().parent != own:  # derive's scratch ledger writes too
                    return real(path, text)
                calls["n"] += 1
                if calls["n"] == _n:
                    raise SimulatedCrash("died inside rebuild")
                return real(path, text)
            with mock.patch.object(ledger_mod, "_atomic_write", dying):
                with self.assertRaises(SimulatedCrash):
                    Ledger(self.dir).rebuild()
        res = Ledger(self.dir).apply({"proposal_id": "f", "action": "fail", "contract_id": "C1"}, "aud")
        self.assertEqual(res["status"], "applied")
        self.assert_consistent()

    def test_reconcile_clean_ledger_is_noop(self):
        self.to_submitted()
        before = {p.name: p.read_bytes() for p in (self.dir / "ledger").iterdir()}
        self.assertFalse(self.led.reconcile())
        self.assertEqual({p.name: p.read_bytes() for p in (self.dir / "ledger").iterdir()}, before)

    def test_reconcile_empty_ledger(self):
        self.assertTrue(self.led.reconcile())  # no head marker yet
        self.assertEqual(self.head(), {"hash": "genesis"})
        self.assertFalse(self.led.reconcile())

    def test_reconcile_fixes_missing_or_wrong_head(self):
        self.to_submitted()
        head = self.dir / "ledger" / "head.json"
        head.unlink()
        self.assertTrue(self.led.reconcile())
        self.assertEqual(self.head(), {"hash": self.led.events()[-1]["hash"]})
        head.write_text('{"hash": "genesis"}', encoding="utf-8")
        self.assertTrue(self.led.reconcile())
        self.assert_consistent()

    def test_reconcile_fixes_cache_that_differs_with_matching_head(self):
        self.to_submitted()
        good = self.caches()
        for path in (self.led.contracts_path, self.led.runs_path, self.led.reports_path, self.led.spec_state_path):
            with self.subTest(file=path.name):
                path.write_text('{"tampered": {}}', encoding="utf-8")
                self.assertTrue(self.led.reconcile())
                self.assertEqual(self.caches(), good)
                self.assertFalse(self.led.reconcile())

    def test_reconcile_refuses_broken_chain(self):
        self.to_submitted()
        lines = self.led.events_path.read_text(encoding="utf-8").splitlines()
        ev = json.loads(lines[1]); ev["identity"] = "me"
        lines[1] = json.dumps(ev, sort_keys=True)
        self.led.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        caches = {p.name: p.read_bytes() for p in (self.dir / "ledger").iterdir()}
        with self.assertRaises(Rejected):
            self.led.reconcile()
        with self.assertRaises(Rejected):
            self.led.rebuild()
        self.assertEqual({p.name: p.read_bytes() for p in (self.dir / "ledger").iterdir()}, caches)

    def test_reconcile_repairs_torn_tail_first(self):
        self.to_submitted()
        good = self.events_bytes()
        with self.led.events_path.open("ab") as f:
            f.write(b'{"x":')
        self.assertFalse(self.led.reconcile())  # log repaired; head and caches already matched
        self.assertEqual(self.events_bytes(), good)
        self.assert_consistent()


# ---------------------------------------------------------------- completion
class CompletionTests(LedgerTestBase):
    def test_none_until_pass_then_the_event(self):
        self.assertIsNone(self.led.completion("C1"))
        self.to_submitted()
        self.assertIsNone(self.led.completion("C1"))
        self.led.apply(self.pass_prop(), "aud")
        comp = self.led.completion("C1")
        self.assertEqual(comp["action"], "pass")
        self.assertEqual(comp["payload"], {"run_id": "r-C1"})
        self.assertEqual(comp["result_status"], "done")
        self.assertIsNone(self.led.completion("C2"))

    def test_never_reads_contracts_json(self):
        self.to_submitted()
        self.led.apply(self.pass_prop(), "aud")
        self.led.contracts_path.write_text("not json", encoding="utf-8")
        with mock.patch.object(Ledger, "contracts", side_effect=AssertionError("read contracts")), \
                mock.patch.object(Ledger, "_read_json", side_effect=AssertionError("read a cache")):
            self.assertEqual(self.led.completion("C1")["proposal_id"], "pass-1")

    def test_cache_saying_done_is_not_completion(self):
        self.to_submitted()
        self.led.contracts_path.write_text(
            self.led.contracts_path.read_text(encoding="utf-8").replace('"submitted"', '"done"'), encoding="utf-8")
        self.assertEqual(self.led.contracts()["C1"]["status"], "done")
        self.assertIsNone(self.led.completion("C1"))

    def test_other_contracts_pass_does_not_count(self):
        self.to_submitted("C1")
        self.to_submitted("C2")
        self.led.apply(self.pass_prop("C2", pid="pass-2"), "aud")
        self.assertIsNone(self.led.completion("C1"))
        self.assertEqual(self.led.completion("C2")["contract_id"], "C2")


# ---------------------------------------------------------------- merge evidence on pass
class MergeEvidenceTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.to_submitted()
        self.led.apply({"proposal_id": "ci-merge", "action": "test_run", "contract_id": "C1",
                        "payload": {"run_id": "rm", "commit": SHA_MERGE, "passed": True}}, "ci")

    def merge(self, **over):
        return dict({"sha": SHA_MERGE, "parents": [SHA_OTHER, SHA_TASK], "kind": "local", "run_id": "rm",
                     "verdict": "pass", "reasons": ["judges agreed"]}, **over)

    def evidence(self, **over):
        return dict({"task_commit": SHA_TASK, "final_sha": SHA_FINAL, "merges": [self.merge()]}, **over)

    def test_valid_merge_evidence_is_applied_and_recorded(self):
        res = self.led.apply(self.pass_prop(**self.evidence()), "aud")
        self.assertEqual(res["status"], "applied")
        self.assertEqual(self.led.contracts()["C1"]["status"], "done")
        payload = self.led.completion("C1")["payload"]
        self.assertEqual(payload["task_commit"], SHA_TASK)
        self.assertEqual(payload["final_sha"], SHA_FINAL)
        self.assertEqual(payload["merges"], [self.merge()])
        self.assert_consistent()
        Ledger(self.dir).replay()  # replays cleanly with the evidence
        self.assertEqual(Ledger(self.dir).contracts()["C1"]["status"], "done")

    def test_divergence_merge_and_multiple_merges(self):
        self.led.apply({"proposal_id": "ci-m2", "action": "test_run", "contract_id": "C1",
                        "payload": {"run_id": "rm2", "commit": SHA_FINAL, "passed": True}}, "ci")
        merges = [self.merge(), self.merge(sha=SHA_FINAL, kind="divergence", run_id="rm2", reasons=[])]
        self.assertEqual(self.led.apply(self.pass_prop(**self.evidence(merges=merges)), "aud")["status"], "applied")

    def test_final_sha_without_merges_and_empty_merges(self):
        self.assertEqual(self.led.apply(self.pass_prop(final_sha=SHA_FINAL, merges=[]), "aud")["status"], "applied")

    def test_task_commit_alone(self):
        self.assertEqual(self.led.apply(self.pass_prop(task_commit=SHA_TASK), "aud")["status"], "applied")

    def test_pass_without_merge_fields_works_as_before(self):
        res = self.led.apply(self.pass_prop(), "aud")
        self.assertEqual(res, {"status": "applied", "contract": self.led.contracts()["C1"], "note": None})
        self.assertEqual(self.led.contracts()["C1"]["status"], "done")
        self.assertEqual(self.led.completion("C1")["payload"], {"run_id": "r-C1"})

    def test_bad_evidence_is_rejected_and_changes_nothing(self):
        # a failing run and a run for another contract, recorded for the negative cases
        self.led.apply({"proposal_id": "ci-bad", "action": "test_run", "contract_id": "C1",
                        "payload": {"run_id": "rbad", "commit": SHA_OTHER, "passed": False}}, "ci")
        self.led.apply(create_prop("C2"), "mgr")
        self.led.apply({"proposal_id": "ci-c2", "action": "test_run", "contract_id": "C2",
                        "payload": {"run_id": "rc2", "commit": SHA_OTHER, "passed": True}}, "ci")
        cases = {
            "missing run": self.evidence(merges=[self.merge(run_id="nope")]),
            "run for another sha": self.evidence(merges=[self.merge(sha=SHA_FINAL)]),
            "failed run": self.evidence(merges=[self.merge(sha=SHA_OTHER, run_id="rbad")]),
            "run of another contract": self.evidence(merges=[self.merge(sha=SHA_OTHER, run_id="rc2")]),
            "verdict fail": self.evidence(merges=[self.merge(verdict="fail")]),
            "bad kind": self.evidence(merges=[self.merge(kind="remote")]),
            "merges without final_sha": {"task_commit": SHA_TASK, "merges": [self.merge()]},
            "wrong task_commit": self.evidence(task_commit=SHA_OTHER),
            "malformed final_sha short": self.evidence(final_sha="b" * 39),
            "malformed final_sha upper": self.evidence(final_sha="B" * 40),
            "malformed final_sha non-hex": self.evidence(final_sha="g" * 40),
            "final_sha not text": self.evidence(final_sha=12),
            "merges not a list": self.evidence(merges={"sha": SHA_MERGE}),
            "merge not a dict": self.evidence(merges=["x"]),
            "merge sha malformed": self.evidence(merges=[self.merge(sha="a" * 7)]),
            "merge missing sha": self.evidence(merges=[{k: v for k, v in self.merge().items() if k != "sha"}]),
            "merge missing verdict": self.evidence(merges=[{k: v for k, v in self.merge().items() if k != "verdict"}]),
            "parents not a list": self.evidence(merges=[self.merge(parents=SHA_TASK)]),
            "parents not text": self.evidence(merges=[self.merge(parents=[1])]),
            "reasons not a list": self.evidence(merges=[self.merge(reasons="ok")]),
            "reasons not text": self.evidence(merges=[self.merge(reasons=[None])]),
            "run_id not text": self.evidence(merges=[self.merge(run_id=5)]),
            "task_commit not text": self.evidence(task_commit=["x"]),
        }
        for i, (label, extra) in enumerate(cases.items()):
            with self.subTest(label):
                before = self.led.snapshot()
                with self.assertRaises(Rejected) as ctx:
                    self.led.apply(self.pass_prop(pid=f"bad-{i}", **extra), "aud")
                self.assertTrue(str(ctx.exception).startswith("pass merge evidence incomplete:"), str(ctx.exception))
                self.assertEqual(self.led.snapshot(), before)
                self.assertIsNone(self.led.completion("C1"))
        self.assertEqual(self.led.contracts()["C1"]["status"], "submitted")
        self.assertEqual(self.led.apply(self.pass_prop(**self.evidence()), "aud")["status"], "applied")

    def test_old_evidence_rules_still_come_first(self):
        # a good merge cannot stand in for a missing task run
        before = self.led.snapshot()
        with self.assertRaises(Rejected) as ctx:
            self.led.apply({"proposal_id": "p", "action": "pass", "contract_id": "C1",
                            "payload": dict(self.evidence(), run_id="rm")}, "aud")
        self.assertIn("CI-recorded passing test run", str(ctx.exception))
        self.assertEqual(self.led.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
