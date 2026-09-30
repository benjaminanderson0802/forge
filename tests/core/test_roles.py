"""T1B3a: role instructions live in protected files agents/<role>.md, read from the main checkout."""
import json
import tempfile
import unittest
from pathlib import Path

from core import protect
from core.roles import DEFAULTS, ROLE_NAMES, role_text
from tests.core.test_bootstrap import Harness, git

FORGE = Path(__file__).resolve().parents[2]
FIRST = {
    "test_writer": "You are the TEST WRITER.",
    "builder": "You are the BUILDER.",
    "reviewer": "You are the REVIEWER (read-only).",
    "troubleshooter": "You are the TROUBLESHOOTER.",
    "drift_keeper": "You are the DRIFT KEEPER (read-only).",
    "planner": "You are the PLANNER.",
}


class RoleFilesTests(unittest.TestCase):
    def test_role_names_and_defaults(self):
        self.assertEqual(ROLE_NAMES, ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper",
                                      "planner"))
        self.assertEqual(set(DEFAULTS), set(ROLE_NAMES))
        for role, first in FIRST.items():
            with self.subTest(role=role):
                self.assertTrue(DEFAULTS[role].startswith(first), DEFAULTS[role])

    def test_each_role_file_exists_with_the_default_first_sentence(self):
        for role, first in FIRST.items():
            with self.subTest(role=role):
                p = FORGE / "agents" / f"{role}.md"
                self.assertTrue(p.is_file(), p)
                raw = p.read_bytes()
                self.assertTrue(raw.strip())
                self.assertNotIn(b"\r", raw, "LF line endings only")
                text = raw.decode("utf-8")
                self.assertLess(len(text), 3000)
                self.assertEqual(text.splitlines()[0], first)
                self.assertTrue(DEFAULTS[role].startswith(text.splitlines()[0]))

    def test_role_files_carry_the_standing_rules(self):
        text = {r: (FORGE / "agents" / f"{r}.md").read_text(encoding="utf-8").lower() for r in ROLE_NAMES}
        self.assertIn("files_in_scope", text["builder"])
        self.assertIn("tried", text["builder"])  # D-031 blocker evidence
        self.assertIn("error", text["builder"])
        self.assertIn("2", text["builder"])
        self.assertIn("json", text["builder"])
        for r in ("reviewer", "drift_keeper"):
            self.assertIn("read-only", text[r])
        self.assertIn("scratch", text["troubleshooter"])
        self.assertIn("plan file", text["planner"])
        self.assertIn("test file", text["test_writer"])
        for r in ROLE_NAMES:
            with self.subTest(role=r):
                self.assertIn("json", text[r])


class RoleTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "agents").mkdir()
        self.path = self.repo / "agents" / "builder.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_text_is_returned_with_trailing_whitespace_stripped(self):
        self.path.write_bytes("You are the BUILDER.\nCustom rule é.\n\n  \n".encode("utf-8"))
        self.assertEqual(role_text(self.repo, "builder"), "You are the BUILDER.\nCustom rule é.")

    def test_windows_line_endings_are_normalized(self):
        """Integration fix C: on Windows a role file written in text mode (or checked out with
        core.autocrlf=true) has CRLF line endings; its text must read exactly like the LF file."""
        self.path.write_bytes(b"You are the BUILDER.\r\nRule one.\r\nOld Mac line.\rEnd.\r\n\r\n")
        self.assertEqual(role_text(self.repo, "builder"), "You are the BUILDER.\nRule one.\nOld Mac line.\nEnd.")

    def test_defaults_for_every_file_problem(self):
        cases = {
            "missing": lambda: None,
            "empty": lambda: self.path.write_bytes(b""),
            "whitespace": lambda: self.path.write_bytes(b"  \n\t\n"),
            "invalid utf-8": lambda: self.path.write_bytes(b"You are \xff\xfe broken"),
            "directory": lambda: self.path.mkdir(),
            "too long": lambda: self.path.write_bytes(b"x" * 20001),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                if self.path.is_dir():
                    self.path.rmdir()
                elif self.path.exists():
                    self.path.unlink()
                make()
                self.assertEqual(role_text(self.repo, "builder"), DEFAULTS["builder"])

    def test_exactly_20000_characters_is_accepted(self):
        self.path.write_bytes(b"y" * 20000)
        self.assertEqual(role_text(self.repo, "builder"), "y" * 20000)

    def test_missing_agents_folder_gives_defaults(self):
        empty = self.repo / "nothing-here"
        for role in ROLE_NAMES:
            self.assertEqual(role_text(empty, role), DEFAULTS[role])

    def test_unreadable_file_gives_default(self):
        from unittest.mock import patch
        self.path.write_text("You are the BUILDER.\nx\n", encoding="utf-8")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertEqual(role_text(self.repo, "builder"), DEFAULTS["builder"])

    def test_unknown_role_raises(self):
        for bad in ("manager", "", "BUILDER", "../builder"):
            with self.subTest(role=bad), self.assertRaises(ValueError):
                role_text(self.repo, bad)


class ProtectionTests(unittest.TestCase):
    def test_agents_folder_is_protected(self):
        self.assertEqual(protect.violations(["agents/builder.md", "src/x.py"]), ["agents/builder.md"])
        self.assertEqual(protect.violations(["agents/sub/x.md"]), ["agents/sub/x.md"])
        self.assertEqual(protect.violations(["agents/builder.md"], labels=["human-approved"]), [])

    def test_existing_protected_paths_stay_protected(self):
        for p in ["core/bootstrap.py", "drills/run_drills.py", "tests/acceptance/a.py", "charter/limits.json",
                  "spec/spec.md", ".github/workflows/ci.yml", "roles.json", "CODEOWNERS", "docs/PURPOSE.md",
                  "docs/DECISIONS.md"]:
            with self.subTest(path=p):
                self.assertEqual(protect.violations([p]), [p])
        for pat in ["core/*", "core/**", "drills/*", "drills/**", "tests/acceptance/*", "tests/acceptance/**",
                    "charter/*", "charter/**", "spec/*", "spec/**", ".github/*", ".github/**", "roles.json",
                    "CODEOWNERS", "docs/PURPOSE.md", "docs/DECISIONS.md", "agents/*", "agents/**"]:
            self.assertIn(pat, protect.PROTECTED)

    def test_codeowners_names_agents(self):
        lines = (FORGE / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
        self.assertIn("/agents/ @benjaminanderson0802", lines)
        self.assertIn("/core/ @benjaminanderson0802", lines)


class ConductorRolePromptTests(Harness):
    """Prompts start with role_text(repo, role) + a blank line; the layer worktree's agents/ is never read."""

    def plant(self, where: Path, role: str, marker: str):
        (where / "agents").mkdir(parents=True, exist_ok=True)
        (where / "agents" / f"{role}.md").write_text(f"{marker}\nRules for {role}.\n", encoding="utf-8")

    def layer_plant(self, role, marker):
        self.plant(self.work / self.layer, role, marker)

    def all_prompts(self):
        return [p for a in vars(self.team).values() for p in a.prompts]

    def run_to_done(self, reviewer=None):
        agents = {"test_writer": self.write_tests, "builder": self.build_feature}
        if reviewer:
            agents["reviewer"] = reviewer
        c = self.init(agents=agents)
        return c

    def test_repo_role_files_start_each_prompt(self):
        markers = {r: f"REPO-MARKER-{r}-7f3a" for r in ("test_writer", "builder", "reviewer", "drift_keeper")}
        for r, m in markers.items():
            self.plant(self.repo, r, m)
        c = self.run_to_done()
        for r, m in markers.items():  # a different marker in the layer worktree is never used
            self.layer_plant(r, f"LAYER-MARKER-{r}-9c1d")
        for _ in range(3):
            c.step()
        self.assertEqual(c._task("T1")["status"], "done")
        for r, m in markers.items():
            with self.subTest(role=r):
                prompts = getattr(self.team, r).prompts
                self.assertTrue(prompts)
                self.assertTrue(prompts[0].startswith(f"{m}\nRules for {r}.\n\n"), prompts[0][:200])
        self.assertFalse(any("LAYER-MARKER" in p for p in self.all_prompts()))

    def test_layer_worktree_role_file_committed_is_never_used(self):
        c = self.run_to_done()
        wt = self.work / self.layer
        self.layer_plant("builder", "LAYER-MARKER-builder")
        self.layer_plant("test_writer", "LAYER-MARKER-test_writer")
        self.layer_plant("reviewer", "LAYER-MARKER-reviewer")
        git(wt, "add", "agents")
        git(wt, "commit", "-m", "agent edits its own instructions")
        for _ in range(3):
            c.step()
        self.assertFalse(any("LAYER-MARKER" in p for p in self.all_prompts()))
        self.assertTrue(self.team.builder.prompts[0].startswith(DEFAULTS["builder"] + "\n\n"))

    def test_defaults_sent_without_agents_folder(self):
        self.assertFalse((self.repo / "agents").exists())
        c = self.run_to_done()
        for _ in range(3):
            c.step()
        for r in ("test_writer", "builder", "reviewer", "drift_keeper"):
            with self.subTest(role=r):
                p = getattr(self.team, r).prompts[0]
                self.assertTrue(p.startswith(DEFAULTS[r] + "\n\n"), p[:200])

    def test_stage_instructions_stay_after_role_text(self):
        self.plant(self.repo, "builder", "BUILDER-MARKER")
        self.plant(self.repo, "reviewer", "REVIEWER-MARKER")
        c = self.run_to_done(reviewer=lambda p, cwd: ('{"verdict":"fail","reasons":["needs edge case"]}', 1))
        for _ in range(3):
            c.step()
        second = self.team.builder.prompts[1]
        self.assertTrue(second.startswith("BUILDER-MARKER"))
        self.assertLess(second.index("BUILDER-MARKER"), second.index("TASK T1"))
        self.assertIn("REVIEW FEEDBACK:", second)
        self.assertIn("needs edge case", second)
        self.assertIn("Answer with JSON", second)
        rv = self.team.reviewer.prompts[0]
        self.assertTrue(rv.startswith("REVIEWER-MARKER"))
        self.assertIn("DIFF:", rv)

    def test_troubleshooter_and_planner_prompts(self):
        self.plant(self.repo, "troubleshooter", "TS-MARKER")
        self.plant(self.repo, "planner", "PLAN-MARKER")
        self.plant(self.repo, "reviewer", "RV-MARKER")
        planfile = "plan.md"
        task = {"id": "P1", "kind": "plan", "title": "Plan", "section": "Plan it", "plan_file": planfile}

        def planner(p, cwd):
            (cwd / planfile).write_text("plan\n", encoding="utf-8")
            return json.dumps({"tasks": [self.task(id="T2", section="s" * 600)]}), 1  # R41: sections >= 600 chars

        c = self.init(task, agents={"planner": planner, "test_writer": self.write_tests,
                                    "builder": lambda p, cwd: ('{"status":"done"}', 1)})
        self.layer_plant("planner", "LAYER-MARKER-planner")
        self.layer_plant("troubleshooter", "LAYER-MARKER-troubleshooter")
        for _ in range(4):
            c.step()
        self.assertTrue(self.team.planner.prompts[0].startswith("PLAN-MARKER\nRules for planner.\n\n"))
        self.assertIn(planfile, self.team.planner.prompts[0])
        self.assertTrue(self.team.reviewer.prompts[0].startswith("RV-MARKER"))
        self.assertIn("PLAN:", self.team.reviewer.prompts[0])
        self.assertTrue(self.team.troubleshooter.prompts, "the failing builder must reach the troubleshooter")
        self.assertTrue(self.team.troubleshooter.prompts[0].startswith("TS-MARKER\nRules for troubleshooter.\n\n"))
        self.assertIn("RECENT FAILURES", self.team.troubleshooter.prompts[0])
        self.assertFalse(any("LAYER-MARKER" in p for p in self.all_prompts()))


if __name__ == "__main__":
    unittest.main()
