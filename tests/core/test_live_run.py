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


class ReviewRoundTwoRunTests(Harness):
    read_state = LiveRunTests.read_state

    def setUp(self):
        super().setUp()
        # main has no state override and checks its local state before smoke.
        # Redirect __file__ rather than mocking Path: real Path semantics then
        # give main and Conductor the same isolated state directory (unlike an
        # __init__-only redirect). The initializer wrapper only injects a clock.
        self.state = self.repo / "state" / "bootstrap"
        self.state.mkdir(parents=True)
        self.now = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
        self.make_conductor()
        self.c.clock = lambda: self.now
        self.c._write("smoke_ok.json", {"at": (self.now - timedelta(hours=25)).isoformat()})
        self.inbox = Mock(side_effect=lambda: list(self.messages))
        real_init = Conductor.__init__

        def timed_init(conductor, *args, **kwargs):
            kwargs["clock"] = lambda: self.now
            real_init(conductor, *args, **kwargs)

        patches = {
            "module_file": patch("core.bootstrap.__file__", str(self.repo / "core" / "bootstrap.py")),
            "limits": patch("core.agents.load_limits", return_value=self.c.limits),
            "team": patch("core.bootstrap.real_team", return_value=self.team),
            "mailer": patch("core.bootstrap.gmail_mailer", return_value=self.c.mailer),
            "inbox_factory": patch("core.bootstrap.gmail_inbox", return_value=self.inbox),
            "gh": patch("core.bootstrap.gh_cli", return_value=self.gh),
            "init": patch.object(Conductor, "__init__", new=timed_init),
            "lock": patch("core.bootstrap.acquire_lock", side_effect=lambda state: Mock()),
            "guarded_smoke": patch("core.bootstrap._guarded_smoke", return_value=[]),
            "smoke": patch("core.bootstrap.smoke", return_value=[]),
            "run": patch.object(Conductor, "run", return_value="idle"),
        }
        self.cli = {}
        for name, patcher in patches.items():
            self.cli[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def assert_no_work(self):
        self.cli["guarded_smoke"].assert_not_called()
        self.cli["smoke"].assert_not_called()
        self.cli["run"].assert_not_called()

    def pending_halt(self, qid="tamper-1"):
        questions = self.c._read("questions.json", {})
        questions[qid] = {"kind": "tamper", "status": "open", "code": "abcdefgh",
                          "subject": "Forge tamper halt", "body": "tamper: queue.json changed",
                          "halt": True, "delivered": False}
        self.c._write("questions.json", questions)
        (self.state / "KILL").touch()
        return qid

    def test_R31_cli_owner_stop_prevents_stale_smoke_and_run(self):
        self.assertFalse((self.state / "KILL").exists())
        self.assertTrue(bootstrap._smoke_stale(self.state, self.now))
        self.messages.append({"from": "benjaminanderson0802@gmail.com",
                              "subject": "Re: [Forge] smoke failed", "body": "STOP"})
        self.assertEqual(bootstrap.main(["run"]), 0)
        with self.subTest(check="kill"):
            self.assertTrue((self.state / "KILL").exists())
        with self.subTest(check="no work"):
            self.assert_no_work()
        with self.subTest(check="inbox"):
            self.inbox.assert_called_once_with()

    def test_R31_cli_without_stop_reads_inbox_before_stale_smoke(self):
        events = []
        self.inbox.side_effect = lambda: events.append("inbox") or []
        self.cli["guarded_smoke"].side_effect = lambda *a, **kw: events.append("smoke") or []
        self.assertEqual(bootstrap.main(["run"]), 0)
        self.cli["guarded_smoke"].assert_called_once()
        self.assertEqual(events, ["inbox", "smoke"])
        self.assertFalse((self.state / "KILL").exists())

    def test_R32_cli_killed_retries_halt_and_throttles_across_restarts(self):
        qid = self.pending_halt()
        self.assertEqual(bootstrap.main(["run"]), 0)
        with self.subTest(check="delivery"):
            self.assertEqual(len(self.mails), 1)
            self.assertIn("tamper", " ".join(self.mails[0]).lower())
            self.assertIs(self.read_state("questions.json")[qid]["delivered"], True)
        with self.subTest(check="no work"):
            self.assert_no_work()
        self.inbox.assert_not_called()
        # Another pending halt makes this exercise the persisted throttle, not
        # merely the delivered flag on the first question.
        second = self.pending_halt("tamper-2")
        self.now += timedelta(hours=11, minutes=59)
        before = list(self.mails)
        self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertEqual(self.mails, before)
        self.assertIs(self.read_state("questions.json")[second]["delivered"], False)
        with self.subTest(check="restart no work"):
            self.assert_no_work()
        self.inbox.assert_not_called()

    def test_R32_cli_budget_refusal_does_not_reserve_halt_throttle(self):
        self.c.limits.update(mail_per_hour=1, mail_per_day=30)
        self.assertTrue(self.c._send("ordinary", "uses this hour's budget"))
        self.mails.clear()
        qid = self.pending_halt()
        self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertEqual(self.mails, [])
        self.assertNotIn("halt", self.c._read("notices.json", {}))
        self.assertIs(self.read_state("questions.json")[qid]["delivered"], False)
        with self.subTest(check="no work before budget reset"):
            self.assert_no_work()
        self.now += timedelta(minutes=61)  # Budget resets well before 12h.
        self.assertEqual(bootstrap.main(["run"]), 0)
        with self.subTest(check="delivery after budget reset"):
            self.assertEqual(len(self.mails), 1)
            self.assertIn("tamper", " ".join(self.mails[0]).lower())
            self.assertIs(self.read_state("questions.json")[qid]["delivered"], True)
        with self.subTest(check="no work after budget reset"):
            self.assert_no_work()
        self.inbox.assert_not_called()


class ReviewRoundTwoReplyTests(Harness):
    def test_R32_ask_persists_halt_for_failed_delivery(self):
        c = self.make_conductor()
        (self.state / "KILL").touch()
        c.mailer = Mock(side_effect=OSError("fake SMTP failure"))
        qid = c._ask("tamper", "tamper halt", "queue changed", halt=True)
        question = json.loads((self.state / "questions.json").read_text(encoding="utf-8"))[qid]
        self.assertIs(question.get("halt"), True)
        self.assertIs(question["delivered"], False)

    def test_R33_clean_reply_removes_wrapped_and_outlook_quotes(self):
        bodies = [
            "Yes, continue\n\nOn Tue, Sep 29, 2026 at 1:15 PM Forge <x@gmail.com>\nwrote:\n> reply STOP to stop",
            "Yes, continue\n\n-----Original Message-----\nFrom: x\nSent: y\nreply STOP",
            "Yes, continue\n________________________________\nFrom: Forge\nSent: today\nTo: Ben\nSubject: [Forge] conductor started\n\nTo stop everything: reply STOP",
            "Yes, continue\nFrom: Forge <x@gmail.com>\nDate: today\nTo: Ben\n\nreply STOP",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(bootstrap.clean_reply(body), "Yes, continue")

    def test_R33_clean_reply_keeps_from_line_without_header_block(self):
        self.assertEqual(bootstrap.clean_reply("From: my notes, keep going"),
                         "From: my notes, keep going")

    def test_R33_outlook_start_notice_quote_does_not_kill(self):
        c = self.make_conductor()
        self.messages.append({"from": c.owner, "subject": "Re: [Forge] conductor started",
                              "body": "Yes, continue\n________________________________\nFrom: Forge\n"
                                      "Sent: today\nTo: Ben\nSubject: [Forge] conductor started\n\n"
                                      "To stop everything: reply STOP"})
        (self.state / "PAUSED").touch()  # Process mail, then stop before task work.
        status = c.step()
        self.assertFalse((self.state / "KILL").exists())
        self.assertEqual(status, "paused")


class R33LineEndingTests(unittest.TestCase):
    def test_R33_on_wrote_normalizes_mail_line_endings(self):
        """R33: normalize CRLF and lone CR before a wrapped On ... wrote: quote."""
        for ending in ("\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                body = ending.join([
                    "Yes, continue", "", "On Tue, Sep 29, 2026 at 1:15 PM Forge <x@gmail.com>",
                    "wrote:", "To stop everything: reply STOP", "",
                ])
                self.assertEqual(bootstrap.clean_reply(body), "Yes, continue")

    def test_R33_original_message_normalizes_mail_line_endings(self):
        """R33: normalize CRLF and lone CR before an Original Message separator."""
        for ending in ("\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                body = ending.join([
                    "Yes, continue", "", "-----Original Message-----",
                    "To stop everything: reply STOP", "",
                ])
                self.assertEqual(bootstrap.clean_reply(body), "Yes, continue")

    def test_R33_underscores_normalizes_mail_line_endings(self):
        """R33: normalize CRLF and lone CR before an underscores quote separator."""
        for ending in ("\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                body = ending.join([
                    "Yes, continue", "________________________________",
                    "To stop everything: reply STOP", "",
                ])
                self.assertEqual(bootstrap.clean_reply(body), "Yes, continue")

    def test_R33_from_headers_normalizes_mail_line_endings(self):
        """R33: normalize CRLF and lone CR before From plus Sent, Date, or To headers."""
        for ending in ("\r\n", "\r"):
            for header in ("Sent: today", "Date: today", "To: Ben"):
                with self.subTest(ending=repr(ending), header=header):
                    body = ending.join([
                        "Yes, continue", "From: Forge <x@gmail.com>", header, "",
                        "To stop everything: reply STOP", "",
                    ])
                    self.assertEqual(bootstrap.clean_reply(body), "Yes, continue")


class R33SMTPReplyTests(Harness):
    reader = ReviewInboxTests.reader

    def test_R33_smtp_outlook_quote_through_gmail_inbox_does_not_kill(self):
        """R33: an SMTP reply delivered after baseline cannot STOP via its Outlook quote."""
        from email.message import EmailMessage
        from email.policy import SMTP

        c = self.make_conductor()
        server = FakePeekIMAP()
        read = self.reader(server)
        self.assertEqual(read(), [])

        msg = EmailMessage()
        msg["From"] = c.owner
        msg["To"] = c.owner
        msg["Subject"] = "Re: [Forge] conductor started"
        msg["Message-ID"] = "<r33-smtp-reply@example.com>"
        msg.set_content(
            "Yes, continue\n________________________________\nFrom: Forge\n"
            "Sent: today\nTo: Ben\nSubject: [Forge] conductor started\n\n"
            "To stop everything: reply STOP\n"
        )
        raw = msg.as_bytes(policy=SMTP)
        self.assertIn(b"Yes, continue\r\n________________________________\r\n", raw)
        server.messages.append(raw)

        messages = read()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from"], c.owner)
        self.assertEqual(messages[0]["subject"], "Re: [Forge] conductor started")
        self.assertIn("To stop everything: reply STOP", messages[0]["body"])
        c.inbox = Mock(return_value=messages)
        (self.state / "PAUSED").touch()  # Process the real decoded reply before task work.
        status = c.step()
        c.inbox.assert_called_once_with()
        self.assertFalse((self.state / "KILL").exists())
        self.assertEqual(status, "paused")


class ReviewRoundFourConductorTests(Harness):
    read_state = LiveRunTests.read_state
    gate = ReviewMailTests.gate

    def test_R35_stop_persists_gate_reply_and_replays_after_kill_cleared(self):
        """A consumed inbox batch survives STOP until the owner clears KILL."""
        c = self.make_conductor()
        qid, subject = self.gate(c)
        reply = {"from": c.owner, "subject": subject, "body": "y"}
        c.inbox = Mock(side_effect=[[
            {"from": c.owner, "subject": "STOP", "body": ""}, reply,
        ], []])
        self.assertEqual(c.step(), "killed")
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "open")
        pending = self.state / "inbox_pending.json"
        self.assertTrue(pending.exists(), "STOP must persist the rest of the batch")
        self.assertEqual(self.read_state("inbox_pending.json"), [reply])

        (self.state / "KILL").unlink()
        (self.state / "PAUSED").touch()  # Isolate mail processing from agent work.
        self.assertEqual(c.step(), "paused")
        self.assertEqual(c.inbox.call_count, 2)
        self.assertIn(["pr", "merge", "7", "--merge", "--delete-branch"], self.gh_calls)
        self.assertEqual(self.read_state("questions.json")[qid]["status"], "answered")
        self.assertFalse(json.loads(pending.read_text(encoding="utf-8")) if pending.exists() else [])

    def test_R35_answer_error_is_logged_and_next_reply_is_processed(self):
        """An individual answer failure cannot prevent the following gate merge."""
        c = self.make_conductor()
        first_id, first_subject = self.gate(c)
        second_id, second_subject = self.gate(c)
        c.inbox = Mock(return_value=[
            {"from": c.owner, "subject": first_subject, "body": "y"},
            {"from": c.owner, "subject": second_subject, "body": "y"},
        ])
        (self.state / "PAUSED").touch()
        original_answer = c._answer
        calls = []

        def fail_first(qid, body, code):
            calls.append(qid)
            if len(calls) == 1:
                raise RuntimeError("R35 fake answer failure")
            return original_answer(qid, body, code)

        with patch.object(c, "_answer", side_effect=fail_first):
            self.assertEqual(c.step(), "paused")
        self.assertEqual(calls, [first_id, second_id])
        self.assertIn("R35 fake answer failure", (self.state / "errors.log").read_text(encoding="utf-8"))
        questions = self.read_state("questions.json")
        self.assertEqual(questions[first_id]["status"], "open")
        self.assertEqual(questions[second_id]["status"], "answered")
        self.assertIn(["pr", "merge", "7", "--merge", "--delete-branch"], self.gh_calls)

    def test_R35_pending_messages_are_bounded_and_bodies_cleaned(self):
        """STOP retains a bounded batch with cleaned, capped reply bodies."""
        c = self.make_conductor()
        replies = [{"from": c.owner, "subject": f"Re: [Forge] reply {i}",
                    "body": f"reply {i}\n> quoted-secret\n" + "x" * 5000 +
                            "\nOn Tuesday Ben wrote:\nold-secret"} for i in range(60)]
        c.inbox = Mock(return_value=[{"from": c.owner, "subject": "STOP", "body": ""}] + replies)
        self.assertEqual(c.step(), "killed")
        pending = self.state / "inbox_pending.json"
        self.assertTrue(pending.exists(), "STOP must retain pending replies")
        messages = self.read_state("inbox_pending.json")
        self.assertGreater(len(messages), 0)
        self.assertLessEqual(len(messages), 50)
        self.assertEqual(len({m["subject"] for m in messages}), len(messages))
        originals = {m["subject"]: m for m in replies}
        for message in messages:
            with self.subTest(subject=message["subject"]):
                self.assertIn(message["subject"], originals)
                self.assertTrue(message["body"].startswith("reply "))
                self.assertLessEqual(len(message["body"]), 2000)
                self.assertNotIn("quoted-secret", message["body"])
                self.assertNotIn("old-secret", message["body"])


class ReviewRoundFourReaderTests(Harness):
    reader = ReviewInboxTests.reader
    read_state = LiveRunTests.read_state

    def test_R36_unknown_charset_and_undecodable_reply_do_not_lose_batch(self):
        """Bad decoding is isolated and all three new Message-IDs are persisted."""
        import email

        server = FakePeekIMAP()
        read = self.reader(server)
        self.assertEqual(read(), [])
        bad_id, unknown_id, good_id = (
            "<r36-bad@example.com>", "<r36-unknown@example.com>", "<r36-good@example.com>",
        )
        server.add(bad_id, "fake undecodable payload")
        bad_raw = server.messages[-1]
        server.messages.append(
            b"From: ben@example.com\r\nTo: ben@example.com\r\n"
            b"Subject: Re: [Forge Q-gate-1 abcdefgh] ready\r\n"
            b"Message-ID: <r36-unknown@example.com>\r\n"
            b'Content-Type: text/plain; charset="x-unknown-forge"\r\n'
            b"Content-Transfer-Encoding: 8bit\r\n\r\nProceed caf\xc3\xa9 \xff\r\n"
        )
        server.add(good_id, "Good reply after both failures")
        parse = email.message_from_bytes
        failures = []

        def decode(raw, *args, **kwargs):
            # A deterministic fake decoding failure; headers remain readable so
            # the bad message has a usable ID and must not be retried forever.
            if raw == bad_raw:
                failures.append(bad_id)
                raise ValueError("R36 fake undecodable message")
            return parse(raw, *args, **kwargs)

        with patch("email.message_from_bytes", side_effect=decode):
            messages = read()
            self.assertEqual([m["message_id"] for m in messages], [unknown_id, good_id])
            self.assertEqual(messages[0]["body"].strip(), "Proceed caf\u00e9 \ufffd")
            self.assertEqual(messages[1]["body"].strip(), "Good reply after both failures")
            self.assertTrue(all(m["from"] == "ben@example.com" for m in messages))
            self.assertTrue({bad_id, unknown_id, good_id} <= set(self.read_state("inbox_seen.json")))
            self.assertEqual(read(), [])
            self.assertEqual(self.reader(server)(), [])
        self.assertEqual(failures, [bad_id])
        self.assertEqual(server.stores, [])

    def test_R36_fetch_failure_does_not_block_other_replies_or_saved_progress(self):
        """A failed body fetch preserves other replies and is retried next read."""
        server = FakePeekIMAP()
        read = self.reader(server)
        self.assertEqual(read(), [])
        ids = [f"<r36-fetch-{i}@example.com>" for i in range(3)]
        for i, mid in enumerate(ids):
            server.add(mid, f"reply {i}")
        fetch = server.fetch
        failures = []

        def fail_middle(num, message_parts):
            if int(num) == 2 and message_parts == "(BODY.PEEK[])":
                failures.append(num)
                raise OSError("R36 fake fetch failure")
            return fetch(num, message_parts)

        with patch.object(server, "fetch", side_effect=fail_middle):
            messages = read()
            self.assertEqual([m["message_id"] for m in messages], [ids[0], ids[2]])
            self.assertEqual([m["body"].strip() for m in messages], ["reply 0", "reply 2"])
            seen = set(self.read_state("inbox_seen.json"))
            self.assertTrue({ids[0], ids[2]} <= seen)
            self.assertNotIn(ids[1], seen)
        self.assertEqual(len(failures), 1)
        messages = read()
        self.assertEqual([m["message_id"] for m in messages], [ids[1]])
        self.assertEqual(messages[0]["body"].strip(), "reply 1")
        self.assertTrue(set(ids) <= set(self.read_state("inbox_seen.json")))
        self.assertEqual(read(), [])
        self.assertEqual(self.reader(server)(), [])
        self.assertEqual(server.stores, [])

    def test_R36_empty_fetch_does_not_block_other_replies_or_saved_progress(self):
        """An empty body fetch stays unseen and returns once after recovery."""
        server = FakePeekIMAP()
        read = self.reader(server)
        self.assertEqual(read(), [])
        ids = [f"<r36-empty-{i}@example.com>" for i in range(3)]
        for i, mid in enumerate(ids):
            server.add(mid, f"reply {i}")
        fetch = server.fetch
        empty_fetches = []

        def empty_middle(num, message_parts):
            if int(num) == 2 and message_parts == "(BODY.PEEK[])":
                empty_fetches.append(num)
                return "OK", [None]
            return fetch(num, message_parts)

        with patch.object(server, "fetch", side_effect=empty_middle):
            messages = read()
            self.assertEqual([m["message_id"] for m in messages], [ids[0], ids[2]])
            self.assertEqual([m["body"].strip() for m in messages], ["reply 0", "reply 2"])
            seen = set(self.read_state("inbox_seen.json"))
            self.assertTrue({ids[0], ids[2]} <= seen)
            self.assertNotIn(ids[1], seen)
        self.assertEqual(len(empty_fetches), 1)
        messages = read()
        self.assertEqual([m["message_id"] for m in messages], [ids[1]])
        self.assertEqual(messages[0]["body"].strip(), "reply 1")
        self.assertTrue(set(ids) <= set(self.read_state("inbox_seen.json")))
        self.assertEqual(read(), [])
        self.assertEqual(self.reader(server)(), [])
        self.assertEqual(server.stores, [])


class RealTeamTokenCapTests(Harness):
    def setUp(self):
        super().setUp()
        launch_patch = patch("core.agents.launch", side_effect=AssertionError("unexpected agent launch"))
        self.launch = launch_patch.start()
        self.addCleanup(launch_patch.stop)
        self.cap = 10
        self.clock = lambda: datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
        limits = {"claude_daily_token_cap": 100, "codex_daily_token_cap": self.cap}
        self.c = Conductor(
            self.repo, self.work, self.state, bootstrap.real_team(limits), limits,
            owner_email="ben@example.com", mailer=lambda s, b: self.mails.append((s, b)),
            inbox=lambda: [], gh=self.gh, clock=self.clock, judge_cmds=[], push=False,
        )
        self.agent_runs = []
        for role in Team.__dataclass_fields__:
            run_patch = patch.object(
                getattr(self.c.team, role), "run",
                side_effect=AssertionError(f"unexpected {role} agent call"),
            )
            self.agent_runs.append(run_patch.start())
            self.addCleanup(run_patch.stop)

    def add_codex_usage(self, tokens):
        from core.usage import Meter

        Meter(self.state, self.clock).add("codex", tokens)
        self.assertEqual(self.c.meter.used_today("codex"), tokens)

    def test_R6_claude_provider_identifies_token_caps(self):
        """R6: Claude's provider id matches the meter id used for token caps."""
        from core.agents import ClaudeAgent

        self.assertEqual(ClaudeAgent().provider, "claude")

    def test_R6_codex_provider_identifies_token_caps(self):
        """R6: Codex's provider id matches the meter id used for token caps."""
        from core.agents import CodexAgent

        self.assertEqual(CodexAgent().provider, "codex")

    def test_R6_real_team_detects_exceeded_token_caps(self):
        """R6: token caps apply to real_team members with persisted Codex usage."""
        self.add_codex_usage(self.cap + 1)
        self.assertIs(self.c._capped(), True)
        self.launch.assert_not_called()

    def test_R6_real_team_step_stops_at_token_caps(self):
        """R6: token caps make a real-team conductor step return capped."""
        self.add_codex_usage(self.cap + 1)
        self.assertEqual(self.c.step(), "capped")
        for run in self.agent_runs:
            run.assert_not_called()
        self.launch.assert_not_called()

    def test_R29_real_team_guarded_smoke_respects_token_caps(self):
        """R29: token caps stop guarded smoke before any real-team agent call."""
        self.add_codex_usage(self.cap + 1)
        problems = bootstrap._guarded_smoke(self.c, self.work)
        with self.subTest(check="cap problem"):
            self.assertTrue(any("cap" in problem.lower() for problem in problems), problems)
        for role, run in zip(Team.__dataclass_fields__, self.agent_runs):
            with self.subTest(role=role):
                run.assert_not_called()
        self.launch.assert_not_called()

    def test_R6_real_team_under_token_caps_is_not_capped(self):
        """R6: usage below token caps does not cap a real-team conductor."""
        self.add_codex_usage(self.cap - 1)
        self.assertIs(self.c._capped(), False)
        self.launch.assert_not_called()
class R37PerLaunchCapTests(Harness):
    cap = 10

    def capped_conductor(self, *tasks, agents=None):
        from core.usage import Meter

        c = self.init(*tasks, agents=agents, limits={
            "claude_daily_token_cap": self.cap,
            "codex_daily_token_cap": self.cap,
        })
        # Simulate concurrent provider usage without changing guarded state
        # during an agent call: retain the real Meter in a sibling temp directory.
        c.meter = Meter(self.state.parent / "shared-usage", c.clock)
        runner = patch.object(c, "_run_tests", side_effect=lambda task, cwd=None: (
            (0, "Ran 1 test\nOK", False) if ((cwd or c.wt) / "feat.py").exists()
            else (1, "Ran 1 test\nFAILED (errors=1)", False)
        ))
        runner.start()
        self.addCleanup(runner.stop)
        return c

    def assert_tests_commit_restored(self, c):
        self.assertEqual(bootstrap._git(c.wt, "rev-parse", "HEAD"),
                         c._task("T1")["tests_commit"])
        self.assertEqual(bootstrap._git(c.wt, "status", "--porcelain"), "")
        self.assertFalse((c.wt / "feat.py").exists())
        self.assertTrue((c.wt / "tests/core/test_feat.py").is_file())

    def test_R37_build_reviewer_cap_reopens_without_failure_and_resumes(self):
        """R37: a cap reached inside the builder defers review cleanly and permits retry."""
        def builder(prompt, cwd):
            answer = self.build_feature(prompt, cwd)
            c.meter.add("codex", self.cap + 1)
            return answer

        c = self.capped_conductor(agents={"test_writer": self.write_tests, "builder": builder})
        self.assertEqual(c.step(), "worked")
        c._update("T1", fails_since=1, notes=["earlier failure"], fail_signatures=["earlier-signature"])
        before = c._task("T1")
        self.assertFalse(c._capped())
        result = c.step()
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertGreater(c.meter.used_today("codex"), self.cap)
        self.assertFalse((self.state / "KILL").exists())
        with self.subTest(check="reviewer not launched"):
            self.assertEqual(c.team.reviewer.prompts, [])
        with self.subTest(check="capped result"):
            self.assertEqual(result, "capped")
        with self.subTest(check="contract reopened"):
            self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")
        with self.subTest(check="no failure recorded"):
            after = c._task("T1")
            for field in ("status", "fails_since", "notes", "fail_signatures"):
                self.assertEqual(after[field], before[field], field)
        with self.subTest(check="worktree restored"):
            self.assert_tests_commit_restored(c)

        c.limits["codex_daily_token_cap"] = 1000
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")  # Deferred drift check after successful review.
        self.assertEqual(c._task("T1")["status"], "done")
        self.assertEqual(c._ledger().contracts()["T1"]["status"], "done")
        self.assertEqual(len(c.team.builder.prompts), 2)
        self.assertEqual(len(c.team.reviewer.prompts), 1)
        self.assertTrue({"feat.py", "tests/core/test_feat.py"} <= self.branch_files())

    def test_R37_builder_cap_releases_claim_and_resets_worktree(self):
        """R37: a builder capped before launch releases its claim and keeps tests_ok."""
        c = self.capped_conductor(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        self.assertEqual(c.step(), "worked")
        before = c._task("T1")
        (c.wt / "stray.txt").write_text("unfinished attempt", encoding="utf-8")
        c.meter.add("claude", self.cap + 1)
        with patch.object(Conductor, "_capped", return_value=False):
            result = c.step()
        with self.subTest(check="builder not launched"):
            self.assertEqual(c.team.builder.prompts, [])
        with self.subTest(check="capped result"):
            self.assertEqual(result, "capped")
        with self.subTest(check="claim released"):
            self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")
        with self.subTest(check="task unchanged"):
            self.assertEqual(c._task("T1"), before)
        with self.subTest(check="worktree restored"):
            self.assert_tests_commit_restored(c)
            self.assertFalse((c.wt / "stray.txt").exists())

    def test_R37_troubleshooter_cap_preserves_bookkeeping(self):
        """R37: a failing builder crosses Claude's cap before the troubleshooter is due."""
        attempts = []

        def failing_builder(prompt, cwd):
            attempts.append(prompt)
            # No implementation means the fake judge fails on both attempts.
            return '{"status":"done"}', self.cap + 1 if len(attempts) == 2 else 1

        c = self.capped_conductor(agents={"test_writer": self.write_tests, "builder": failing_builder})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")
        before = c._task("T1")
        self.assertEqual(before["fails_since"], 1)
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertFalse(c._capped())
        result = c.step()
        self.assertEqual(len(attempts), 2)
        self.assertGreater(c.meter.used_today("claude"), self.cap)
        with self.subTest(check="troubleshooter not launched"):
            self.assertEqual(c.team.troubleshooter.prompts, [])
        with self.subTest(check="capped result"):
            self.assertEqual(result, "capped")
        with self.subTest(check="no troubleshoot bookkeeping"):
            after = c._task("T1")
            for field in ("troubleshot", "troubleshoots", "trouble_notes"):
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(after["fails_since"], 2)
            self.assertFalse((self.state / "dead_ends.jsonl").exists())

    def test_R37_plan_reviewer_cap_resets_plan_and_keeps_todo(self):
        """R37: a planner crossing Codex's cap leaves no plan or queued child tasks."""
        def planner(prompt, cwd):
            (cwd / "plan.md").write_text("Implement feature value 42 with acceptance tests.\n", encoding="utf-8")
            c.meter.add("codex", self.cap + 1)
            return json.dumps({"tasks": [self.task(id="T2")]}), 1

        c = self.capped_conductor(self.task(kind="plan", plan_file="plan.md"), agents={"planner": planner})
        before = c._task("T1")
        result = c.step()
        self.assertEqual(len(c.team.planner.prompts), 1)
        self.assertGreater(c.meter.used_today("codex"), self.cap)
        self.assertFalse((self.state / "KILL").exists())
        with self.subTest(check="reviewer not launched"):
            self.assertEqual(c.team.reviewer.prompts, [])
        with self.subTest(check="capped result"):
            self.assertEqual(result, "capped")
        with self.subTest(check="plan stays todo"):
            self.assertEqual(c._queue()["tasks"], [before])
        with self.subTest(check="plan reset"):
            self.assertFalse((c.wt / "plan.md").exists())
            self.assertNotIn("plan.md", self.branch_files())
            self.assertEqual(bootstrap._git(c.wt, "status", "--porcelain"), "")

    def test_R37_smoke_stops_later_codex_roles_at_cap(self):
        """R37: smoke usage from the test writer prevents later Codex launches and success."""
        c = self.capped_conductor()
        team, calls = SmokeTests.smoke_team(self)
        for role in Team.__dataclass_fields__:
            getattr(team, role).provider = "codex" if role in ("test_writer", "reviewer") else "claude"
        writer = team.test_writer.script

        def costly_writer(prompt, cwd):
            text, _ = writer(prompt, cwd)
            return text, self.cap + 1

        team.test_writer = FakeAgent(costly_writer, provider="codex")
        c.team = team
        self.assertFalse(c._capped())
        problems = bootstrap._guarded_smoke(c, self.work)
        self.assertEqual(len(team.test_writer.prompts), 1)
        self.assertGreater(c.meter.used_today("codex"), self.cap)
        with self.subTest(check="later Codex roles not launched"):
            self.assertEqual(team.reviewer.prompts, [])
            self.assertNotIn("reviewer", [role for role, cwd in calls])
        with self.subTest(check="cap problem"):
            self.assertTrue(any("token cap reached" in problem.lower() for problem in problems), problems)
        with self.subTest(check="no success stamp"):
            self.assertFalse((self.state / "smoke_ok.json").exists())

    def test_R37_call_raises_capped_without_launch_or_run_record(self):
        """R37: _call raises Capped at or above either provider's cap without recording a run."""
        c = self.capped_conductor()
        for role, provider in (("builder", "claude"), ("reviewer", "codex")):
            for usage in (self.cap, self.cap + 1):
                with self.subTest(provider=provider, usage=usage):
                    c.meter.add(provider, usage - c.meter.used_today(provider))
                    before = set((self.state / "runs").rglob("*"))
                    caught = None
                    try:
                        c._call(role, "Must not launch", None, cwd=self.work)
                    except Exception as exc:
                        caught = exc
                    self.assertEqual(getattr(c.team, role).prompts, [])
                    self.assertEqual(set((self.state / "runs").rglob("*")), before)
                    self.assertIsNotNone(caught, "_call must raise core.bootstrap.Capped")
                    capped = getattr(bootstrap, "Capped", None)
                    self.assertIsNotNone(capped, "core.bootstrap must expose Capped")
                    self.assertIsInstance(caught, capped)


class R38DeferredTroubleshootingTests(Harness):
    cap = 10
    capped_conductor = R37PerLaunchCapTests.capped_conductor
    read_state = LiveRunTests.read_state
    failure_output = "Ran 1 test\nFAILED: R38 discarded prefix:" + "diagnostic detail " * 300 + " LAST FAILURE"
    advice = "R38: implement feat.VALUE as 42 before retrying the judge."

    def defer_troubleshooter(self):
        attempts = []

        def builder(prompt, cwd):
            attempts.append(prompt)
            if len(attempts) == 2:
                c.meter.add("claude", self.cap + 1)
            if len(attempts) > 2:
                return self.build_feature(prompt, cwd)
            return '{"status":"done"}', 1

        def troubleshoot(prompt, cwd):
            return json.dumps({"kind": "fix", "notes": self.advice}), 1

        c = self.capped_conductor(agents={
            "test_writer": self.write_tests, "builder": builder,
            "troubleshooter": troubleshoot,
        })
        runner = patch.object(c, "_run_tests", side_effect=lambda task, cwd=None: (
            (0, "Ran 1 test\nOK", False) if ((cwd or c.wt) / "feat.py").exists()
            else (1, self.failure_output, False)
        ))
        runner.start()
        self.addCleanup(runner.stop)
        self.assertEqual(c.step(), "worked")  # Acceptance tests.
        self.assertEqual(c._task("T1")["status"], "tests_ok")
        self.assertEqual(c.step(), "worked")  # First builder failure.
        self.assertEqual(c._task("T1")["fails_since"], 1)
        self.assertFalse(c._capped())
        self.assertEqual(c.step(), "capped")  # Second failure needs troubleshooting.
        self.assertEqual(len(attempts), 2)
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertFalse((self.state / "KILL").exists())
        return c

    def test_R38_capped_troubleshooter_persists_failure_reason_and_output_tail(self):
        """R38: a cap saves the actual failure and exactly its last 4000 characters."""
        c = self.defer_troubleshooter()
        task = self.read_state("queue.json")["tasks"][0]
        pending = task.get("troubleshoot_pending")
        self.assertIsInstance(pending, dict)
        self.assertEqual(pending["reason"], "judge failed: task tests")
        self.assertEqual(pending["output"], self.failure_output[-4000:])
        self.assertEqual(task["fails_since"], 2)
        self.assertEqual(c.team.troubleshooter.prompts, [])

    def test_R38_resume_runs_only_troubleshooter_then_builder_receives_notes(self):
        """R38: lifting the cap reserves one step for diagnosis before rebuilding."""
        c = self.defer_troubleshooter()
        c.limits["claude_daily_token_cap"] = 1000
        before = {role: len(getattr(c.team, role).prompts) for role in vars(c.team)}
        self.assertEqual(c.step(), "worked")
        for role, count in before.items():
            with self.subTest(role=role):
                self.assertEqual(len(getattr(c.team, role).prompts),
                                 count + (role == "troubleshooter"))
        task = self.read_state("queue.json")["tasks"][0]
        self.assertFalse(task.get("troubleshoot_pending"))
        self.assertIn(self.advice, task["trouble_notes"])
        self.assertIn(self.failure_output[-4000:], c.team.troubleshooter.prompts[-1])
        self.assertEqual(c.step(), "worked")
        self.assertEqual(len(c.team.builder.prompts), before["builder"] + 1)
        self.assertIn(self.advice, c.team.builder.prompts[-1])
        self.assertEqual(len(c.team.troubleshooter.prompts), 1)

    def test_R38_repeated_cap_retains_pending_without_builder_attempt(self):
        """R38: another capped retry cannot discard diagnosis or run the builder."""
        c = self.defer_troubleshooter()
        expected = {"reason": "judge failed: task tests", "output": self.failure_output[-4000:]}
        c.limits["claude_daily_token_cap"] = 1000
        c.meter.add("claude", 1001)
        self.assertEqual(c.step(), "capped")
        self.assertEqual(len(c.team.builder.prompts), 2)
        self.assertEqual(c.team.troubleshooter.prompts, [])
        self.assertEqual(self.read_state("queue.json")["tasks"][0].get("troubleshoot_pending"), expected)


class R39SmokeCleanupTests(Harness):
    smoke_team = SmokeTests.smoke_team

    def busy_smoke_folder(self, guarded):
        import shutil

        team, calls = self.smoke_team()
        c = self.make_conductor() if guarded else None
        if c is not None:
            c.team = team
        remove = shutil.rmtree
        attempts = []

        def busy_builder(path, *args, **kwargs):
            if Path(path).name.startswith("forge-smoke-builder-"):
                attempts.append(Path(path))
                raise PermissionError("R39 fake child process still holds this folder")
            return remove(path, *args, **kwargs)

        with patch("shutil.rmtree", side_effect=busy_builder), patch("time.sleep") as sleep:
            problems = (bootstrap._guarded_smoke(c, self.work) if guarded
                        else bootstrap.smoke(team, self.work))
        with self.subTest(check="cleanup failure is not a smoke problem"):
            self.assertEqual(problems, [])
        with self.subTest(check="all fake agents pass"):
            self.assertCountEqual([role for role, cwd in calls], list(vars(team)))
        with self.subTest(check="five attempts at the same folder"):
            self.assertEqual(len(attempts), 5)
            self.assertEqual(len(set(attempts)), 1)
        with self.subTest(check="two seconds between attempts"):
            self.assertEqual([call.args for call in sleep.call_args_list], [(2,)] * 4)
        self.assertTrue(attempts[0].is_dir())
        if guarded:
            with self.subTest(check="conductor logs the leftover folder"):
                log = self.state / "errors.log"
                self.assertTrue(log.is_file(), "cleanup warning must be logged")
                self.assertIn(attempts[0].name, log.read_text(encoding="utf-8"))

    def test_R39_plain_smoke_tolerates_busy_folder_after_five_attempts(self):
        """R39: exhausted deletion retries do not make passing smoke agents fail."""
        self.busy_smoke_folder(guarded=False)

    def test_R39_guarded_smoke_logs_busy_folder_without_failing(self):
        """R39: guarded smoke logs the retained folder and still returns no problems."""
        self.busy_smoke_folder(guarded=True)

    def test_R39_smoke_sweeps_previous_run_folder_before_first_agent(self):
        """R39: old forge-smoke folders are swept before any new smoke agent runs."""
        stale = self.work / "forge-smoke-old-xyz"
        stale.mkdir()
        (stale / "leftover.txt").write_text("old smoke run", encoding="utf-8")
        team, calls = self.smoke_team()
        stale_at_launch = []
        for role in vars(team):
            agent = getattr(team, role)
            script = agent.script

            def observe(prompt, cwd, script=script):
                stale_at_launch.append(stale.exists())
                return script(prompt, cwd)

            agent.script = observe
        self.assertEqual(bootstrap.smoke(team, self.work), [])
        self.assertEqual(len(calls), 6)
        self.assertEqual(stale_at_launch, [False] * 6)
        self.assertFalse(stale.exists())


class R40PausedRunTests(Harness):
    def setUp(self):
        super().setUp()
        # Use the R31/R32 main fixture pattern: real temporary state and a
        # clock-injecting initializer, with every external dependency faked.
        self.state = self.repo / "state" / "bootstrap"
        self.state.mkdir(parents=True)
        self.now = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
        self.make_conductor()
        self.c.clock = lambda: self.now
        self.c._write("smoke_ok.json", {"at": (self.now - timedelta(hours=25)).isoformat()})
        self.inbox = Mock(return_value=[])
        real_init = Conductor.__init__

        def timed_init(conductor, *args, **kwargs):
            kwargs["clock"] = lambda: self.now
            real_init(conductor, *args, **kwargs)

        patches = {
            "module_file": patch("core.bootstrap.__file__", str(self.repo / "core" / "bootstrap.py")),
            "limits": patch("core.agents.load_limits", return_value=self.c.limits),
            "team": patch("core.bootstrap.real_team", return_value=self.team),
            "mailer": patch("core.bootstrap.gmail_mailer", return_value=self.c.mailer),
            "inbox_factory": patch("core.bootstrap.gmail_inbox", return_value=self.inbox),
            "gh": patch("core.bootstrap.gh_cli", return_value=self.gh),
            "init": patch.object(Conductor, "__init__", new=timed_init),
            "lock": patch("core.bootstrap.acquire_lock", side_effect=lambda state: Mock()),
            "guarded_smoke": patch("core.bootstrap._guarded_smoke", return_value=[]),
            "smoke": patch("core.bootstrap.smoke", return_value=[]),
            "run": patch.object(Conductor, "run", return_value="idle"),
        }
        self.cli = {}
        for name, patcher in patches.items():
            self.cli[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.paused = self.state / "PAUSED"
        self.paused.touch()
        self.assertTrue(bootstrap._smoke_stale(self.state, self.now))

    assert_no_work = ReviewRoundTwoRunTests.assert_no_work

    def test_R40_cli_waits_for_unpause_before_stale_smoke_and_run(self):
        """R40: poll the inbox each minute while paused, then smoke before running."""
        events = []
        inbox_counts = []
        self.inbox.side_effect = lambda: events.append("inbox") or []

        def resume_after_three_sleeps(seconds):
            self.assertEqual(seconds, 60)
            self.assertTrue(self.paused.exists())
            self.assert_no_work()
            inbox_counts.append(self.inbox.call_count)
            self.now += timedelta(seconds=seconds)
            if len(inbox_counts) == 3:
                self.paused.unlink()  # Simulate an answer clearing the pause.
                events.append("unpaused")
            self.assertLessEqual(len(inbox_counts), 3, "main must leave the pause wait")

        def smoke_after_unpause(*args, **kwargs):
            self.assertFalse(self.paused.exists(), "smoke launched while PAUSED exists")
            events.append("smoke")
            return []

        def run_after_smoke(*args, **kwargs):
            self.assertFalse(self.paused.exists())
            self.assertIn("smoke", events)
            events.append("run")
            return "idle"

        self.cli["guarded_smoke"].side_effect = smoke_after_unpause
        self.cli["run"].side_effect = run_after_smoke
        with patch("core.bootstrap.time.sleep", side_effect=resume_after_three_sleeps) as sleep:
            self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertEqual(sleep.call_count, 3)
        self.assertGreaterEqual(inbox_counts[0], 1)
        self.assertTrue(all(later > earlier for earlier, later in zip(inbox_counts, inbox_counts[1:])),
                        "the inbox must be read again between waits")
        self.assertEqual(events[0], "inbox")
        self.assertLess(events.index("unpaused"), events.index("smoke"))
        self.assertLess(events.index("smoke"), events.index("run"))
        self.cli["guarded_smoke"].assert_called_once()
        self.cli["run"].assert_called_once()

    def test_R40_cli_kill_during_pause_exits_without_smoke_or_run(self):
        """R40: KILL appearing during the pause wait exits main successfully without work."""
        def kill_during_wait(seconds):
            self.assertEqual(seconds, 60)
            self.assertTrue(self.paused.exists())
            self.assert_no_work()
            self.assertFalse((self.state / "KILL").exists(), "main must exit after KILL")
            (self.state / "KILL").touch()

        with patch("core.bootstrap.time.sleep", side_effect=kill_during_wait) as sleep:
            self.assertEqual(bootstrap.main(["run"]), 0)
        self.assert_no_work()
        sleep.assert_called_once_with(60)
        self.assertTrue((self.state / "KILL").exists())
        self.assertTrue(self.paused.exists())
        self.inbox.assert_called_with()

    def test_R40_cli_writes_heartbeat_while_waiting(self):
        """R40: a heartbeat is written during each paused wait before any work launches."""
        heartbeat = self.state / "conductor.heartbeat"
        observed = []
        self.assertFalse(heartbeat.exists())

        def observe_wait(seconds):
            self.assertEqual(seconds, 60)
            self.assertTrue(self.paused.exists())
            self.assert_no_work()
            self.assertTrue(heartbeat.is_file(), "paused main must write its heartbeat")
            observed.append(heartbeat.read_text(encoding="utf-8"))
            self.assertTrue(observed[-1].strip())
            self.assertLessEqual(len(observed), 2, "main must exit after KILL")
            if len(observed) == 2:
                (self.state / "KILL").touch()
            else:
                heartbeat.unlink()  # The next wait must write it again.
                self.now += timedelta(seconds=seconds)

        with patch("core.bootstrap.time.sleep", side_effect=observe_wait):
            self.assertEqual(bootstrap.main(["run"]), 0)
        self.assertEqual(len(observed), 2, "main must maintain a heartbeat while paused")
        self.assert_no_work()
