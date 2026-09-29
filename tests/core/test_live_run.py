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
                        (cwd / "smoke.txt").write_text("ok", encoding="utf-8")
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


class ReviewMailTests(Harness):
    read_state = LiveRunTests.read_state
    timed_conductor = LiveRunTests.timed_conductor

    def gate(self, c):
        qid = c._ask("gate", "ready", "reply y", pr=7)
        q = self.read_state("questions.json")[qid]
        return qid, f"Re: [Forge Q-{qid} {q['code']}] ready"

    def test_R24_kill_refuses_normal_send(self):
        """R24: direct sends are refused while KILL exists."""
        c = self.timed_conductor()
        (self.state / "KILL").touch()
        self.assertIs(c._send("ordinary", "body"), False)
        self.assertEqual(self.mails, [])

    def test_R24_halt_alert_uses_clock_and_budget(self):
        """R24: one halt bypasses KILL, recurs after 12h, and consumes budget."""
        c = self.timed_conductor(mail_per_hour=1, mail_per_day=30)
        (self.state / "KILL").touch()
        self.assertIs(c._send("halt", "tamper", halt=True), True)
        self.now += timedelta(hours=11, minutes=59)
        self.assertIs(c._send("halt again", "tamper", halt=True), False)
        self.assertEqual(len(self.mails), 1)
        self.now += timedelta(minutes=2)
        self.assertIs(c._send("halt later", "tamper", halt=True), True)
        (self.state / "KILL").unlink()
        self.assertIs(c._send("ordinary", "body"), False)
        self.assertEqual(len(self.mails), 2)

    def test_R24_tamper_sends_exactly_one_halt_after_kill(self):
        """R24: state tampering writes KILL before sending exactly one halt alert."""
        def tamper(prompt, cwd):
            (self.state / "queue.json").write_text('{}', encoding="utf-8")
            return '{"files":[]}', 1
        c = self.init(agents={"test_writer": tamper})
        observed = []
        def mailer(subject, body, **kw):
            observed.append(((self.state / "KILL").exists(), subject + body))
        c.mailer = mailer
        with patch.object(c, "_send", wraps=c._send) as send:
            c.step()
            self.assertEqual(c.step(), "killed")
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][0])
        self.assertIn("tamper", observed[0][1].lower())
        self.assertTrue(any(call.kwargs.get("halt") is True for call in send.call_args_list))

    def test_R24_stop_ends_inbox_batch_before_gate_reply(self):
        """R24: a valid gate reply after STOP in the same batch cannot merge."""
        c = self.make_conductor()
        qid, subject = self.gate(c)
        self.messages.extend([{"from": c.owner, "subject": "STOP", "body": ""},
                              {"from": c.owner, "subject": subject, "body": "y"}])
        self.assertEqual(c.step(), "killed")
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "open")

    def test_R25_failed_sends_consume_hourly_budget(self):
        """R25: two failed delivery attempts exhaust a two-per-hour budget."""
        c = self.timed_conductor(mail_per_hour=2)
        c.mailer = Mock(side_effect=[OSError("SMTP failed"), OSError("SMTP failed"), None])
        self.assertIs(c._send("first", "body"), False)
        self.assertIs(c._send("second", "body"), False)
        self.assertIs(c._send("third", "body"), False)
        self.assertEqual(c.mailer.call_count, 2)
        self.now += timedelta(minutes=61)
        self.assertIs(c._send("later", "body"), True)

    def test_R25_failed_notice_is_throttled_for_12h(self):
        """R25: a notice's failing mailer still reserves its 12-hour throttle."""
        c = self.timed_conductor()
        c.mailer = Mock(side_effect=OSError("SMTP failed after acceptance"))
        self.assertIs(c._notice_once("started", "started", "body"), False)
        self.now += timedelta(hours=11, minutes=59)
        self.assertIs(c._notice_once("started", "started", "body"), False)
        self.assertEqual(c.mailer.call_count, 1)
        self.now += timedelta(minutes=2)
        c.mailer.side_effect = None
        self.assertIs(c._notice_once("started", "started", "body"), True)

    def test_R26_sent_message_id_blocks_headerless_self_reply(self):
        """R26: generated Message-IDs persist and block even headerless self-mail."""
        c = self.make_conductor()
        qid, subject = self.gate(c)
        sent = []
        c.mailer = lambda s, b, **kw: sent.append(kw)
        self.assertIs(c._send(subject, "y"), True)
        self.assertIn("message_id", sent[0])
        message_id = sent[0]["message_id"]
        self.assertRegex(message_id, r"^<.+@.+>$")
        self.assertIn(message_id, json.dumps(self.read_state("mail_log.json")))
        # Restart to prove the filter uses the persisted list.
        c = self.make_conductor()
        self.messages.append({"from": c.owner, "subject": subject, "body": "y", "message_id": message_id})
        c._handle_inbox()
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "open")

    def test_R27_stop_subject_and_clean_first_word(self):
        """R27: STOP needs no code and works in a cleaned reply to a notice."""
        c = self.make_conductor()
        for subject, body in [("STOP", ""), ("Re: Fwd: stop!", ""),
                              ("Re: [Forge] conductor started", "stop\n\nOn Tuesday wrote:\n> old"),
                              ("Re: [Forge] conductor started", "STOP! please")]:
            with self.subTest(subject=subject, body=body):
                (self.state / "KILL").unlink(missing_ok=True)
                self.messages[:] = [{"from": c.owner, "subject": subject, "body": body}]
                self.assertEqual(c.step(), "killed")
                self.assertTrue((self.state / "KILL").exists())

    def test_R27_quoted_or_noncommand_stop_does_not_kill(self):
        """R27: quoted STOP, prose, and a stopped-notice subject are not commands."""
        c = self.make_conductor()
        for subject, body in [("Re: [Forge] conductor started", "keep going\n> stop"),
                              ("Re: [Forge] conductor started", "Please don't stop yet"),
                              ("Re: [Forge Q-x code] Forge stopped: ...", "y")]:
            with self.subTest(body=body):
                (self.state / "KILL").unlink(missing_ok=True)
                self.messages[:] = [{"from": c.owner, "subject": subject, "body": body}]
                c._handle_inbox()
                self.assertFalse((self.state / "KILL").exists())

    def test_R28_question_storage_bounds(self):
        """R28: question text is bounded before persistence, not just delivery."""
        c = self.make_conductor()
        qid = c._ask("blocked", "s" * 5000, "b" * 1_000_000)
        q = self.read_state("questions.json")[qid]
        self.assertLessEqual(len(q["subject"]), 300)
        self.assertLessEqual(len(q["body"]), 20000)

    def test_R28_keep_open_and_only_50_recent_closed_questions(self):
        """R28: eighty answers retain every open question and the newest 50 closed."""
        c = self.init()
        open_ids = {c._ask("blocked", f"open {i}", "body") for i in range(3)}
        answered = []
        for i in range(80):
            qid = c._ask("replan", f"question {i}", "body")
            self.assertNotIn(qid, open_ids | set(answered), "pruning must not reuse question IDs")
            code = self.read_state("questions.json")[qid]["code"]
            c._answer(qid, f"answer {i}", code)
            answered.append(qid)
        qs = self.read_state("questions.json")
        self.assertEqual({k for k, q in qs.items() if q["status"] == "open"}, open_ids)
        closed = {k for k, q in qs.items() if q["status"] != "open"}
        self.assertLessEqual(len(closed), 50)
        self.assertEqual(closed, set(answered[-50:]))

    def test_R28_replan_queue_notes_keep_30_bounded_entries(self):
        """R28: queue-level replan notes retain the last 30 entries capped at 2000."""
        c = self.init()
        for i in range(40):
            qid = c._ask("replan", "advice needed", "body")
            code = self.read_state("questions.json")[qid]["code"]
            c._answer(qid, f"{i:02d}:" + "x" * 5000, code)
        notes = self.read_state("queue.json")["notes"]
        self.assertEqual(len(notes), 30)
        self.assertEqual([n[:8] for n in notes], [f"Ben: {i:02d}:" for i in range(10, 40)])
        self.assertTrue(all(len(n) <= 2000 for n in notes))


class FakePeekIMAP:
    """A persistent mailbox exposed through ordinary imaplib method signatures."""
    def __init__(self):
        self.messages = []
        self.fetches, self.stores, self.searches = [], [], []

    def add(self, message_id, body="y", outgoing=False):
        from email.message import EmailMessage
        from email.policy import SMTP
        msg = EmailMessage()
        msg["From"] = "ben@example.com"
        msg["To"] = "ben@example.com"
        msg["Subject"] = "Re: [Forge Q-gate-1 abcdefgh] ready"
        msg["Message-ID"] = message_id
        if outgoing:
            msg["X-Forge-Outgoing"] = "1"
        msg.set_content(body)
        self.messages.append(msg.as_bytes(policy=SMTP))

    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criteria):
        self.searches.append((charset, criteria))
        return "OK", [b" ".join(str(i).encode() for i in range(1, len(self.messages) + 1))]

    def fetch(self, num, message_parts):
        self.fetches.append((num, message_parts))
        raw = self.messages[int(num) - 1]
        if message_parts == "(BODY.PEEK[HEADER])":
            raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        elif message_parts != "(BODY.PEEK[])":
            raise AssertionError(f"Non-PEEK or unsupported fetch: {message_parts}")
        return "OK", [(str(int(num)).encode() + b" (...)", raw), b")"]

    def store(self, num, command, flags):
        self.stores.append((num, command, flags))
        raise AssertionError("Inbox must never change read flags")

    def logout(self):
        return "BYE", [b"logged out"]


class ReviewInboxTests(Harness):
    def reader(self, server):
        keyring = ModuleType("keyring")
        keyring.get_password = Mock(return_value="fake-password")
        patches = [patch.dict(sys.modules, {"keyring": keyring}),
                   patch("imaplib.IMAP4_SSL", side_effect=AssertionError("Real IMAP forbidden"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return bootstrap.gmail_inbox("ben@example.com", self.state,
                                     imap_factory=lambda *args, **kw: server)

    def test_R26_first_read_seeds_seen_without_returning_old_mail(self):
        """R26: first read records existing Message-IDs and returns no stale replies."""
        server = FakePeekIMAP()
        server.add("<old-owner@example.com>")
        server.add("<old-forge@example.com>", outgoing=True)
        read = self.reader(server)
        self.assertEqual(read(), [])
        seen = (self.state / "inbox_seen.json").read_text(encoding="utf-8")
        for mid in ("<old-owner@example.com>", "<old-forge@example.com>"):
            self.assertIn(mid, seen)
        self.assertEqual(server.stores, [])
        self.assertTrue(server.fetches)
        self.assertTrue(all(item == "(BODY.PEEK[HEADER])" for _, item in server.fetches))

    def test_R26_new_owner_reply_returned_once_with_peek_only(self):
        """R26: new owner mail is returned once, including across reader recreation."""
        server = FakePeekIMAP()
        server.add("<old@example.com>")
        read = self.reader(server)
        self.assertEqual(read(), [])
        server.fetches.clear()
        server.add("<new@example.com>", "Please proceed")
        messages = read()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from"], "ben@example.com")
        self.assertEqual(messages[0]["body"].strip(), "Please proceed")
        self.assertEqual(messages[0]["message_id"], "<new@example.com>")
        self.assertEqual(self.reader(server)(), [])
        self.assertEqual(server.stores, [])
        items = [item for num, item in server.fetches if int(num) == 2]
        self.assertIn("(BODY.PEEK[])", items)
        self.assertLess(items.index("(BODY.PEEK[HEADER])"), items.index("(BODY.PEEK[])"))
        self.assertTrue(all(item in {"(BODY.PEEK[HEADER])", "(BODY.PEEK[])"}
                            for _, item in server.fetches))

    def test_R26_outgoing_header_cannot_answer_gate(self):
        """R26: a new outgoing message is omitted or marked and ignored by conductor."""
        server = FakePeekIMAP()
        read = self.reader(server)
        self.assertEqual(read(), [])
        c = self.make_conductor()
        (self.state / "questions.json").write_text(json.dumps({"gate-1": {
            "kind": "gate", "status": "open", "code": "abcdefgh", "pr": 7,
            "subject": "ready", "body": "y", "delivered": True}}), encoding="utf-8")
        server.add("<outgoing@example.com>", outgoing=True)
        messages = read()
        self.assertTrue(all(m.get("outgoing") is True for m in messages))
        c.inbox = lambda: messages
        c._handle_inbox()
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(json.loads((self.state / "questions.json").read_text())["gate-1"]["status"], "open")
        self.assertEqual(server.stores, [])
        self.assertTrue(all(item == "(BODY.PEEK[HEADER])" for _, item in server.fetches))


class ReviewSmokeTests(Harness):
    def smoke_team(self, bad_role=None, fault=None):
        calls = []
        answers = {"test_writer": {"files": ["smoke.txt"]}, "builder": {"status": "done"},
                   "planner": {"tasks": []}, "reviewer": {"verdict": "pass", "reasons": []},
                   "drift_keeper": {"status": "ok"}, "troubleshooter": {"kind": "suggestion", "notes": "ok"}}
        members = {}
        for role, answer in answers.items():
            def script(prompt, cwd, role=role, answer=answer):
                calls.append((role, Path(cwd)))
                if role in {"test_writer", "builder", "planner"}:
                    if role == bad_role and fault == "directory":
                        (cwd / "smoke.txt").mkdir()
                    else:
                        (cwd / "smoke.txt").write_text(
                            "nope" if role == bad_role and fault == "content" else "ok", encoding="utf-8")
                if role == bad_role:
                    if fault == "commit":
                        bootstrap._git(cwd, "add", "smoke.txt")
                        bootstrap._git(cwd, "-c", "user.name=Fake", "-c", "user.email=fake@example.com",
                                       "commit", "-m", "unauthorized agent commit")
                    elif fault == "untracked":
                        (cwd / "unexpected.txt").write_text("changed", encoding="utf-8")
                    elif fault == "null_tasks":
                        answer = {"tasks": None}
                    elif fault == "enum":
                        answer = {"verdict": "maybe", "reasons": []}
                    elif fault == "tamper":
                        (self.state / "queue.json").write_text('{"tampered":true}', encoding="utf-8")
                return json.dumps(answer), 7
            members[role] = FakeAgent(script, provider="codex" if role in {"test_writer", "reviewer"} else "claude")
        return Team(**members), calls

    def guarded_conductor(self, bad_role=None, fault=None):
        c = self.make_conductor()
        c.team, calls = self.smoke_team(bad_role, fault)
        self.now = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        c.mailer = lambda s, b, **kw: self.mails.append((s, b))
        return c, calls

    def test_R29_each_role_has_a_distinct_fresh_repo(self):
        """R29: each role runs in a different fresh git repository."""
        team, calls = self.smoke_team()
        snapshots = []
        def call(role, prompt, schema, cwd):
            snapshots.append((role, bootstrap._git(cwd, "status", "--porcelain", "-uall"),
                              (cwd / "smoke.txt").exists(), (cwd / ".git").is_dir()))
            return getattr(team, role).run(prompt, cwd, schema)
        self.assertEqual(bootstrap.smoke(team, self.work, call=call), [])
        self.assertEqual(len(calls), 6)
        self.assertEqual(len({cwd.resolve() for _, cwd in calls}), 6)
        self.assertEqual(len(snapshots), 6)
        self.assertTrue(all(status == "" and not smoke_exists and git_dir
                            for _, status, smoke_exists, git_dir in snapshots))

    def test_R29_writer_directory_or_wrong_content_fails(self):
        """R29: all writer roles must create a regular smoke.txt containing ok."""
        for role in ("test_writer", "builder", "planner"):
            for fault in ("directory", "content"):
                with self.subTest(role=role, fault=fault):
                    team, _ = self.smoke_team(role, fault)
                    problems = bootstrap.smoke(team, self.work)
                    self.assertTrue(any(role in p for p in problems), problems)

    def test_R29_writer_commit_changes_head_and_fails(self):
        """R29: a writer cannot hide its change by committing smoke.txt."""
        for role in ("test_writer", "builder", "planner"):
            with self.subTest(role=role):
                team, _ = self.smoke_team(role, "commit")
                problems = bootstrap.smoke(team, self.work)
                self.assertTrue(any(role in p for p in problems), problems)

    def test_R29_readonly_untracked_file_fails_guarded_smoke(self):
        """R29: guarded smoke rejects a new untracked file from a read-only role."""
        c, calls = self.guarded_conductor("reviewer", "untracked")
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertTrue(any("reviewer" in p for p in problems), problems)
        self.assertIn("reviewer", [role for role, _ in calls])

    def test_R29_planner_null_tasks_fails_full_schema_validation(self):
        """R29: the planner's required tasks key cannot contain null."""
        team, _ = self.smoke_team("planner", "null_tasks")
        problems = bootstrap.smoke(team, self.work)
        self.assertTrue(any("planner" in p for p in problems), problems)

    def test_R29_reviewer_enum_violation_fails(self):
        """R29: required keys alone cannot make verdict maybe a valid review."""
        team, _ = self.smoke_team("reviewer", "enum")
        problems = bootstrap.smoke(team, self.work)
        self.assertTrue(any("reviewer" in p for p in problems), problems)

    def test_R29_schema_ok_checks_nested_types_and_required_keys(self):
        """R29: full schema validation checks nested task items, types, and enums."""
        from core import agents
        valid = {"tasks": [{"id": "T1", "title": "task", "section": "spec", "files_in_scope": ["a.py"],
                            "test_files": ["tests/test_a.py"], "test_cmd": "python -m unittest tests/test_a.py"}]}
        self.assertTrue(agents.schema_ok(valid, bootstrap.S_PLAN))
        for invalid in ({"tasks": None}, {"tasks": [{}]}, {"tasks": ["wrong"]}, {}):
            with self.subTest(invalid=invalid):
                self.assertFalse(agents.schema_ok(invalid, bootstrap.S_PLAN))
        invalid = json.loads(json.dumps(valid))
        invalid["tasks"][0]["test_files"] = [42]
        self.assertFalse(agents.schema_ok(invalid, bootstrap.S_PLAN))
        self.assertFalse(agents.schema_ok({"verdict": "maybe", "reasons": []}, bootstrap.S_REVIEW))

    def test_R29_guarded_tamper_kills_and_returns_problem(self):
        """R29: a smoke agent changing queue.json triggers KILL and a halt alert."""
        c, calls = self.guarded_conductor("test_writer", "tamper")
        (self.state / "queue.json").write_text('{"tasks":[]}', encoding="utf-8")
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertTrue(problems)
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual([role for role, _ in calls], ["test_writer"])
        self.assertEqual(len(self.mails), 1)
        self.assertIn("tamper", " ".join(self.mails[0]).lower())

    def test_R29_guarded_smoke_meters_tokens_and_records_runs(self):
        """R29: all smoke calls use conductor metering and persist run evidence."""
        c, calls = self.guarded_conductor()
        c.meter.add("codex", 3)
        before = (self.state / "meter.json").read_bytes()
        self.assertEqual(bootstrap._guarded_smoke(c, self.work), [])
        self.assertEqual(len(calls), 6)
        self.assertNotEqual((self.state / "meter.json").read_bytes(), before)
        self.assertEqual(c.meter.used_today("codex"), 3 + 14)
        self.assertEqual(c.meter.used_today("claude"), 28)
        self.assertEqual(len(list((self.state / "runs").glob("*/prompt.md"))), 6)
        self.assertEqual(len(list((self.state / "runs").glob("*/output.json"))), 6)
        self.assertTrue((self.state / "smoke_ok.json").exists())

    def test_R29_guarded_smoke_refuses_reached_token_cap(self):
        """R29: an already exhausted provider cap prevents every smoke agent call."""
        c, calls = self.guarded_conductor()
        c.limits["codex_daily_token_cap"] = 7
        c.meter.add("codex", 7)
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertTrue(any("cap" in p.lower() for p in problems), problems)
        self.assertEqual(calls, [])

    def test_R29_failed_attempt_removes_success_and_waits_30_minutes(self):
        """R29: failed smoke clears old success and persists a 30-minute retry wait."""
        c, calls = self.guarded_conductor("builder", "content")
        ok = self.state / "smoke_ok.json"
        ok.write_text('{"stale":true}', encoding="utf-8")
        original_run = c.team.test_writer.run
        def observe_start(*args, **kw):
            self.assertFalse(ok.exists(), "success must be removed before the first agent call")
            return original_run(*args, **kw)
        with patch.object(c.team.test_writer, "run", side_effect=observe_start):
            self.assertTrue(bootstrap._guarded_smoke(c, self.work))
        self.assertTrue(calls)
        self.assertFalse(ok.exists())
        self.assertTrue((self.state / "smoke_fail.json").exists())
        self.now += timedelta(minutes=29)
        calls.clear()
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertEqual(calls, [])
        self.assertTrue(any("wait" in p.lower() for p in problems), problems)
        self.now += timedelta(minutes=2)
        bootstrap._guarded_smoke(c, self.work)
        self.assertTrue(calls)
