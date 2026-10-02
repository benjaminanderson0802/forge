"""Owner mail replies, including refusals that leave approval questions open."""
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest.mock import patch

from core.bootstrap import Journal, NOTE_CAP, NOTES_KEEP, gmail_inbox
from drills.run_drills import _channel_conductor


def mail(mid, subject, body, sender="ben@example.com", outgoing=False):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = mid
    if outgoing:
        msg["X-Forge-Outgoing"] = "1"
    msg.set_content(body)
    msg.add_alternative("<p>This HTML is not the reply.</p>", subtype="html")
    return msg.as_bytes()


class FakeIMAP:
    """Two sequence slots, replaceable between reads; all operations recorded."""
    def __init__(self):
        self.messages = [mail("<old-1>", "[Forge] old", "old"),
                         mail("<old-2>", "STOP", "old")]
        self.calls = []

    def login(self, owner, password):
        self.calls.append(("login", owner, password))
        return "OK", []

    def select(self, mailbox):
        self.calls.append(("select", mailbox))
        return "OK", [b"2"]

    def search(self, charset, crit):
        self.calls.append(("search", charset, crit))
        return "OK", [b"1 2"]

    def fetch(self, n, spec):
        self.calls.append(("fetch", n, spec))
        raw = self.messages[int(n) - 1]
        if "HEADER" in spec:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"n (...)", raw), b")"]

    def store(self, *args):
        self.calls.append(("store", *args))
        raise AssertionError("The inbox reader must never change read flags")

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []


class RepliesTests(unittest.TestCase):
    def setUp(self):
        self.c, self.mails, self.inbox, self.agent_calls = _channel_conductor(limits=None)
        self.addCleanup(shutil.rmtree, self.c.state.parent)
        self.now = datetime(2026, 10, 2, 17, tzinfo=timezone.utc)
        self.c.clock = lambda: self.now
        self.gh_calls = []

        def gh(args):
            self.gh_calls.append(list(args))
            return 0, ""

        self.c.gh = gh

    def questions(self):
        return json.loads((self.c.state / "questions.json").read_text(encoding="utf-8"))

    def tag(self, qid):
        return f"Re: [Forge Q-{qid} {self.questions()[qid]['code']}] reply"

    def reply(self, qid, body, sender="ben@example.com"):
        self.inbox.clear()
        self.inbox.append({"from": sender, "subject": self.tag(qid), "body": body})
        try:
            self.c._handle_inbox()
        finally:
            self.inbox.clear()

    def reader(self):
        imap = FakeIMAP()
        reader = gmail_inbox("ben@example.com", self.c.state,
                             imap_factory=lambda: imap, clock=self.c.clock)
        self.assertEqual(reader(), [])  # Prime before delivering actionable replies.
        return imap, reader

    def assert_approved(self, qid):
        self.assertEqual(self.gh_calls, [
            ["pr", "edit", "7", "--add-label", "human-approved"],
            ["pr", "merge", "7", "--merge", "--delete-branch"],
        ])
        self.assertEqual(self.questions()[qid]["status"], "answered")

    def test_imap_peeks_primes_decodes_and_remembers_message_ids(self):
        imap, reader = self.reader()
        seen = self.c.state / "inbox_seen.json"
        self.assertEqual(set(json.loads(seen.read_text())), {"<old-1>", "<old-2>"})
        self.assertTrue(all("HEADER" in call[2] for call in imap.calls if call[0] == "fetch"))
        subject = "Re: [Forge Q-blocked-1 abcdefgh] caf\u00e9"
        imap.messages = [mail("<new>", subject, "Try plan B."),
                         mail("<outgoing>", "[Forge] sent", "y", outgoing=True)]
        self.assertEqual(reader(), [{"from": "ben@example.com", "subject": subject,
                                     "body": "Try plan B.\n", "message_id": "<new>",
                                     "outgoing": False}])
        self.assertEqual(reader(), [])
        # Dedupe survives constructing a new reader, not just a closure cache.
        self.assertEqual(gmail_inbox("ben@example.com", self.c.state,
                                    imap_factory=lambda: imap, clock=self.c.clock)(), [])
        self.assertIn(("login", "ben@example.com", ""), imap.calls)
        self.assertIn(("select", "INBOX"), imap.calls)
        searches = [call for call in imap.calls if call[0] == "search"]
        self.assertTrue(all(call[1] is None for call in searches))
        self.assertTrue(any('SUBJECT "[Forge"' in call[2] for call in searches))
        self.assertTrue(any('SUBJECT "STOP"' in call[2] for call in searches))
        fetches = [call for call in imap.calls if call[0] == "fetch"]
        self.assertTrue(fetches)
        self.assertTrue(all("BODY.PEEK[" in call[2] and "BODY[" not in call[2] for call in fetches))
        self.assertFalse(any(call[0] == "store" for call in imap.calls))
        self.assertEqual(sum(call[0] == "logout" for call in imap.calls), 4)

    def test_imap_reply_requires_matching_open_question_and_code(self):
        target = self.c._ask("blocked", "Task blocked", "details")
        other = self.c._ask("replan", "Plan blocked", "details")
        imap, self.c.inbox = self.reader()
        before = self.questions()
        wrong = f"Re: [Forge Q-{target} {before[other]['code']}] reply"
        imap.messages[0] = mail("<wrong-code>", wrong, "try plan B")
        self.c._handle_inbox()
        self.assertEqual(self.questions(), before)
        imap.messages[0] = mail("<valid>", self.tag(target), "try plan B")
        self.c._handle_inbox()
        answered = self.questions()
        self.assertEqual(answered[target]["status"], "answered")
        self.assertEqual(answered[target]["answer"], "try plan B")
        self.assertEqual(answered[other], before[other])
        imap.messages[0] = mail("<closed>", self.tag(target), "replace previous answer")
        self.c._handle_inbox()
        self.assertEqual(self.questions(), answered)

    def test_foreign_and_ambiguous_senders_cannot_answer_or_stop(self):
        qid = self.c._ask("gate", "layer-1 is ready", "report", pr="7")
        imap, self.c.inbox = self.reader()
        before = self.questions()
        for i, sender in enumerate(("mallory@evil.example",
                                     '"ben@example.com" <mallory@evil.example>',
                                     "ben@example.com, mallory@evil.example")):
            with self.subTest(sender=sender):
                imap.messages = [mail(f"<foreign-{i}>", self.tag(qid), "y", sender),
                                 mail(f"<stop-{i}>", "STOP", "STOP", sender)]
                self.c._handle_inbox()
                self.assertEqual(self.questions(), before)
                self.assertFalse((self.c.state / "KILL").exists())
                self.assertEqual(self.gh_calls, [])
        imap.messages[0] = mail("<owner-stop>", "STOP", "STOP", "Ben <ben@example.com>")
        self.c._handle_inbox()
        self.assertTrue((self.c.state / "KILL").is_file())
        self.assertEqual(self.c.step(), "killed")
        self.assertEqual(self.agent_calls, [])

    def test_gate_y_and_yes_label_then_merge(self):
        for body in ("y", "yes"):
            with self.subTest(body=body):
                self.gh_calls.clear()
                qid = self.c._ask("gate", "layer-1 is ready", "report", pr="7")
                self.reply(qid, body)
                self.assert_approved(qid)

    def test_gate_refusals_are_recorded_and_later_y_can_approve(self):
        for body in ("n", "No.", "NO!, please wait\nI need more time."):
            with self.subTest(body=body):
                self.gh_calls.clear()
                qid = self.c._ask("gate", "layer-1 is ready", "report", pr="7")
                with patch.object(self.c, "_log", wraps=self.c._log) as log:
                    self.reply(qid, body)
                q = self.questions()[qid]
                self.assertEqual(q["status"], "open")
                self.assertEqual(q.get("replies"), [body])
                self.assertEqual(q.get("declined_at"), self.now.isoformat())
                self.assertNotIn("answer", q)
                self.assertNotIn("closed_at", q)
                self.assertEqual(self.gh_calls, [])
                log.assert_any_call(f"Q-{qid} declined by the owner: nothing merged")
                self.reply(qid, "y")
                self.assert_approved(qid)

    def test_merge_refusal_does_not_unblock_but_later_retry_does(self):
        for body in ("n", "No."):
            with self.subTest(body=body):
                qid = self.c._ask("merge", "Merge blocked", "details")
                with patch.object(Journal, "unblock") as unblock:
                    with patch.object(self.c, "_log", wraps=self.c._log) as log:
                        self.reply(qid, body)
                    unblock.assert_not_called()
                    q = self.questions()[qid]
                    self.assertEqual(q["status"], "open")
                    self.assertEqual(q.get("replies"), [body])
                    self.assertEqual(q.get("declined_at"), self.now.isoformat())
                    self.assertNotIn("answer", q)
                    self.assertEqual(self.gh_calls, [])
                    log.assert_any_call(f"Q-{qid} declined by the owner: nothing merged")
                    self.reply(qid, "retry now")
                    unblock.assert_called_once_with(qid, "retry now")
                self.assertEqual(self.questions()[qid]["status"], "answered")
                self.assertEqual(self.questions()[qid]["answer"], "retry now")

    def test_refusal_history_keeps_last_notes_and_caps_full_body(self):
        for kind in ("gate", "merge"):
            with self.subTest(kind=kind), patch.object(Journal, "unblock") as unblock:
                qid = self.c._ask(kind, "Waiting", "details", pr="7")
                bodies = []
                for i in range(NOTES_KEEP + 2):
                    self.now += timedelta(seconds=1)
                    body = f"  n reason {i}\n" + "x" * NOTE_CAP + "  "
                    bodies.append(body.strip()[:NOTE_CAP])
                    # Direct entry also checks the cap without clean_reply doing it first.
                    self.c._answer(qid, body, self.questions()[qid]["code"])
                q = self.questions()[qid]
                self.assertEqual(q.get("replies"), bodies[-NOTES_KEEP:])
                self.assertEqual(q.get("declined_at"), self.now.isoformat())
                self.assertEqual(q["status"], "open")
                self.assertEqual(self.gh_calls, [])
                unblock.assert_not_called()

    def test_other_gate_replies_are_ignored_and_other_merge_replies_retry(self):
        gate = self.c._ask("gate", "layer-1 is ready", "report", pr="7")
        before = self.questions()[gate]
        for body in ("", "maybe", "not yet", "nope"):
            with self.subTest(body=body):
                self.reply(gate, body)
                self.assertEqual(self.questions()[gate], before)
                merge = self.c._ask("merge", "Merge blocked", "details")
                with patch.object(Journal, "unblock") as unblock:
                    self.reply(merge, body)
                    unblock.assert_called_once_with(merge, body)
                self.assertEqual(self.questions()[merge]["status"], "answered")
        self.assertEqual(self.gh_calls, [])

    def test_free_text_keeps_existing_replan_and_capability_behavior(self):
        replan = self.c._ask("replan", "Plan paused", "details")
        (self.c.state / "PAUSED").write_text("waiting", encoding="utf-8")
        self.reply(replan, "n, use a smaller plan")
        self.assertEqual(self.questions()[replan]["status"], "answered")
        self.assertEqual(self.questions()[replan]["answer"], "n, use a smaller plan")
        self.assertFalse((self.c.state / "PAUSED").exists())
        capability = self.c._ask("capability", "Browser unavailable", "details", capability="browser")
        self.reply(capability, "n, check again later")
        q = self.questions()[capability]
        self.assertEqual(q["status"], "open")
        self.assertEqual(q["replies"], ["n, check again later"])
        self.assertNotIn("declined_at", q)
        self.assertIn("browser", json.loads((self.c.state / "readiness_force.json").read_text()))


if __name__ == "__main__":
    unittest.main()
