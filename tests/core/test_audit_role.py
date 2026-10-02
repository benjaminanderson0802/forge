"""T2Ab: auditor output, read-only team member, and isolated smoke checks."""
import copy
import importlib
import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

from core import bootstrap, roles
from core.agents import CodexAgent, FakeAgent, schema_ok, strict_schema


LEGACY_ROLES = (
    "test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper", "planner",
)


def finding(**changes):
    value = {"id": "a1", "severity": "major", "file": "core/x.py",
             "summary": "Missing contract check", "evidence": "The input is accepted unchecked."}
    value.update(changes)
    return value


class AuditSchemaTests(unittest.TestCase):
    def setUp(self):
        self.audit = importlib.import_module("core.audit")

    def test_schema_contract_and_optional_nullable_fields(self):
        expected = {
            "type": "object",
            "properties": {
                "id": {"type": "string"}, "severity": {"type": "string"},
                "file": {"type": "string"}, "line": {"type": ["integer", "null"]},
                "summary": {"type": "string"}, "evidence": {"type": "string"},
                "contract_ref": {"type": ["string", "null"]},
            },
            "required": ["id", "severity", "file", "summary", "evidence"],
        }
        self.assertEqual(self.audit.FINDING, expected)
        self.assertEqual(self.audit.S_AUDIT, {
            "type": "object", "properties": {
                "verdict": {"type": "string", "enum": ["clean", "findings"]},
                "findings": {"type": "array", "items": expected},
            }, "required": ["verdict", "findings"],
        })
        answer = {"verdict": "findings", "findings": [finding()]}
        self.assertTrue(schema_ok(answer, self.audit.S_AUDIT))
        answer["findings"][0].update(line=None, contract_ref=None)
        self.assertTrue(schema_ok(answer, strict_schema(self.audit.S_AUDIT)))

    def test_unknown_severity_is_left_to_plain_code_validation(self):
        self.assertNotIn("enum", self.audit.FINDING["properties"]["severity"])
        self.assertTrue(schema_ok({"verdict": "findings", "findings": [
            finding(severity="critical")]}, self.audit.S_AUDIT))
        self.assertFalse(schema_ok({"findings": []}, self.audit.S_AUDIT))


class FindingsValidationTests(unittest.TestCase):
    def setUp(self):
        self.audit = importlib.import_module("core.audit")

    def validate(self, values):
        return self.audit.validate_findings(values, lambda path: path == "core/x.py")

    def test_valid_finding_is_copied_with_exact_output_keys(self):
        source = finding(extra="discard this")
        original = copy.deepcopy(source)
        kept, dropped = self.validate([source])
        expected = finding(line=None, contract_ref=None)
        self.assertEqual((kept, dropped), ([expected], 0))
        self.assertIsNot(kept[0], source)
        self.assertEqual(source, original)
        self.assertEqual(self.audit.verdict_for(kept), "findings")

    def test_mixed_invalid_findings_are_counted_once_and_valid_ones_survive(self):
        bad = [finding(severity="critical"), finding(file="missing.py"),
               finding(summary=""), finding(evidence=" \n\t"),
               finding(file="../x.py"), "not a dict"]
        values = [finding(), *bad, finding(id="last", severity="minor")]
        kept, dropped = self.validate(values)
        self.assertEqual(dropped, 6)
        self.assertEqual([item["id"] for item in kept], ["a1", "last"])
        self.assertEqual(self.validate(bad), ([], 6))
        self.assertEqual(self.audit.verdict_for([]), "clean")
        self.assertEqual(self.validate([finding(severity="critical", file="missing.py",
                                                summary="", evidence="")]), ([], 1))

    def test_invalid_field_types_and_unsafe_paths(self):
        for key, values in {
            "severity": [None, 1, [], {}],
            "file": [None, 7, "", "/core/x.py", "C:/core/x.py",
                     "C:\\core\\x.py", "\\\\server\\core\\x.py", "core/../x.py"],
            "summary": [None, 42, " \t\n"],
            "evidence": [None, [], ""],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    # A permissive existence check must not allow unsafe paths through.
                    self.assertEqual(self.audit.validate_findings(
                        [finding(**{key: value})], lambda path: True), ([], 1))

    def test_normalized_path_is_passed_to_exists(self):
        for path in ("core\\x.py", "./core/x.py", ".\\core\\x.py"):
            with self.subTest(path=path):
                seen = []

                def exists(value):
                    seen.append(value)
                    return value == "core/x.py"

                kept, dropped = self.audit.validate_findings([finding(file=path)], exists)
                self.assertEqual(dropped, 0)
                self.assertEqual(kept[0]["file"], "core/x.py")
                self.assertEqual(seen, ["core/x.py"])

    def test_limits_and_overflow_count(self):
        self.assertEqual(self.audit.SEVERITIES, ("blocker", "major", "minor"))
        self.assertEqual(self.audit.FINDINGS_MAX, 30)
        self.assertEqual(self.audit.TEXT_MAX, 2000)
        kept, dropped = self.validate([finding(id=str(i)) for i in range(35)])
        self.assertEqual(dropped, 5)
        self.assertEqual([f["id"] for f in kept], [str(i) for i in range(30)])
        kept, dropped = self.validate([finding(id="i" * 100, summary="s" * 3000,
                                             evidence="e" * 3000, contract_ref="c" * 3000)])
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["id"], "i" * 40)
        for key, char in (("summary", "s"), ("evidence", "e"), ("contract_ref", "c")):
            self.assertEqual(kept[0][key], char * 2000)

    def test_optional_fields_and_original_position_ids(self):
        missing_id = finding(line=12, contract_ref="design section 2", severity="blocker")
        del missing_id["id"]
        kept, dropped = self.validate([None, missing_id, finding(id="", line=True, contract_ref=5)])
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0], dict(missing_id, id="f2"))
        self.assertEqual(kept[1], finding(id="f3", line=None, contract_ref=None))
        for line in (False, "12", 1.5, None):
            with self.subTest(line=line):
                self.assertIsNone(self.validate([finding(line=line)])[0][0]["line"])

    def test_non_list_input_is_empty_without_consulting_exists(self):
        def unexpected(path):
            self.fail("Non-list input must not call exists")

        for value in (None, {}, "findings", 5, (finding(),)):
            with self.subTest(value=value):
                self.assertEqual(self.audit.validate_findings(value, unexpected), ([], 0))


class AuditorRoleAndTeamTests(unittest.TestCase):
    def test_extra_role_does_not_extend_legacy_roles_or_defaults(self):
        self.assertEqual(roles.ROLE_NAMES, LEGACY_ROLES)
        self.assertEqual(set(roles.DEFAULTS), set(LEGACY_ROLES))
        self.assertEqual(roles.EXTRA_ROLES, ("auditor",))
        self.assertEqual(set(roles.EXTRA_DEFAULTS), {"auditor"})
        default = roles.EXTRA_DEFAULTS["auditor"]
        self.assertIn("AUDITOR (read-only)", default)
        self.assertIn("You change nothing", default)
        self.assertIn("Answer with JSON matching the schema given in the prompt.", default)

    def test_role_file_override_fallback_and_unknown_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertEqual(roles.role_text(repo, "auditor"), roles.EXTRA_DEFAULTS["auditor"])
            (repo / "agents").mkdir()
            (repo / "agents/auditor.md").write_bytes(b"Custom audit instructions.\r\nRead only.\r\n")
            self.assertEqual(roles.role_text(repo, "auditor"), "Custom audit instructions.\nRead only.")
            with self.assertRaises(ValueError):
                roles.role_text(repo, "nope")

    def test_shipped_auditor_instructions_cover_the_contract(self):
        path = Path(__file__).resolve().parents[2] / "agents/auditor.md"
        text = path.read_text(encoding="utf-8").lower()
        for term in ("read-only", "merged task", "ledger", "contract", "section", "plan", "design",
                     "evidence", "file", "line", "blocker", "major", "minor", "style", "clean",
                     "json", "schema"):
            with self.subTest(term=term):
                self.assertIn(term, text)
        self.assertRegex(text, r"change(?:s)? nothing|(?:must not|never|do not) (?:edit|change)")

    def test_six_member_constructor_and_real_auditor(self):
        members = [object() for _ in LEGACY_ROLES]
        team = bootstrap.Team(*members)
        self.assertEqual(tuple(f.name for f in fields(bootstrap.Team)), LEGACY_ROLES)
        self.assertEqual(vars(team), dict(zip(LEGACY_ROLES, members)))
        self.assertIsNone(team.auditor)
        self.assertNotIn("auditor", vars(team))
        for limits in ({}, {"agent_timeout_s": 123}):
            with self.subTest(limits=limits):
                auditor = bootstrap.real_team(limits).auditor
                self.assertIsInstance(auditor, CodexAgent)
                self.assertEqual(auditor.sandbox, "read-only")
                self.assertEqual(auditor.provider, "codex")
                self.assertEqual(auditor.timeout_s, limits.get("agent_timeout_s", 1800))


class AuditorSmokeTests(unittest.TestCase):
    def make_team(self, auditor=True, mode="clean"):
        self.calls = []

        def agent(role):
            def run(prompt, cwd):
                self.assertTrue((cwd / ".git").is_dir())
                self.assertEqual({p.name for p in cwd.iterdir()}, {".git", "README.md"})
                self.calls.append((role, cwd))
                if role == "auditor":
                    if mode == "write":
                        (cwd / "unauthorized.txt").write_text("changed", encoding="utf-8")
                    return ("{}" if mode == "bad" else '{"verdict":"clean","findings":[]}', 1)
                _, writes, example = bootstrap.SMOKE_ROLES[role]
                if writes:
                    (cwd / "smoke.txt").write_text("ok", encoding="utf-8")
                return example, 1
            return FakeAgent(run)

        team = bootstrap.Team(*(agent(role) for role in LEGACY_ROLES))
        if auditor:
            team.auditor = agent("auditor")
        return team

    def run_smoke(self, team):
        with tempfile.TemporaryDirectory() as tmp:
            return bootstrap.smoke(team, Path(tmp))

    def test_registered_schema_and_read_only_example(self):
        audit = importlib.import_module("core.audit")
        schema, writes, example = bootstrap.SMOKE_ROLES["auditor"]
        self.assertEqual(schema, audit.S_AUDIT)
        self.assertEqual(bootstrap.S_AUDIT, audit.S_AUDIT)
        self.assertIs(writes, False)
        self.assertEqual(json.loads(example), {"verdict": "clean", "findings": []})

    def test_clean_auditor_called_once_in_own_fresh_repository(self):
        team = self.make_team()
        self.assertEqual(self.run_smoke(team), [])
        self.assertCountEqual([role for role, _ in self.calls], [*LEGACY_ROLES, "auditor"])
        self.assertEqual(len(team.auditor.prompts), 1)
        self.assertEqual(len({cwd for _, cwd in self.calls}), 7)

    def test_writing_auditor_is_reported(self):
        problems = self.run_smoke(self.make_team(mode="write"))
        self.assertTrue(any("auditor" in p and "read-only" in p for p in problems), problems)

    def test_invalid_auditor_answer_is_reported(self):
        problems = self.run_smoke(self.make_team(mode="bad"))
        self.assertTrue(any("auditor" in p for p in problems), problems)

    def test_six_member_team_skips_absent_auditor(self):
        for missing_attribute in (False, True):
            with self.subTest(missing_attribute=missing_attribute):
                team = self.make_team(auditor=False)
                if missing_attribute:
                    team = SimpleNamespace(**vars(team))
                self.assertEqual(self.run_smoke(team), [])
                self.assertCountEqual([role for role, _ in self.calls], LEGACY_ROLES)
                self.assertEqual(len(self.calls), 6)


if __name__ == "__main__":
    unittest.main()
