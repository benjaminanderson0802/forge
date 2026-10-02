"""T2Ca: ledger-backed agent runs and pure role scores."""
import copy
import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from core.ledger import ACTIONS, Ledger, Rejected


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
ROLE_COUNTS = {
    "builder": ("false_claims", "easy_outs", "overturned", "escaped_defects"),
    "troubleshooter": ("overturned_dead_ends", "overturned_blocked"),
    "reviewer": ("missed_defects",),
    "auditor": ("unconfirmed_findings", "dropped_findings", "invalid_runs"),
    "challenger": ("unverified_overturns", "stands_without_evidence"),
}
ROLES = {"forge-core": "core", "forge-executor": "executor",
         "forge-auditor": "auditor", "forge-manager": "manager",
         "ci": "ci", "human": "human"}


def event(action, payload=None, **fields):
    return dict(action=action, payload={} if payload is None else payload, **fields)


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="t2ca-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        (self.folder / "roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
        self.led = Ledger(self.folder)
        self.serial = 0

    def apply(self, action, payload=None, identity="forge-core", cid="T1", pid=None):
        self.serial += 1
        return self.led.apply({"proposal_id": pid or f"p{self.serial}",
                               "action": action, "contract_id": cid,
                               "payload": {} if payload is None else payload}, identity)

    def create(self):
        self.apply("create", {"title": "score fixture", "spec_ref": "spec.md#1",
                              "acceptance": "python -m unittest", "files_in_scope": ["demo.py"],
                              "max_attempts": 5, "token_budget": 1000}, "forge-manager")

    def report(self, **extra):
        self.apply("run_report", dict(run_id="build-1", changed=[], violations=[],
                                      out_of_scope=[], **extra))

    def failed_attempt(self):
        self.create()
        self.apply("claim", identity="forge-executor")
        self.report(claim="done")
        self.apply("submit", {"commit": "a" * 40}, "forge-executor")
        self.apply("fail", identity="forge-auditor")

    def record_run(self, role="builder", **kwargs):
        return self.apply("agent_run", {"role": role, "run_id": f"run-{self.serial}",
                                        "at": NOW.isoformat()}, **kwargs)

    def state(self):
        return (self.led.contracts(), self.led.test_runs(), self.led.reports(),
                self.led.approved_spec())


class AgentRunTests(LedgerFixture):
    def test_core_records_without_contract_and_duplicate_is_idempotent(self):
        before = self.state()
        result = self.record_run(cid="run-without-contract", pid="agent-1")
        self.assertEqual(result["status"], "applied")
        self.assertIsNone(result["note"])
        self.assertEqual(self.state(), before)
        recorded = self.led.events()[-1]
        self.assertEqual(recorded["action"], "agent_run")
        self.assertEqual(recorded["payload"]["role"], "builder")
        self.assertIsNone(recorded["result_status"])
        self.assertNotIn("note", recorded)
        snapshot = self.led.snapshot()
        self.assertEqual(self.record_run(cid="run-without-contract", pid="agent-1")["status"],
                         "duplicate")
        self.assertEqual(self.led.snapshot(), snapshot)
        self.assertTrue(self.led.verify_chain())
        self.assertEqual(ACTIONS["agent_run"], ({"core"}, None, None))
        self.assertNotIn("audit", ACTIONS)
        self.assertNotIn("challenge", ACTIONS)

    def test_only_core_is_authorized(self):
        self.record_run(cid="seed")
        for identity in ROLES:
            if identity == "forge-core":
                continue
            with self.subTest(identity=identity):
                before = self.led.snapshot()
                with self.assertRaises(Rejected):
                    self.record_run(identity=identity, cid="not-a-contract")
                self.assertEqual(self.led.snapshot(), before)

    def test_payload_validation_names_field_and_preserves_snapshot(self):
        self.record_run(cid="seed")
        valid = {"role": "builder", "run_id": "build-1"}
        cases = [("role", None), ("role", ""), ("role", 1), ("role", "r" * 61),
                 ("run_id", None), ("run_id", ""), ("run_id", 12),
                 ("run_id", "r" * 201), ("at", None), ("at", 42), ("at", "x" * 65)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = dict(valid, **{field: value})
                before = self.led.snapshot()
                with self.assertRaisesRegex(Rejected, field):
                    self.apply("agent_run", payload, cid="not-a-contract")
                self.assertEqual(self.led.snapshot(), before)
        for field in ("role", "run_id"):
            payload = dict(valid)
            del payload[field]
            before = self.led.snapshot()
            with self.assertRaisesRegex(Rejected, field):
                self.apply("agent_run", payload, cid="missing-field")
            self.assertEqual(self.led.snapshot(), before)

    def test_limits_are_inclusive_and_at_is_optional_text(self):
        for extra in ({}, {"at": ""}, {"at": "x" * 64}):
            with self.subTest(extra=extra):
                result = self.apply("agent_run", dict(role="r" * 60, run_id="i" * 200,
                                                       **extra), cid="no-contract")
                self.assertEqual(result["status"], "applied")

    def test_mixed_log_replays_and_preserves_false_claims_and_completion(self):
        self.failed_attempt()
        before = self.state()
        self.assertEqual(self.led.false_claims(), 1)
        self.record_run(cid="T1")
        self.record_run("reviewer", cid="review-run")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.led.false_claims(), 1)
        self.assertEqual(self.led.false_claims("T1"), 1)
        self.assertEqual(self.led.false_claims("review-run"), 0)
        self.assertIsNone(self.led.completion("T1"))
        self.assertIsNone(self.led.completion("review-run"))
        self.assertTrue(self.led.verify_chain())
        self.assertFalse(self.led.reconcile())
        derived = self.led.derive()
        self.assertEqual(derived["contracts"], before[0])
        self.assertEqual(derived["test_runs"], before[1])
        self.assertEqual(derived["reports"], before[2])
        snapshot = self.led.snapshot()
        self.led.rebuild()
        self.assertEqual(self.led.snapshot(), snapshot)
        self.apply("reopen", identity="forge-manager")
        self.apply("claim", identity="forge-executor")
        self.report(claim="done")
        self.apply("submit", {"commit": "b" * 40}, "forge-executor")
        self.apply("test_run", {"run_id": "ci-1", "commit": "b" * 40, "passed": True}, "ci")
        self.apply("pass", {"run_id": "ci-1"}, "forge-auditor")
        completion = self.led.completion("T1")
        before = self.state()
        self.record_run("reviewer", cid="T1")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.led.completion("T1"), completion)
        self.assertEqual(self.led.false_claims(), 1)
        self.assertFalse(self.led.reconcile())


class ScoresTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.scores = importlib.import_module("core.scores")

    def score(self, events):
        return self.scores.score_events(events, NOW)

    def test_empty_scores_have_complete_shape(self):
        self.assertEqual(self.scores.ROLE_COUNTS, ROLE_COUNTS)
        self.assertEqual(self.scores.WINDOWS, {"last_7_days": 168, "last_24_hours": 24})
        self.assertEqual(self.scores.SCORES_FILE, "scores.json")
        roles = {role: {"runs": 0, "counts": dict.fromkeys(names, 0),
                        "rates": dict.fromkeys(names)} for role, names in ROLE_COUNTS.items()}
        expected = {"generated_at": NOW.isoformat(),
                    **{window: {"roles": roles} for window in
                       ("total", "last_7_days", "last_24_hours")}}
        self.assertEqual(self.score([]), expected)
        self.assertEqual(self.scores.scores(ledger=self.led, now=NOW), expected)

    def test_real_ledger_denominators_verdicts_and_state_entry_point(self):
        self.failed_attempt()
        self.apply("reopen", identity="forge-manager")
        self.apply("claim", identity="forge-executor")
        self.report(easy_out={"reason": "x", "capability": None})
        self.record_run(cid="builder-1")
        self.record_run(cid="builder-2")
        self.record_run("reviewer", cid="reviewer-1")
        # Invalid sidecars must never be consulted: the ledger is the sole input.
        for name in ("easy_outs.jsonl", "challenges.jsonl", "audits.jsonl"):
            (self.folder / name).write_text("not JSON\n", encoding="utf-8")
        snapshot = self.led.snapshot()
        data = self.scores.scores(ledger=self.led, now=NOW)
        builder = data["total"]["roles"]["builder"]
        self.assertEqual(builder, {"runs": 2,
                                  "counts": {"false_claims": 1, "easy_outs": 1,
                                             "overturned": 0, "escaped_defects": 0},
                                  "rates": {"false_claims": 0.5, "easy_outs": 0.5,
                                            "overturned": 0.0, "escaped_defects": 0.0}})
        self.assertEqual(data["total"]["roles"]["reviewer"]["runs"], 1)
        self.assertEqual(self.scores.scores(state=self.folder, now=NOW), data)
        before = datetime.now(timezone.utc)
        default = self.scores.scores(state=self.folder)
        after = datetime.now(timezone.utc)
        self.assertEqual(default["total"], data["total"])
        self.assertLessEqual(before, self.scores.parse_at(default["generated_at"]))
        self.assertLessEqual(self.scores.parse_at(default["generated_at"]), after)
        self.assertEqual(self.led.snapshot(), snapshot)

    def test_audit_challenge_counts_and_no_inferred_runs(self):
        events = [
            event("agent_run", {"role": "auditor", "run_id": "20261002T080000-auditor-abc123"}),
            event("audit", {"kind": "report", "run_id": "audit-T1-1a2b3c4d", "dropped": 2}),
            *[event("audit", {"kind": "report", "dropped": value})
              for value in (True, -1, 1.5, "3", None)],
            event("audit", {"kind": "invalid"}),
            event("audit", {"kind": "unconfirmed", "of": "T1", "finding": "U1"}),
            event("audit", {"kind": "unconfirmed", "of": "T1", "finding": "U1"}),
            event("audit", {"kind": "confirmed", "of": "T1", "finding": "F1"}),
            event("audit", {"kind": "confirmed", "of": "T1", "finding": "F1"}),
            event("audit", {"kind": "confirmed", "of": "T2", "finding": "F1"}),
            event("audit", {"kind": "confirmed", "of": "T1", "finding": "F2"}),
            event("pass", contract_id="T1"),
            *[event("challenge", {"target": target, "verdict": verdict,
                                   "run_ids": ["challenge-1", "challenge-2"]})
              for target in ("blocker", "dead_end", "blocked")
              for verdict in ("overturned", "stands", "unconfirmed")],
            event("challenge", {"outcomes": ["unverified_overturn", "stands_no_evidence", "stands"]}),
            event("challenge", {"outcomes": "unverified_overturn"}),
            event("challenge", {"outcomes": {"stands_no_evidence": True}}),
            event("run_report", {"easy_out": {}}),
            event("run_report", {"easy_out": []}),
            event("unrelated", note="false_claim"),
            "bad event", {"action": "audit", "payload": []},
        ]
        for i, item in enumerate(events):
            if isinstance(item, dict):
                item["proposal_id"] = f"distinct-{i}"
        untouched = copy.deepcopy(events)
        roles = self.score(events)["total"]["roles"]
        self.assertEqual(roles["builder"]["counts"],
                         {"false_claims": 1, "easy_outs": 1, "overturned": 1, "escaped_defects": 3})
        self.assertEqual(roles["troubleshooter"]["counts"],
                         {"overturned_dead_ends": 1, "overturned_blocked": 1})
        self.assertEqual(roles["reviewer"]["counts"], {"missed_defects": 2})
        self.assertEqual(roles["auditor"]["counts"],
                         {"unconfirmed_findings": 1, "dropped_findings": 2, "invalid_runs": 1})
        self.assertEqual(roles["challenger"]["counts"],
                         {"unverified_overturns": 1, "stands_without_evidence": 1})
        for role, row in roles.items():
            self.assertEqual(row["runs"], int(role == "auditor"))
            if role != "auditor":
                self.assertTrue(all(rate is None for rate in row["rates"].values()))
        self.assertEqual(events, untouched)

    def test_malformed_identifiers_are_skipped_before_deduplication(self):
        valid = [event("audit", {"kind": "confirmed", "of": "T1", "finding": "F1"}),
                 event("audit", {"kind": "unconfirmed", "of": "T1", "finding": "U1"}),
                 event("pass", contract_id="T1")]
        bad = [None, 12, "bad", {}, {"payload": []}, {"payload": None}]
        for kind in ("confirmed", "unconfirmed"):
            for field in ("of", "finding"):
                for value in ([], ["T1"], {}, {"id": "T1"}, None, 42, True, ""):
                    bad.append(event("audit", {"kind": kind, "of": "T1", "finding": "F1"}))
                    bad[-1]["payload"][field] = value
                payload = {"kind": kind, "of": "T1", "finding": "F1"}
                del payload[field]
                bad.append(event("audit", payload))
        bad.extend(event("agent_run", {"role": value}) for value in ([], {}, None, 1, ""))
        bad.extend(event("pass", contract_id=value) for value in ([], {}, None))
        self.assertEqual(self.score(valid + bad), self.score(valid))

    def test_windows_boundaries_missing_timestamps_and_unknown_roles(self):
        stamps = [(NOW - timedelta(days=6)).isoformat(),
                  (NOW - timedelta(days=8)).isoformat(),
                  (NOW - timedelta(hours=23)).isoformat(),
                  (NOW - timedelta(hours=25)).isoformat(), None, "garbage",
                  NOW.replace(tzinfo=None).isoformat(), NOW.isoformat().replace("+00:00", "Z"),
                  (NOW - timedelta(days=7)).isoformat(),
                  (NOW - timedelta(hours=24)).isoformat(),
                  (NOW - timedelta(days=7, microseconds=1)).isoformat(),
                  (NOW - timedelta(hours=24, microseconds=1)).isoformat()]
        events = []
        for stamp in stamps:
            payload = {"role": "builder"}
            if stamp is not None:
                payload["at"] = stamp
            events.extend([event("agent_run", payload),
                           event("run_report", dict(payload, easy_out={}))])
        events.extend([event("agent_run", {"role": "z-custom", "at": NOW.isoformat()}),
                       event("agent_run", {"role": "a-custom", "at": NOW.isoformat()})])
        data = self.score(events)
        for window, expected in (("total", 12), ("last_7_days", 8), ("last_24_hours", 4)):
            with self.subTest(window=window):
                builder = data[window]["roles"]["builder"]
                self.assertEqual(builder["runs"], expected)
                self.assertEqual(builder["counts"]["easy_outs"], expected)
                self.assertEqual(builder["rates"]["easy_outs"], 1.0)
                self.assertEqual(data[window]["roles"]["z-custom"],
                                 {"runs": 1, "counts": {}, "rates": {}})
        self.assertEqual(self.scores.digest_lines(data)[-2:],
                         ["- a-custom: 1 runs", "- z-custom: 1 runs"])

    def test_deduplication_is_per_window_and_pass_lookup_is_unwindowed(self):
        old = (NOW - timedelta(days=8)).isoformat()
        recent = NOW.isoformat()
        events = [event("pass", {"at": old}, contract_id="T1")]
        for stamp in (old, recent, recent):
            for kind in ("confirmed", "unconfirmed"):
                events.append(event("audit", {"kind": kind, "of": "T1", "finding": "F1", "at": stamp}))
        data = self.score(events)
        for window in ("total", "last_7_days", "last_24_hours"):
            roles = data[window]["roles"]
            self.assertEqual(roles["builder"]["counts"]["escaped_defects"], 1)
            self.assertEqual(roles["reviewer"]["counts"]["missed_defects"], 1)
            self.assertEqual(roles["auditor"]["counts"]["unconfirmed_findings"], 1)

    def test_parse_at_and_rate_rounding(self):
        for value in (None, 1, True, [], {}, "", "not-a-date", "2026-99-99"):
            with self.subTest(value=value):
                self.assertIsNone(self.scores.parse_at(value))
        for value in ("2026-10-02T12:00:00Z", "2026-10-02T12:00:00",
                      "2026-10-02T14:00:00+02:00"):
            parsed = self.scores.parse_at(value)
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(parsed, NOW)
        events = [event("agent_run", {"role": "builder"}) for _ in range(3)]
        events.append(event("fail", note="false_claim"))
        self.assertEqual(self.score(events)["total"]["roles"]["builder"]["rates"]["false_claims"],
                         0.3333)

    def test_write_scores_atomic_utf8_lf_and_replace(self):
        folder = self.folder / "new" / "state"
        target = folder / "scores.json"
        data = self.score([event("agent_run", {"role": "caf\u00e9"})])
        real_replace, real_fsync = os.replace, os.fsync
        synced = []
        replacements = []

        def fsync(fd):
            synced.append(fd)
            return real_fsync(fd)

        def replace(src, dst):
            self.assertEqual(Path(src).parent.resolve(), folder.resolve())
            self.assertEqual(Path(dst).resolve(), target.resolve())
            self.assertNotEqual(Path(src).resolve(), target.resolve())
            self.assertTrue(synced, "file must be flushed and fsynced before replacement")
            self.assertEqual(json.loads(Path(src).read_text(encoding="utf-8")), data)
            replacements.append((src, dst))
            return real_replace(src, dst)

        with mock.patch("os.fsync", side_effect=fsync), mock.patch("os.replace", side_effect=replace):
            self.assertEqual(self.scores.write_scores(folder, data), target)
        self.assertEqual(len(replacements), 1)
        raw = target.read_bytes()
        self.assertNotIn(b"\r", raw)
        rendered = raw.decode("utf-8").rstrip("\n")
        self.assertIn(rendered, [json.dumps(data, indent=2, sort_keys=True, ensure_ascii=ascii_only)
                                 for ascii_only in (True, False)])
        self.assertEqual(set(folder.iterdir()), {target})
        updated = self.score([])
        self.assertEqual(self.scores.write_scores(folder, updated), target)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), updated)
        self.assertEqual(set(folder.iterdir()), {target})

    def test_record_alarm_and_digest_exact_text(self):
        events = [event("agent_run", {"role": "builder", "at": NOW.isoformat()}) for _ in range(4)]
        events += [event("fail", {"at": NOW.isoformat()}, note="false_claim") for _ in range(2)]
        events += [event("run_report", {"at": NOW.isoformat(), "easy_out": {}})]
        data = self.score(events)
        self.assertEqual(self.scores.record_line(data, "builder"),
                         "YOUR RECORD (last 7 days): false claims 2, easy-outs 1, overturned 0")
        self.assertEqual(self.scores.alarm_counts(data),
                         {"builder": 3, "troubleshooter": 0, "reviewer": 0, "auditor": 0, "challenger": 0})
        self.assertEqual(self.scores.digest_lines(data), ["Scores (last 7 days):",
                         "- builder: 4 runs; false claims 2 (0.50 per run); easy outs 1 (0.25 per run); "
                         "overturned 0 (0.00 per run); escaped defects 0 (0.00 per run)"])
        verdicts = [event("challenge", {"target": target, "verdict": "overturned", "at": NOW.isoformat()})
                    for target in ("dead_end", "blocked")]
        data = self.score(verdicts)
        self.assertEqual(self.scores.record_line(data, "troubleshooter"),
                         "YOUR RECORD (last 7 days): false claims 0, easy-outs 0, overturned 2")
        self.assertEqual(self.scores.digest_lines(data), ["Scores (last 7 days):",
                         "- troubleshooter: 0 runs; overturned dead ends 1; overturned blocked 1"])
        # Each formatter must use its specified window rather than total.
        data["total"]["roles"]["builder"]["counts"]["false_claims"] = 99
        data["last_7_days"]["roles"]["builder"]["counts"]["false_claims"] = 7
        self.assertEqual(self.scores.alarm_counts(data)["builder"], 0)
        self.assertIn("false claims 7,", self.scores.record_line(data, "builder"))

    def test_formatters_tolerate_malformed_data_and_missing_roles(self):
        empty_line = "Scores (last 7 days): no agent runs or verdicts recorded yet."
        zero_record = "YOUR RECORD (last 7 days): false claims 0, easy-outs 0, overturned 0"
        malformed = [None, [], "bad", 3, {}, {"last_7_days": None, "last_24_hours": []},
                     {"last_7_days": {"roles": []}, "last_24_hours": {"roles": "bad"}}]
        for data in malformed:
            with self.subTest(data=data):
                self.assertEqual(self.scores.record_line(data, "builder"), zero_record)
                self.assertEqual(self.scores.alarm_counts(data), {})
                self.assertEqual(self.scores.digest_lines(data), [empty_line])
        for row in (None, [], "bad", {"runs": [], "counts": [], "rates": []},
                    {"counts": {"false_claims": {}, "easy_outs": [], "overturned": None}}):
            data = {window: {"roles": {"builder": row}} for window in ("last_7_days", "last_24_hours")}
            self.assertEqual(self.scores.record_line(data, "builder"), zero_record)
            self.assertEqual(self.scores.digest_lines(data), [empty_line])
            self.assertIsInstance(self.scores.alarm_counts(data), dict)
        self.assertEqual(self.scores.record_line(self.score([]), "unknown"), zero_record)
        self.assertEqual(self.scores.digest_lines(self.score([])), [empty_line])

    def test_digest_uses_role_order_independent_of_event_order(self):
        events = [event("agent_run", {"role": role, "at": NOW.isoformat()})
                  for role in ("z-extra", "challenger", "auditor", "reviewer",
                               "a-extra", "troubleshooter", "builder")]
        lines = self.scores.digest_lines(self.score(events))
        self.assertEqual(lines[0], "Scores (last 7 days):")
        self.assertEqual([line.split(":", 1)[0] for line in lines[1:]],
                         [f"- {role}" for role in (*ROLE_COUNTS, "a-extra", "z-extra")])


if __name__ == "__main__":
    unittest.main()
