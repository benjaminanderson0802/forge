"""Live-run regression contracts; all agents and mail transports are local fakes."""
import json
import sys
import unittest
from types import ModuleType
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core import bootstrap
from core.agents import FakeAgent
from core.bootstrap import Conductor, Team

try:
    from tests.core.test_bootstrap import Harness
except ImportError:
    from test_bootstrap import Harness


class LiveRunTests(Harness):
    def read_state(self, name):
        return json.loads((self.state / name).read_text(encoding="utf-8"))

    def blocked_question(self):
        def weak_writer(prompt, cwd):
            path = cwd / "tests/core/test_feat.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("import unittest\nclass T(unittest.TestCase):\n def test_weak(self): self.assertTrue(True)\n", encoding="utf-8")
            return '{"files":["tests/core/test_feat.py"]}', 1

        c = self.init(agents={"test_writer": weak_writer})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "blocked")
        self.assertTrue(all("weak" in note for note in c._task("T1")["notes"]))
        questions = self.read_state("questions.json")
        self.assertEqual(len(questions), 1)
        return c, next(iter(questions))

    def timed_conductor(self, **limits):
        self.now = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
        self.make_conductor()
        c = Conductor(self.repo, self.work, self.state, self.team,
                      {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9, **limits},
                      owner_email="ben@example.com", mailer=lambda s, b: self.mails.append((s, b)),
                      inbox=lambda: [], gh=self.gh, clock=lambda: self.now, judge_cmds=[], push=False)
        self.c = c
        return c

    def test_R17_required_only_writer_answer_still_works(self):
        """R17: a test writer returning only files still advances the conductor."""
        c = self.init(agents={"test_writer": self.write_tests})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c._task("T1")["status"], "tests_ok")

    def test_R18_outgoing_question_is_not_an_answer(self):
        """R18: Forge's own blocked email cannot reopen a task or grow its notes."""
        c, qid = self.blocked_question()
        before = c._task("T1")["trouble_notes"]
        subject, body = self.mails[-1]
        self.messages.append({"from": c.owner, "subject": subject, "body": body, "outgoing": True})
        c.step()
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "open")
        self.assertEqual(c._task("T1")["status"], "blocked")
        self.assertEqual(c._task("T1")["trouble_notes"], before)
        self.assertEqual(len(self.mails), 1)

    def test_R18_genuine_reply_is_applied_and_quotes_cleaned(self):
        """R18: a genuine coded reply resumes the task and removes both quote forms."""
        c, qid = self.blocked_question()
        self.messages.append({"from": c.owner, "subject": self.mails[-1][0],
                              "body": "Try a different assertion.\n> old quoted line\nKeep this advice.\nOn Tuesday Ben wrote:\nold mail body"})
        (self.state / "PAUSED").touch()  # Inspect reply handling before another agent attempt.
        self.assertEqual(c.step(), "paused")
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "answered")
        self.assertEqual(c._task("T1")["status"], "todo")
        note = c._task("T1")["trouble_notes"][-1]
        self.assertTrue(note.startswith("Ben: "))
        self.assertIn("Try a different assertion.", note)
        self.assertIn("Keep this advice.", note)
        self.assertNotIn("old quoted line", note)
        self.assertNotIn("On Tuesday", note)
        self.assertNotIn("old mail body", note)

    def test_R18_genuine_long_reply_is_bounded(self):
        """R18: a 50000-character genuine reply stores at most 2000 plus the Ben prefix."""
        c, qid = self.blocked_question()
        self.messages.append({"from": c.owner, "subject": self.mails[-1][0], "body": "x" * 50000})
        (self.state / "PAUSED").touch()
        c.step()
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "answered")
        note = c._task("T1")["trouble_notes"][-1]
        self.assertTrue(note.startswith("Ben: x"))
        self.assertLessEqual(len(note), 2000 + len("Ben: "))

    def test_R18_gmail_mailer_marks_outgoing_header(self):
        """R18: SMTP messages carry X-Forge-Outgoing to identify looped-back mail."""
        keyring = ModuleType("keyring")
        keyring.get_password = Mock(return_value="fake-password")
        with patch.dict(sys.modules, {"keyring": keyring}), patch("smtplib.SMTP_SSL") as smtp:
            with patch("keyring.get_password", return_value="fake-password"):
                bootstrap.gmail_mailer("ben@example.com")("question", "guidance needed")
        send = smtp.return_value.__enter__.return_value.send_message
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0]["X-Forge-Outgoing"], "1")

    def test_R19_task_note_lists_keep_only_bounded_recent_entries(self):
        """R19: both persisted note lists retain at most the last 30 bounded entries."""
        c = self.init()
        entries = [f"{i:02d}:" + "x" * 5000 for i in range(40)]
        c._update("T1", notes=entries, trouble_notes=entries)
        task = self.read_state("queue.json")["tasks"][0]
        for field in ("notes", "trouble_notes"):
            with self.subTest(field=field):
                self.assertEqual(len(task[field]), 30)
                self.assertEqual([note[:3] for note in task[field]], [f"{i:02d}:" for i in range(10, 40)])
                self.assertTrue(all(len(note) <= 2000 for note in task[field]))

    def test_R19_blocked_email_body_is_bounded(self):
        """R19: blocked-task email bodies never exceed 20000 characters."""
        c = self.init()
        c._update("T1", notes=["n" * 50000] * 40, trouble_notes=["t" * 50000] * 40)
        c._block("T1", "repeated failures")
        self.assertTrue(self.mails)
        self.assertTrue(all(len(body) <= 20000 for _, body in self.mails))

    def test_R20_hourly_budget_defers_and_retries_questions(self):
        """R20: only two of four questions send this hour; the remainder retry later."""
        c = self.timed_conductor(mail_per_hour=2, mail_per_day=30)
        ids = [c._ask("blocked", f"question {i}", "body", task="T1") for i in range(4)]
        self.assertEqual(len(self.mails), 2)
        self.assertEqual([self.read_state("questions.json")[qid]["delivered"] for qid in ids], [True, True, False, False])
        self.assertTrue((self.state / "mail_log.json").exists())
        c.step()
        self.assertEqual(len(self.mails), 2)
        self.now += timedelta(minutes=61)
        c.step()
        self.assertEqual(len(self.mails), 4)
        self.assertTrue(all(self.read_state("questions.json")[qid]["delivered"] for qid in ids))

    def test_R20_daily_budget_applies_across_hours(self):
        """R20: a three-per-day budget still caps mail after the hourly window resets."""
        c = self.timed_conductor(mail_per_hour=2, mail_per_day=3)
        for i in range(2):
            c._ask("blocked", f"first hour {i}", "body")
        self.now += timedelta(minutes=61)
        ids = [c._ask("blocked", f"second hour {i}", "body") for i in range(2)]
        self.assertEqual(len(self.mails), 3)
        self.assertFalse(self.read_state("questions.json")[ids[-1]]["delivered"])
        self.now += timedelta(minutes=61)
        c.step()
        self.assertEqual(len(self.mails), 3)
        self.now += timedelta(hours=25)
        c.step()
        self.assertEqual(len(self.mails), 4)
        self.assertTrue(self.read_state("questions.json")[ids[-1]]["delivered"])

    def test_R20_send_returns_delivery_boolean(self):
        """R20: the shared send method reports success and budget refusal as booleans."""
        c = self.timed_conductor(mail_per_hour=1, mail_per_day=30)
        self.assertIs(c._send("first", "body"), True)
        self.assertIs(c._send("second", "body"), False)
        self.assertEqual(self.mails, [("first", "body")])

    def test_R21_kill_prevents_inbox_and_pending_delivery(self):
        """R21: KILL is checked before inbox reads and undelivered question retries."""
        c = self.make_conductor()
        question = {"blocked-1": {"kind": "blocked", "status": "open", "task": "T1",
                    "code": "abcdefgh", "subject": "pending", "body": "body", "delivered": False}}
        (self.state / "questions.json").write_text(json.dumps(question), encoding="utf-8")
        inbox = Mock(return_value=[])
        c.inbox = inbox
        (self.state / "KILL").touch()
        self.assertEqual(c.step(), "killed")
        with self.subTest(operation="inbox"):
            inbox.assert_not_called()
        with self.subTest(operation="delivery"):
            self.assertEqual(self.mails, [])
            self.assertEqual(self.read_state("questions.json"), question)

    def test_R22_start_notice_is_persistent_and_limited_to_12_hours(self):
        """R22: start notices persist across conductor instances and recur after 12h."""
        c = self.timed_conductor()
        self.assertIs(c._notice_once("started", "conductor started", "body", every_h=12), True)
        self.assertIs(c._notice_once("started", "conductor started", "body", every_h=12), False)
        self.assertTrue(self.read_state("notices.json"))
        # Reconstruct the conductor without clearing its state, simulating watchdog restart.
        c = self.timed_conductor()
        self.now += timedelta(hours=11, minutes=59)
        self.assertIs(c._notice_once("started", "conductor started", "body", every_h=12), False)
        self.now += timedelta(minutes=2)
        self.assertIs(c._notice_once("started", "conductor started", "body", every_h=12), True)
        self.assertEqual(len(self.mails), 2)

    def test_R22_start_notice_is_suppressed_while_killed(self):
        """R22: KILL suppresses even the first conductor-started notification."""
        c = self.timed_conductor()
        (self.state / "KILL").touch()
        self.assertIs(c._notice_once("started", "conductor started", "body", every_h=12), False)
        self.assertEqual(self.mails, [])

    def test_R22_error_notice_is_limited_across_run_calls(self):
        """R22: repeated failing run invocations produce only one error notice per 12h."""
        c = self.timed_conductor()
        with patch.object(c, "step", return_value="error"):
            for _ in range(2):
                self.assertEqual(c.run(max_steps=4, idle_sleep_s=0, sleep=lambda _: None), "error")
            alerts = [s for s, b in self.mails if "keeps hitting an error" in (s + b).lower()]
            self.assertEqual(len(alerts), 1)
            self.now += timedelta(hours=12, minutes=1)
            c.run(max_steps=4, idle_sleep_s=0, sleep=lambda _: None)
        self.assertEqual(sum("keeps hitting an error" in (s + b).lower() for s, b in self.mails), 2)


class SmokeTests(Harness):
    def smoke_team(self, bad_role=None, failure=None):
        answers = {"test_writer": {"files": ["smoke.txt"]}, "builder": {"status": "done"},
                   "planner": {"tasks": []}, "reviewer": {"verdict": "pass", "reasons": []},
                   "drift_keeper": {"status": "ok"}, "troubleshooter": {"kind": "suggestion", "notes": "ok"}}
        calls = []
        members = {}
        for role, answer in answers.items():
            def script(prompt, cwd, role=role, answer=answer):
                calls.append((role, cwd))
                if role in {"test_writer", "builder", "planner"}:
                    if not (role == bad_role and failure == "no_file"):
                        (cwd / "smoke.txt").write_text("smoke from " + role, encoding="utf-8")
                if role == bad_role and failure == "write":
                    (cwd / "smoke.txt").write_text("read-only role changed this", encoding="utf-8")
                return json.dumps({} if role == bad_role and failure == "shape" else answer), 1
            members[role] = FakeAgent(script)
        return Team(**members), calls

    def test_R23_smoke_accepts_valid_fake_team(self):
        """R23: every role runs once and valid writers produce smoke.txt in their cwd."""
        team, calls = self.smoke_team()
        self.assertEqual(bootstrap.smoke(team, self.work), [])
        self.assertCountEqual([role for role, cwd in calls], list(vars(team)))
        self.assertEqual(len(calls), 6)

    def test_R23_smoke_rejects_each_writer_missing_file(self):
        """R23: each writer must create its own smoke.txt, not inherit another writer's."""
        for role in ("test_writer", "builder", "planner"):
            with self.subTest(role=role):
                team, _ = self.smoke_team(role, "no_file")
                problems = bootstrap.smoke(team, self.work / role)
                self.assertTrue(problems)
                self.assertTrue(any(role in problem for problem in problems), problems)

    def test_R23_smoke_rejects_readonly_writes(self):
        """R23: reviewer and drift keeper file changes are reported by role."""
        for role in ("reviewer", "drift_keeper"):
            with self.subTest(role=role):
                team, _ = self.smoke_team(role, "write")
                problems = bootstrap.smoke(team, self.work / role)
                self.assertTrue(problems)
                self.assertTrue(any(role in problem for problem in problems), problems)

    def test_R23_smoke_rejects_missing_required_json_for_every_role(self):
        """R23: missing required answer keys are smoke failures for every role."""
        for role in ("test_writer", "builder", "planner", "reviewer", "drift_keeper", "troubleshooter"):
            with self.subTest(role=role):
                team, _ = self.smoke_team(role, "shape")
                problems = bootstrap.smoke(team, self.work / role)
                self.assertTrue(problems)
                self.assertTrue(any(role in problem for problem in problems), problems)
