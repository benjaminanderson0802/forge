"""T2Aa: audit events validate evidence without undoing completed work."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from core import ledger as ledger_mod
from core.ledger import Ledger, Rejected


SHA = "0123456789abcdef" * 2 + "01234567"
_UNSET = object()
ROLES = {
    "forge-manager": "manager", "forge-executor": "executor",
    "forge-auditor": "auditor", "forge-core": "core",
    "forge-ci": "ci", "human": "human",
}


def finding(**changes):
    return dict(id="F1", severity="major", file="src/demo.py",
                summary="An edge case fails", evidence="Input zero raises", **changes)


def report(findings=_UNSET, **changes):
    # Omission means a clean report; explicit null must reach validation.
    items = [] if findings is _UNSET else findings
    payload = dict(kind="report", run_id="audit-run", commit=SHA,
                   verdict="findings" if items else "clean", findings=items, dropped=0)
    payload.update(changes)
    return payload


class AuditLedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="forge-audit-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
        self.led = Ledger(self.root)
        self.serial = 0
        self.make_contract("C1", "done")
        self.done = self.led.contracts()["C1"]
        self.pass_event = self.led.completion("C1")

    def proposal(self, action, payload=None, cid="C1"):
        self.serial += 1
        return dict(proposal_id=f"p-{self.serial}", action=action,
                    contract_id=cid, payload={} if payload is None else payload)

    def apply(self, action, role, payload=None, cid="C1"):
        return self.led.apply(self.proposal(action, payload, cid), f"forge-{role}")

    def make_contract(self, cid, status):
        self.apply("create", "manager", {
            "title": "Demo", "spec_ref": "spec.md#1", "acceptance": "tests pass",
            "files_in_scope": ["src/demo.py"], "max_attempts": 3, "token_budget": 1000,
        }, cid)
        if status == "open":
            return
        self.apply("claim", "executor", cid=cid)
        self.apply("usage", "executor", {"tokens": 17}, cid)
        if status == "claimed":
            return
        self.apply("submit", "executor", {"commit": SHA}, cid)
        if status == "submitted":
            return
        self.apply("test_run", "ci", {"run_id": f"ci-{cid}", "commit": SHA,
                                        "passed": True}, cid)
        self.apply("run_report", "core", {
            "run_id": f"ci-{cid}", "claim": "done", "commit": SHA,
            "changed": ["src/demo.py"], "violations": [], "out_of_scope": [],
        }, cid)
        self.apply("fail" if status == "failed" else "pass", "auditor",
                   {"run_id": f"ci-{cid}"}, cid)
        self.assertEqual(self.led.contracts()[cid]["status"], status)

    def assert_audit_applied(self, payload):
        before = self.led.contracts()
        events = self.led.events()
        result = self.apply("audit", "auditor", payload)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.led.contracts(), before)
        self.assertEqual(result["contract"], self.done)
        after = self.led.events()
        self.assertEqual(len(after), len(events) + 1)
        event = after[-1]
        self.assertEqual(event["action"], "audit")
        self.assertEqual(event["identity"], "forge-auditor")
        self.assertEqual(event["role"], "auditor")
        self.assertEqual(event["contract_id"], "C1")
        self.assertEqual(event["payload"], payload)
        self.assertEqual(event["result_status"], "done")
        self.assertEqual(event["prev"], events[-1]["hash"])
        self.assertNotEqual(event.get("note"), "false_claim")
        self.assertEqual(self.led.false_claims("C1"), 0)
        return event

    def assert_audit_rejected(self, payload, identity="forge-auditor", cid="C1"):
        before = self.led.snapshot()
        with self.assertRaises(Rejected) as caught:
            self.led.apply(self.proposal("audit", payload, cid), identity)
        self.assertEqual(self.led.snapshot(), before)
        message = str(caught.exception)
        self.assertTrue(message.strip(), "Rejections must explain the problem")
        self.assertNotIn("unknown action", message.lower(),
                         "Audit must be recognized before its input can be validated")

    def test_public_audit_interface(self):
        self.assertEqual(ledger_mod.ACTIONS.get("audit"), ({"auditor"}, {"done"}, None))
        self.assertEqual(getattr(ledger_mod, "AUDIT_SEVERITIES", None),
                         ("blocker", "major", "minor"))
        self.assertEqual(getattr(ledger_mod, "AUDIT_FINDINGS_MAX", None), 30)
        self.assertEqual(getattr(ledger_mod, "AUDIT_TEXT_MAX", None), 2000)

    def test_clean_and_findings_reports_preserve_completed_contract(self):
        for payload in (report(), report(findings=[]), report([finding()], dropped=2)):
            with self.subTest(verdict=payload["verdict"]):
                event = self.assert_audit_applied(payload)
                self.assertEqual(event["payload"]["dropped"], payload["dropped"])
        self.assertEqual(self.led.contracts()["C1"], self.done)

    def test_invalid_confirmed_and_unconfirmed_events(self):
        payloads = [{"kind": "invalid", "run_id": "audit-run", "reason": "Malformed output"}]
        for kind in ("confirmed", "unconfirmed"):
            payloads.extend([dict(kind=kind, of="T2Aa", finding="F1"),
                             dict(kind=kind, of="T2Aa", finding="F1", task="fix-1")])
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_audit_applied(payload)

    def test_valid_boundaries_and_optional_finding_fields(self):
        items = []
        for i in range(30):
            item = finding()
            item.update(id=f"F{i}", severity=("blocker", "major", "minor")[i % 3],
                        summary="s" * 2000)
            if i % 3 == 0:
                item.update(line=None, contract_ref=None)
            elif i % 3 == 1:
                item.update(line=42, contract_ref="T2Aa")
            items.append(item)
        self.assert_audit_applied(report(items))

    def test_only_auditor_can_apply_audit(self):
        for identity in ("forge-executor", "forge-core", "forge-manager", "forge-ci", "human"):
            with self.subTest(identity=identity):
                self.assert_audit_rejected(report(), identity)

    def test_audit_requires_an_existing_done_contract(self):
        for status in ("open", "claimed", "submitted", "failed"):
            cid = f"contract-{status}"
            self.make_contract(cid, status)
            with self.subTest(status=status):
                self.assert_audit_rejected(report(), cid=cid)
        self.assert_audit_rejected(report(), cid="missing")

    def test_report_rejects_missing_and_wrongly_typed_fields(self):
        bad_values = {
            "run_id": [None, "", 7, False],
            "commit": [None, "abc", SHA.upper(), "g" * 40, SHA + "0", 7],
            "verdict": [None, "unknown", "findings", []],
            "findings": [None, {}, "", [None], ["finding"]],
            "dropped": [None, -1, True, False, 1.5, "0"],
        }
        for field, values in bad_values.items():
            missing = report()
            del missing[field]
            with self.subTest(field=field, value="missing"):
                self.assert_audit_rejected(missing)
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_audit_rejected(report(**{field: value}))
        self.assert_audit_rejected(report([finding()], verdict="clean"))
        self.assert_audit_rejected(report([dict(finding(), id=f"F{i}") for i in range(31)]))

    def test_finding_rejects_missing_or_invalid_fields(self):
        for field in ("id", "severity", "file", "summary", "evidence"):
            for value in (None, "", 3, False, [], {}):
                item = finding()
                item[field] = value
                with self.subTest(field=field, value=value):
                    self.assert_audit_rejected(report([item]))
            item = finding()
            del item[field]
            with self.subTest(field=field, value="missing"):
                self.assert_audit_rejected(report([item]))
        for field, values in {"severity": ["critical"], "line": [True, False, "12", 1.5, []],
                              "contract_ref": [1, False, [], {}]}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_audit_rejected(report([dict(finding(), **{field: value})]))

    def test_other_kinds_reject_missing_or_invalid_fields(self):
        for kind in (None, "", "other", 7, [], {}):
            with self.subTest(kind=kind):
                self.assert_audit_rejected(dict(report(), kind=kind))
        payload = report()
        del payload["kind"]
        self.assert_audit_rejected(payload)
        templates = [dict(kind="invalid", run_id="run", reason="Bad output"),
                     dict(kind="confirmed", of="T2Aa", finding="F1"),
                     dict(kind="unconfirmed", of="T2Aa", finding="F1")]
        for template in templates:
            for field in set(template) - {"kind"}:
                payload = dict(template)
                del payload[field]
                with self.subTest(kind=template["kind"], field=field, value="missing"):
                    self.assert_audit_rejected(payload)
                for value in ("", None, 7, False, [], {}):
                    with self.subTest(kind=template["kind"], field=field, value=value):
                        self.assert_audit_rejected(dict(template, **{field: value}))
            if template["kind"] != "invalid":
                for value in (None, 1, False, [], {}):
                    with self.subTest(kind=template["kind"], task=value):
                        self.assert_audit_rejected(dict(template, task=value))

    def test_every_payload_string_is_capped_including_nested_values(self):
        templates = [report([finding(line=1, contract_ref="T2Aa")]),
                     dict(kind="invalid", run_id="run", reason="Bad output"),
                     dict(kind="confirmed", of="T2Aa", finding="F1", task="fix"),
                     dict(kind="unconfirmed", of="T2Aa", finding="F1", task="fix")]
        for template in templates:
            for field, value in template.items():
                if isinstance(value, str):
                    with self.subTest(kind=template["kind"], field=field):
                        self.assert_audit_rejected(dict(template, **{field: "x" * 2001}))
        for field in ("id", "file", "summary", "evidence", "contract_ref"):
            with self.subTest(finding_field=field):
                self.assert_audit_rejected(report([dict(finding(), **{field: "x" * 2001})]))
        self.assert_audit_rejected(dict(report(), extra={"nested": [{"text": "x" * 2001}]}))

    def test_audits_replay_and_preserve_completion_and_false_claims(self):
        for payload in (report(), report([finding()], dropped=2),
                        dict(kind="invalid", run_id="run", reason="Bad output"),
                        dict(kind="confirmed", of="T2Aa", finding="F1", task="fix"),
                        dict(kind="unconfirmed", of="T2Aa", finding="F1")):
            self.assert_audit_applied(payload)
        contracts = self.led.contracts()
        events = self.led.events()
        snapshot = self.led.snapshot()
        self.assertTrue(self.led.verify_chain())
        derived = self.led.derive()
        self.assertEqual(derived["contracts"], contracts)
        self.assertEqual(derived["test_runs"], self.led.test_runs())
        self.assertEqual(derived["reports"], self.led.reports())
        self.assertEqual(derived["head"], events[-1]["hash"])
        self.assertEqual(self.led.snapshot(), snapshot)
        self.assertIs(self.led.reconcile(), False)
        self.led.rebuild()
        reopened = Ledger(self.root)
        self.assertEqual(reopened.contracts(), contracts)
        self.assertEqual(reopened.events(), events)
        self.assertTrue(reopened.verify_chain())
        self.assertIs(reopened.reconcile(), False)
        self.assertEqual(reopened.completion("C1"), self.pass_event)
        self.assertEqual(reopened.false_claims("C1"), 0)
        self.assertEqual(reopened.false_claims(), 0)

    def test_duplicate_audit_proposal_does_not_append_an_event(self):
        proposal = self.proposal("audit", report([finding()], dropped=2))
        self.assertEqual(self.led.apply(proposal, "forge-auditor")["status"], "applied")
        snapshot = self.led.snapshot()
        events = self.led.events()
        result = Ledger(self.root).apply(copy.deepcopy(proposal), "forge-auditor")
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(self.led.events(), events)
        self.assertEqual(self.led.snapshot(), snapshot)


if __name__ == "__main__":
    unittest.main()
