"""R63 evidence for the test writer (2.2) and builder (2.3).

Proves real_team, validate_task, _check_blocker and _record_easy_out through
role configuration and observable pipeline outcomes.
"""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from core import bootstrap
from core.agents import ClaudeAgent, CodexAgent, FakeAgent, schema_ok
from core.ledger import Ledger


TEST_PATH = "tests/core/test_feat.py"
TEST_SOURCE = (
    "import unittest\nimport feat\n\n"
    "class FeatureTest(unittest.TestCase):\n"
    "    def test_value(self):\n"
    "        self.assertEqual(feat.VALUE, 42)\n"
)
LIMITS = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9,
          "mutation_min": 0.0}


def answer(data):
    return lambda prompt, cwd: (json.dumps(data), 0)


def good_writer(prompt, cwd):
    path = Path(cwd) / TEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEST_SOURCE, encoding="utf-8")
    return json.dumps({"files": [TEST_PATH]}), 0


def good_builder(prompt, cwd):
    (Path(cwd) / "feat.py").write_text("VALUE = 42\n", encoding="utf-8")
    return json.dumps({"status": "done"}), 0


class RoleEvidence(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="l1b-roles-"))
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.state = self.root / "state"
        for path in (self.repo, self.work, self.state):
            path.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.name", "Evidence")
        self.git(self.repo, "config", "user.email", "ben@example.com")
        (self.repo / "feat.py").write_text("VALUE = 0\n", encoding="utf-8")
        (self.repo / "README.md").write_text("Feature fixture\n", encoding="utf-8")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-qm", "seed")
        self.task = {"id": "T1", "kind": "build", "title": "Feat",
                     "section": "Make feat.VALUE 42.", "files_in_scope": ["feat.py"],
                     "test_files": [TEST_PATH],
                     "test_cmd": "python -m unittest " + TEST_PATH}
        self.mails = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                check=True)
        return result.stdout.decode("utf-8").strip()

    def conductor(self, writer=good_writer, builder=good_builder, checks=None):
        team = bootstrap.Team(
            test_writer=FakeAgent(writer, provider="codex"),
            builder=FakeAgent(builder, provider="claude"),
            reviewer=FakeAgent(answer({"verdict": "pass", "reasons": []}), provider="codex"),
            troubleshooter=FakeAgent(answer({"kind": "suggestion", "notes": "n"}), provider="claude"),
            drift_keeper=FakeAgent(answer({"status": "ok", "reasons": []}), provider="claude"),
            planner=FakeAgent(answer({"tasks": []}), provider="claude"))
        capability_checks = {"git": lambda: (True, "ok")}
        capability_checks.update(checks or {})
        c = bootstrap.Conductor(
            self.repo, self.work, self.state, team, dict(LIMITS),
            owner_email="ben@example.com", mailer=lambda s, b: self.mails.append((s, b)),
            inbox=lambda: [], gh=lambda a: (0, ""), judge_cmds=[], push=False,
            checks=capability_checks,
            probes={name: FakeAgent(lambda p, c: ("ok", 0), provider=name)
                    for name in ("claude", "codex")})
        c.init_queue("layer-1", [dict(self.task)])
        return c

    def accepted_tests(self, c):
        c.step()
        task = c._task("T1")
        self.assertEqual(task["status"], "tests_ok", task)
        self.assertTrue(task["tests_commit"])
        return task["tests_commit"]

    def assert_note(self, c, prefix):
        task = c._task("T1")
        self.assertTrue(any(n.startswith(prefix) for n in task["notes"]), task)
        self.assertNotEqual(task["status"], "done")

    def test_real_role_configuration(self):
        """real_team selects the required providers and edit permissions."""
        team = bootstrap.real_team(dict(LIMITS))
        self.assertIsInstance(team.test_writer, CodexAgent)
        self.assertEqual(team.test_writer.provider, "codex")
        self.assertEqual(team.test_writer.sandbox, "workspace-write")
        self.assertIsInstance(team.builder, ClaudeAgent)
        self.assertEqual(team.builder.provider, "claude")
        self.assertEqual(team.builder.permission_mode, "acceptEdits")

    def test_writer_commits_only_task_tests(self):
        c = self.conductor()
        sha = self.accepted_tests(c)
        self.assertEqual(self.git(c.wt, "branch", "--show-current"), "layer-1")
        self.assertEqual(self.git(c.wt, "rev-parse", "layer-1"), sha)
        self.assertEqual(self.git(c.wt, "show", "--name-only", "--format=", sha), TEST_PATH)
        self.assertIn("Write only these files: " + TEST_PATH, c.team.test_writer.prompts[0])

    def test_writer_outside_tests_is_rejected_and_cleaned(self):
        def writer(prompt, cwd):
            result = good_writer(prompt, cwd)
            (cwd / "feat.py").write_text("VALUE = 42\n", encoding="utf-8")
            return result
        c = self.conductor(writer=writer)
        head = self.git(c.wt, "rev-parse", "HEAD")
        c.step()
        task = c._task("T1")
        self.assertEqual(task["status"], "todo")
        self.assertTrue(task["notes"][-1].startswith("tests rejected: wrote outside test_files"))
        self.assertIn("feat.py", task["notes"][-1])
        self.assertEqual(self.git(c.wt, "rev-parse", "layer-1"), head)
        self.assertEqual(self.git(c.wt, "status", "--porcelain"), "")

    def rejected_writer_output(self, output):
        def writer(prompt, cwd):
            good_writer(prompt, cwd)
            return output, 0
        c = self.conductor(writer=writer)
        head = self.git(c.wt, "rev-parse", "HEAD")
        c.step()
        self.assert_note(c, "tests rejected: test writer failed")
        self.assertEqual(c._task("T1")["status"], "todo")
        self.assertFalse(c._task("T1").get("tests_commit"))
        self.assertEqual(self.git(c.wt, "rev-parse", "layer-1"), head)
        self.assertEqual(self.git(c.wt, "status", "--porcelain"), "")

    def test_writer_non_json_is_rejected(self):
        self.rejected_writer_output("not JSON")

    def test_writer_missing_files_is_rejected(self):
        self.assertTrue(schema_ok({"files": ["a"]}, bootstrap.S_TESTS))
        self.assertFalse(schema_ok({"summary": "x"}, bootstrap.S_TESTS))
        self.rejected_writer_output(json.dumps({"summary": "x"}))

    def test_test_paths_must_stay_under_tests(self):
        """validate_task rejects source paths and traversal in test_files."""
        self.assertIsNone(bootstrap.validate_task(self.task))
        for path in ("core/x.py", "tests/../x.py"):
            with self.subTest(path=path):
                task = dict(self.task, test_files=[path], test_cmd="python -m unittest " + path)
                problem = bootstrap.validate_task(task)
                self.assertIsInstance(problem, str)
                self.assertTrue(problem)

    def test_builder_completes_and_records_pass(self):
        c = self.conductor()
        self.accepted_tests(c)
        for _ in range(5):
            c.step()
            if c._task("T1")["status"] == "done":
                break
        self.assertEqual(c._task("T1")["status"], "done", c._task("T1"))
        self.assertEqual(self.git(c.wt, "show", "layer-1:feat.py"), "VALUE = 42")
        self.assertTrue(any(e["action"] == "pass" and e["contract_id"] == "T1"
                            for e in Ledger(self.state).events()))

    def test_builder_cannot_write_outside_scope(self):
        def builder(prompt, cwd):
            result = good_builder(prompt, cwd)
            (cwd / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
            return result
        c = self.conductor(builder=builder)
        sha = self.accepted_tests(c)
        c.step()
        self.assert_note(c, "out of scope: other.py")
        self.assertNotIn("other.py", self.git(c.wt, "ls-tree", "-r", "--name-only", "layer-1").splitlines())
        self.assertEqual(self.git(c.wt, "rev-parse", "layer-1"), sha)

    def test_builder_cannot_edit_accepted_tests(self):
        def builder(prompt, cwd):
            result = good_builder(prompt, cwd)
            (cwd / TEST_PATH).write_text(TEST_SOURCE.replace("42", "0"), encoding="utf-8")
            return result
        c = self.conductor(builder=builder)
        sha = self.accepted_tests(c)
        before = subprocess.check_output(["git", "show", sha + ":" + TEST_PATH], cwd=c.wt)
        c.step()
        self.assert_note(c, "touched test files")
        after = subprocess.check_output(["git", "show", "layer-1:" + TEST_PATH], cwd=c.wt)
        self.assertEqual(after, before)

    def test_builder_non_json_claim_is_rejected(self):
        self.assertEqual(bootstrap.S_BUILD["properties"]["status"]["enum"], ["done", "blocked"])
        self.assertEqual(bootstrap.S_BUILD["required"], ["status"])
        c = self.conductor(builder=lambda p, c: ("not JSON", 0))
        self.accepted_tests(c)
        c.step()
        self.assert_note(c, "builder output unusable")

    def test_blocker_without_evidence_is_logged_as_easy_out(self):
        """_check_blocker rejects missing evidence; _record_easy_out persists it."""
        c = self.conductor(builder=answer({"status": "blocked", "tried": ["one"], "error": "x"}))
        self.accepted_tests(c)
        log = self.state / "easy_outs.jsonl"
        self.assertFalse(log.exists())
        c.step()
        self.assertIn("blocker rejected: no evidence (easy out)", c._task("T1")["notes"])
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "easy_out")
        self.assertEqual(entries[0]["task"], "T1")
        self.assertNotEqual(c._task("T1")["status"], "done")

    def blocker(self, capability):
        return {"status": "blocked", "tried": ["start service", "connect to remote service"],
                "error": "daemon down", "capability": capability,
                "meanwhile": "Document the required service configuration"}

    def test_blocker_cannot_contradict_working_capability(self):
        """_check_blocker rejects a claimed outage when the check succeeds."""
        c = self.conductor(builder=answer(self.blocker("git")))
        self.accepted_tests(c)
        c.step()
        self.assert_note(c, "blocker rejected: contradicts capability map")
        self.assertNotIn("git", c._task("T1").get("needs", []))

    def test_evidenced_blocker_adds_failed_capability_to_needs(self):
        """_check_blocker accepts full evidence with a failed check and passing review."""
        c = self.conductor(builder=answer(self.blocker("docker")),
                           checks={"docker": lambda: (False, "daemon down")})
        # An unavailable service can consume an initial readiness-routing step.
        for _ in range(4):
            c.step()
            if c._task("T1")["status"] == "tests_ok":
                break
        self.assertEqual(c._task("T1")["status"], "tests_ok", c._task("T1"))
        c.step()
        self.assert_note(c, "blocker accepted: needs docker")
        self.assertIn("docker", c._task("T1")["needs"])
        self.assertTrue(any("BLOCKER CLAIM:" in p for p in c.team.reviewer.prompts))
        self.assertFalse((self.state / "easy_outs.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
