"""Behavioral contract for challenge records and evidence-backed rescue."""
import json
from pathlib import Path
import tempfile
import unittest

from core.ledger import Ledger, Rejected


class LedgerChallengeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "roles.json").write_text(json.dumps({
            "forge-core": "core", "forge-auditor": "auditor",
            "forge-executor": "executor", "forge-manager": "manager",
            "forge-ci": "ci", "ben": "human",
        }), encoding="utf-8")
        self.ledger = Ledger(self.root)
        self.serial = 0
        self.create("T1")

    def proposal(self, action, cid="T1", payload=None):
        self.serial += 1
        return {"proposal_id": f"p{self.serial}", "action": action,
                "contract_id": cid, "payload": {} if payload is None else payload}

    def apply(self, action, cid="T1", payload=None, identity="forge-core"):
        proposal = self.proposal(action, cid, payload)
        result = self.ledger.apply(proposal, identity)
        self.assertEqual(result["status"], "applied")
        return proposal

    def create(self, cid):
        return self.apply("create", cid, {
            "title": "Build the route", "spec_ref": "phase-2-design.md#4",
            "acceptance": "The route works", "files_in_scope": ["route.py"],
            "max_attempts": 6, "token_budget": 1000,
        }, "forge-manager")

    def payload(self, **overrides):
        return dict({"target": "blocked", "claimant": "builder",
                     "verdict": "overturned", "proof": "patch",
                     "route": "Use the verified alternate route",
                     "run_ids": ["challenger-1"],
                     "outcomes": ["verified_overturn"]}, **overrides)

    def challenge(self, cid="T1", **overrides):
        return self.apply("challenge", cid, self.payload(**overrides))

    def claim(self, cid="T1"):
        self.apply("claim", cid, identity="forge-executor")

    def submit(self, cid="T1"):
        self.apply("run_report", cid, {
            "run_id": f"build-{self.serial}", "claim": "done", "commit": "a" * 40,
            "changed": ["route.py"], "violations": [], "out_of_scope": [],
        })
        self.apply("submit", cid, {"commit": "a" * 40}, "forge-executor")

    def exhaust_attempts(self, cid="T1"):
        for attempt in range(6):
            self.claim(cid)
            self.submit(cid)
            self.apply("fail", cid, identity="forge-auditor")
            if attempt < 5:
                self.apply("reopen", cid, identity="forge-manager")
        self.assertEqual(self.ledger.contracts()[cid]["status"], "parked")
        self.assertEqual(self.ledger.contracts()[cid]["attempts"], 6)

    def assert_rejected(self, proposal, identity="forge-core", field=None):
        before = self.ledger.snapshot()
        with self.assertRaises(Rejected) as caught:
            self.ledger.apply(proposal, identity)
        if field is not None:
            self.assertIn(field, str(caught.exception))
        self.assertEqual(self.ledger.snapshot(), before)

    def test_challenge_preserves_every_contract_status_and_other_state(self):
        self.ledger.spec_file.parent.mkdir()
        self.ledger.spec_file.write_text("Approved specification", encoding="utf-8")
        self.apply("approve_spec", payload={"spec_hash": self.ledger.spec_hash_on_disk()},
                   identity="ben")
        for status in ("open", "claimed", "submitted", "failed", "parked", "done"):
            with self.subTest(status=status):
                cid = "T-" + status
                self.create(cid)
                if status != "open":
                    self.claim(cid)
                    self.apply("usage", cid, {"tokens": 17})
                if status in {"submitted", "failed", "done"}:
                    self.submit(cid)
                if status == "failed":
                    self.apply("fail", cid, identity="forge-auditor")
                if status == "parked":
                    self.apply("park", cid)
                if status == "done":
                    self.apply("test_run", cid, {"run_id": "ci-done", "commit": "a" * 40,
                                                  "passed": True}, "forge-ci")
                    self.apply("pass", cid, {"run_id": "ci-done"}, "forge-auditor")
                before = self.ledger.contracts()
                self.assertEqual(before[cid]["status"], status)
                runs, reports = self.ledger.test_runs(), self.ledger.reports()
                spec = self.ledger.spec_state_path.read_bytes()
                claims = self.ledger.false_claims()
                self.challenge(cid)
                self.assertEqual(self.ledger.contracts(), before)
                self.assertEqual(self.ledger.test_runs(), runs)
                self.assertEqual(self.ledger.reports(), reports)
                self.assertEqual(self.ledger.spec_state_path.read_bytes(), spec)
                self.assertEqual(self.ledger.false_claims(), claims)
                event = self.ledger.events()[-1]
                self.assertEqual(event["result_status"], status)
                self.assertEqual(event["role"], "core")
                self.assertNotIn("note", event)
        self.assertTrue(self.ledger.verify_chain())

    def test_only_core_can_record_challenges(self):
        self.challenge()
        for identity in ("forge-auditor", "forge-executor", "forge-manager", "ben"):
            with self.subTest(identity=identity):
                self.assert_rejected(self.proposal("challenge", payload=self.payload()), identity)

    def test_invalid_payload_fields_are_named_and_leave_snapshot_unchanged(self):
        self.challenge()
        invalid = {
            "target": ["other", None, []],
            "claimant": ["auditor", None, []],
            "verdict": ["pass", None, []],
            "proof": ["assertion", False, []],
            "route": ["x" * 2001, None, 123],
            "run_ids": [[], "run", None, [1], ["run"] * 11],
            "outcomes": [[], ["stands", "stands"], ["unknown"], "stands", None, [[]]],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected(self.proposal("challenge", payload=self.payload(**{field: value})),
                                         field=field)
        for field in ("target", "claimant", "verdict", "run_ids"):
            with self.subTest(missing=field):
                payload = self.payload()
                del payload[field]
                self.assert_rejected(self.proposal("challenge", payload=payload), field=field)

    def test_valid_optional_fields_enums_and_size_boundaries(self):
        outcomes = ["verified_overturn", "unverified_overturn", "stands",
                    "stands_no_evidence", "unusable", "interrupted"]
        self.challenge(route="x" * 2000, run_ids=[str(i) for i in range(10)],
                       outcomes=(outcomes * 2)[:10])
        for target in ("blocker", "dead_end", "blocked"):
            for verdict in ("overturned", "stands", "unconfirmed"):
                for proof in ("patch", "capability", None):
                    with self.subTest(target=target, verdict=verdict, proof=proof):
                        self.challenge(target=target, verdict=verdict, proof=proof,
                                       claimant="troubleshooter")
        payload = self.payload()
        for field in ("proof", "route", "outcomes"):
            del payload[field]
        self.apply("challenge", payload=payload)

    def test_unknown_contract_exception_is_only_for_capability_dead_ends(self):
        self.assert_rejected(self.proposal("challenge", "T9", self.payload()), field="no contract T9")
        self.challenge("capability:docker", target="dead_end", claimant="troubleshooter")
        self.assertIsNone(self.ledger.events()[-1]["result_status"])
        self.assertNotIn("capability:docker", self.ledger.contracts())
        for target in ("blocker", "blocked"):
            self.assert_rejected(self.proposal("challenge", "capability:docker",
                                               self.payload(target=target)))
        self.assert_rejected(self.proposal("challenge", "T9", self.payload(target="dead_end")),
                             field="no contract T9")

    def test_challenges_are_ordered_filtered_and_idempotent_without_false_claims(self):
        self.exhaust_attempts()
        before = self.ledger.false_claims()
        self.assertEqual(before, 6)
        self.create("T2")
        proposals = [self.challenge(), self.challenge("T2", verdict="stands"),
                     self.challenge("capability:docker", target="dead_end"),
                     self.challenge(verdict="unconfirmed")]
        snapshot = self.ledger.snapshot()
        result = self.ledger.apply(proposals[0], "forge-core")
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(self.ledger.snapshot(), snapshot)
        expected = [{"proposal_id": p["proposal_id"], "contract_id": p["contract_id"],
                     **p["payload"]} for p in proposals]
        self.assertEqual(self.ledger.challenges(), expected)
        self.assertEqual(self.ledger.challenges("T1"), [expected[0], expected[3]])
        self.assertEqual(self.ledger.challenges("capability:docker"), [expected[2]])
        self.assertEqual(self.ledger.challenges("missing"), [])
        self.assertEqual(self.ledger.false_claims(), before)
        self.assertEqual(self.ledger.false_claims("T1"), before)

    def test_core_rescue_adds_budget_allows_claim_and_cannot_reuse_evidence(self):
        self.exhaust_attempts()
        before = self.ledger.contracts()["T1"]
        challenge = self.challenge()
        rescue = self.apply("unpark", payload={"challenge": challenge["proposal_id"],
                                               "extra_attempts": 2})
        expected = dict(before, status="open", max_attempts=8)
        self.assertEqual(self.ledger.contracts()["T1"], expected)
        self.assertEqual(self.ledger.events()[-1]["result_status"], "open")
        self.assertEqual(self.ledger.apply(rescue, "forge-core")["status"], "duplicate")
        self.claim()
        self.assertEqual(self.ledger.contracts()["T1"]["status"], "claimed")
        self.apply("park")
        self.assert_rejected(self.proposal("unpark", payload=rescue["payload"]))
        self.assertFalse(self.ledger.reconcile())
        self.assertEqual(self.ledger.derive()["contracts"], self.ledger.contracts())

    def test_core_rescue_requires_prior_same_contract_blocked_overturn(self):
        self.apply("park")
        self.create("T2")
        valid = self.challenge()["proposal_id"]
        bad_ids = ["missing", self.challenge("T2")["proposal_id"],
                   self.challenge(target="blocker")["proposal_id"],
                   self.challenge(target="dead_end")["proposal_id"],
                   self.challenge(verdict="stands")["proposal_id"],
                   self.challenge(verdict="unconfirmed")["proposal_id"],
                   self.ledger.events()[0]["proposal_id"], None, [], 1]
        for challenge_id in bad_ids:
            with self.subTest(challenge_id=challenge_id):
                self.assert_rejected(self.proposal("unpark", payload={
                    "challenge": challenge_id, "extra_attempts": 2}))
        self.assert_rejected(self.proposal("unpark", payload={"extra_attempts": 2}))
        for extra in (0, 7, True, False, -1, 2.0, "2", None):
            with self.subTest(extra=extra):
                self.assert_rejected(self.proposal("unpark", payload={
                    "challenge": valid, "extra_attempts": extra}))
        self.assert_rejected(self.proposal("unpark", payload={"challenge": valid}))
        future = self.proposal("challenge", payload=self.payload())
        self.assert_rejected(self.proposal("unpark", payload={
            "challenge": future["proposal_id"], "extra_attempts": 2}))
        self.ledger.apply(future, "forge-core")
        self.apply("unpark", payload={"challenge": future["proposal_id"], "extra_attempts": 2})

    def test_rescue_boundaries_and_nonparked_status_refusal(self):
        for extra in (1, 6):
            cid = f"budget-{extra}"
            self.create(cid)
            self.apply("park", cid)
            evidence = self.challenge(cid)["proposal_id"]
            self.apply("unpark", cid, {"challenge": evidence, "extra_attempts": extra})
            self.assertEqual(self.ledger.contracts()[cid]["max_attempts"], extra)
            self.assertEqual(self.ledger.contracts()[cid]["status"], "open")
        evidence = self.challenge()["proposal_id"]
        payload = {"challenge": evidence, "extra_attempts": 2}
        self.assert_rejected(self.proposal("unpark", payload=payload))
        self.claim()
        self.assert_rejected(self.proposal("unpark", payload=payload))
        self.submit()
        self.assert_rejected(self.proposal("unpark", payload=payload))

    def test_human_unpark_is_unchanged_and_consumes_named_challenge(self):
        self.apply("park")
        before = self.ledger.contracts()["T1"]
        self.apply("unpark", identity="ben")
        self.assertEqual(self.ledger.contracts()["T1"], dict(before, status="open"))
        self.apply("park")
        evidence = self.challenge()["proposal_id"]
        self.apply("unpark", payload={"challenge": evidence}, identity="ben")
        self.apply("park")
        self.assert_rejected(self.proposal("unpark", payload={"challenge": evidence, "extra_attempts": 2}))

    def test_replay_rebuild_and_reconcile_preserve_challenges_and_rescue(self):
        self.exhaust_attempts()
        evidence = self.challenge()["proposal_id"]
        self.apply("unpark", payload={"challenge": evidence, "extra_attempts": 2})
        self.claim()
        self.challenge(verdict="stands")
        self.challenge("capability:docker", target="dead_end", claimant="troubleshooter")
        contracts = self.ledger.contracts()
        challenges = self.ledger.challenges()
        events = self.ledger.events_path.read_bytes()
        derived = self.ledger.derive()
        self.assertEqual(derived["contracts"], contracts)
        self.assertEqual(derived["reports"], self.ledger.reports())
        self.assertEqual(derived["test_runs"], self.ledger.test_runs())
        self.assertFalse(self.ledger.reconcile())
        self.ledger.rebuild()
        self.assertEqual(self.ledger.contracts(), contracts)
        self.ledger.contracts_path.write_text("{}", encoding="utf-8")
        self.assertTrue(self.ledger.reconcile())
        self.assertEqual(self.ledger.contracts(), contracts)
        self.assertEqual(self.ledger.challenges(), challenges)
        self.assertEqual(self.ledger.events_path.read_bytes(), events)
        self.assertTrue(self.ledger.verify_chain())
        self.assertFalse(self.ledger.reconcile())

    def test_challenge_obeys_kill_switch_and_frozen_spec(self):
        self.challenge()
        self.ledger.kill_path.write_text("stopped", encoding="utf-8")
        self.assert_rejected(self.proposal("challenge", payload=self.payload()), field="kill")
        self.ledger.kill_path.unlink()
        self.ledger.spec_file.parent.mkdir()
        self.ledger.spec_file.write_text("Approved", encoding="utf-8")
        self.apply("approve_spec", payload={"spec_hash": self.ledger.spec_hash_on_disk()}, identity="ben")
        self.ledger.spec_file.write_text("Changed", encoding="utf-8")
        self.assert_rejected(self.proposal("challenge", payload=self.payload()), field="frozen")

    def test_tampering_with_challenge_breaks_chain_and_refuses_further_events(self):
        self.challenge()
        events = self.ledger.events()
        events[-1]["payload"]["route"] = "unverified replacement"
        self.ledger.events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.assertFalse(self.ledger.verify_chain())
        self.assert_rejected(self.proposal("challenge", payload=self.payload()), field="hash chain")


if __name__ == "__main__":
    unittest.main()
