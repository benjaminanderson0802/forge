"""R63 evidence for design 6.1/6.2, using real routing and fake mail."""
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from core import channel
from drills.run_drills import _channel_conductor


LOCAL = timezone(timedelta(hours=-5))
LIMITS = {"digest_hour": 8, "quiet_start": 23, "quiet_end": 7,
          "mail_per_hour": 20, "mail_per_day": 50}


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=LOCAL)


class QueueRoutingEvidence(unittest.TestCase):
    def setUp(self):
        self.now = at(2, 12)
        self.c, self.mails, self.inbox, self.agent_calls = self.make_conductor(LIMITS)

    def make_conductor(self, limits):
        result = _channel_conductor(dict(limits))
        c = result[0]
        # Only remove the private temporary directory created by this helper.
        self.addCleanup(shutil.rmtree, c.state.parent)
        c.local_tz = LOCAL
        # Supply UTC to also exercise conversion to the owner's local time.
        c.clock = lambda: self.now.astimezone(timezone.utc)
        return result

    def questions(self):
        return json.loads((self.c.state / "questions.json").read_text(encoding="utf-8"))

    def queue_items(self):
        path = self.c.state / "queue.jsonl"
        self.assertTrue(path.is_file())
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def tag(self, qid):
        return f"[Forge Q-{qid} {self.questions()[qid]['code']}]"

    def test_queue_tracks_open_questions_and_owner_answer(self):
        deadline = "2026-10-02T08:00:00+00:00"
        blocked = self.c._ask("blocked", "Task T1 is blocked", "details",
                              task="T1", deadline=deadline)
        replan = self.c._ask("replan", "Forge paused", "reasons")
        rows = self.queue_items()
        self.assertEqual(len(rows), 2)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {blocked, replan})
        required = {"id", "kind", "question", "default", "deadline", "status",
                    "via", "code", "delivered", "halt"}
        for qid, kind, subject in ((blocked, "blocked", "Task T1 is blocked"),
                                   (replan, "replan", "Forge paused")):
            with self.subTest(kind=kind):
                row = by_id[qid]
                self.assertTrue(required <= row.keys())
                self.assertEqual(row["kind"], kind)
                self.assertEqual(row["question"], subject)
                self.assertEqual(row["default"], channel.default_for(kind))
                self.assertEqual(row["status"], "open")
                self.assertEqual(row["code"], self.questions()[qid]["code"])
                self.assertFalse(row["halt"])
        self.assertEqual(by_id[blocked]["deadline"], deadline)
        self.assertIsNone(by_id[replan]["deadline"])
        self.assertEqual(by_id[blocked]["via"], "digest")
        self.assertFalse(by_id[blocked]["delivered"])
        self.assertEqual(by_id[replan]["via"], "email")
        self.assertTrue(by_id[replan]["delivered"])

        self.inbox.append({"from": "ben@example.com",
                           "subject": "Re: " + self.tag(blocked),
                           "body": "Use the other library"})
        self.c._handle_inbox()
        self.assertEqual(self.questions()[blocked]["status"], "answered")
        self.assertEqual(self.queue_items(), [by_id[replan]])
        self.assertFalse((self.c.state / "queue.jsonl.tmp").exists())

    def test_write_queue_skips_invalid_and_non_open_entries_and_replaces_view(self):
        qid = self.c._ask("blocked", "Task T1 is blocked", "details")
        expected = self.queue_items()
        questions = self.questions()
        questions.update({"null": None, "list": [], "text": "open",
                          "answered": dict(questions[qid], status="answered"),
                          "closed": dict(questions[qid], status="closed"),
                          "missing-status": {"kind": "blocked"}})
        path = self.c.state / "queue.jsonl"
        channel.write_queue(path, questions)
        self.assertEqual(self.queue_items(), expected)
        self.assertFalse(path.with_suffix(".jsonl.tmp").exists())
        channel.write_queue(path, {})
        self.assertEqual(path.read_text(encoding="utf-8"), "")
        self.assertFalse(path.with_suffix(".jsonl.tmp").exists())

    def test_all_and_only_d023_kinds_mail_instantly_at_noon(self):
        kinds = {"gate", "replan", "capability", "spend", "customer", "tamper"}
        self.assertEqual(channel.INSTANT_KINDS, kinds)
        for kind in sorted(kinds):
            with self.subTest(kind=kind):
                before = len(self.mails)
                qid = self.c._ask(kind, "Question about " + kind, "details")
                self.assertEqual(len(self.mails), before + 1)
                subject, body = self.mails[-1]
                self.assertTrue(subject.startswith(self.tag(qid)))
                self.assertTrue(body.endswith("If you don't answer: " + channel.default_for(kind)))
                self.assertTrue(self.questions()[qid]["delivered"])
        for kind in ("blocked", "merge"):
            with self.subTest(kind=kind):
                before = len(self.mails)
                qid = self.c._ask(kind, "Question about " + kind, "details")
                self.c._handle_inbox()
                self.assertEqual(len(self.mails), before)
                question = self.questions()[qid]
                self.assertTrue(question["hold"])
                self.assertTrue(question["digest"])
                self.assertFalse(question["delivered"])
                row = next(row for row in self.queue_items() if row["id"] == qid)
                self.assertEqual(row["via"], "digest")

    def test_policy_off_mails_blocked_question_immediately(self):
        limits = {key: value for key, value in LIMITS.items() if key != "digest_hour"}
        c, mails, _, _ = self.make_conductor(limits)
        qid = c._ask("blocked", "Task T1 is blocked", "details")
        questions = json.loads((c.state / "questions.json").read_text(encoding="utf-8"))
        self.assertEqual(len(mails), 1)
        self.assertTrue(mails[0][0].startswith(f"[Forge Q-{qid} {questions[qid]['code']}]"))
        self.assertTrue(questions[qid]["delivered"])

    def test_daily_digest_at_eight_local_and_once_per_local_day(self):
        self.now = at(2, 7, 59)
        # E4 permits early digests when stalled; keep real runnable work queued.
        self.c._save_queue({"layer": "layer-1", "tasks": [
            {"id": "T2", "kind": "build", "title": "Runnable task", "status": "todo",
             "notes": [], "trouble_notes": [], "fail_signatures": []}]})
        qid = self.c._ask("blocked", "Task T1 is blocked", "details")
        self.c._channel_tick()
        self.assertEqual(self.mails, [])
        self.now = at(2, 8)
        self.c._channel_tick()
        self.assertEqual(len(self.mails), 1)
        subject, body = self.mails[0]
        self.assertTrue(subject.startswith("[Forge] Daily digest"))
        self.assertNotIn("[Forge Q-", subject)
        self.assertIn("Task T1 is blocked", body)
        self.assertIn("If you don't answer: " + channel.default_for("blocked"), body)
        self.assertIn("To answer: send an email with the subject " + self.tag(qid), body)
        for when in (at(2, 8, 30), at(2, 20)):
            self.now = when
            self.c._channel_tick()
            self.assertEqual(len(self.mails), 1)
        self.now = at(3, 8)
        self.c._channel_tick()
        self.assertEqual(len(self.mails), 2)
        self.assertTrue(self.mails[1][0].startswith("[Forge] Daily digest"))
        self.assertIn(self.tag(qid), self.mails[1][1])
        subject, _ = channel.build_digest(
            self.questions(), [], owner="ben@example.com", local_now=self.now,
            mail_used=(0, 0), mail_caps=(20, 50), page_url="http://localhost/")
        self.assertTrue(subject.startswith("[Forge] Daily digest"))
        self.assertNotIn("[Forge Q-", subject)

    def test_quiet_hour_boundaries(self):
        for hour, minute, quiet in ((23, 0, True), (2, 0, True), (6, 59, True),
                                    (7, 0, False), (22, 59, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertIs(channel.is_quiet(at(2, hour, minute), 23, 7), quiet)

    def test_quiet_hours_hold_mail_without_budget_cost_but_allow_emergency(self):
        self.now = at(2, 23, 30)
        gate = self.c._ask("gate", "Approve the layer", "report")
        self.c._ask("blocked", "Task T1 is blocked", "details")
        self.assertEqual(self.mails, [])
        self.assertFalse(self.questions()[gate]["delivered"])
        self.c._channel_tick()
        self.assertEqual(self.mails, [])
        before = self.c._read("mail_log.json", {})
        self.assertEqual(before.get("sent", []), [])
        self.assertIs(self.c._send("[Forge] notice", "body"), False)
        self.assertEqual(self.mails, [])
        self.assertEqual(self.c._read("mail_log.json", {}), before)
        emergency = self.c._ask("tamper", "Forge stopped", "x", halt=True)
        self.assertEqual(len(self.mails), 1)
        self.assertTrue(self.mails[0][0].startswith(self.tag(emergency)))
        self.assertTrue(self.questions()[emergency]["delivered"])
        self.assertFalse(self.questions()[gate]["delivered"])
        self.now = at(3, 7)
        self.c._handle_inbox()
        self.assertEqual(len(self.mails), 2)
        self.assertTrue(self.mails[-1][0].startswith(self.tag(gate)))
        self.assertTrue(self.questions()[gate]["delivered"])


if __name__ == "__main__":
    unittest.main()
