"""Layer 1E: Ben's channel, plain functions (docs/specs/layer-1-design.md §6, D-021 to D-024)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import channel

CHI = timezone(timedelta(hours=-5))


def q(kind, subject="s", body="b", status="open", **extra):
    return {"kind": kind, "status": status, "code": "abcd1234", "subject": subject, "body": body,
            "delivered": False, **extra}


class Routing(unittest.TestCase):
    def test_instant_kinds_follow_d023(self):
        for k in ("gate", "replan", "capability", "spend", "customer", "tamper"):
            self.assertTrue(channel.is_instant(k), k)
        for k in ("blocked", "merge", "anything-else"):
            self.assertFalse(channel.is_instant(k), k)

    def test_every_kind_has_a_default(self):
        for k in ("gate", "replan", "blocked", "merge", "capability", "tamper", "unknown-kind"):
            self.assertTrue(channel.default_for(k).strip(), k)
        self.assertIn("main", channel.default_for("gate"))

    def test_quiet_hours_wrap_midnight(self):
        day = datetime(2026, 9, 30, tzinfo=CHI)
        quiet = [day.replace(hour=h) for h in (23, 0, 3, 6)] + [day.replace(hour=6, minute=59)]
        loud = [day.replace(hour=h) for h in (7, 8, 12, 22)] + [day.replace(hour=22, minute=59)]
        for t in quiet:
            self.assertTrue(channel.is_quiet(t), t)
        for t in loud:
            self.assertFalse(channel.is_quiet(t), t)

    def test_quiet_hours_configurable_and_non_wrapping(self):
        t = datetime(2026, 9, 30, 13, tzinfo=CHI)
        self.assertTrue(channel.is_quiet(t, start=12, end=14))
        self.assertFalse(channel.is_quiet(t, start=14, end=12))
        self.assertFalse(channel.is_quiet(t, start=5, end=5))  # empty window: never quiet

    def test_to_local_uses_given_zone(self):
        utc = datetime(2026, 9, 30, 13, tzinfo=timezone.utc)
        self.assertEqual(channel.to_local(utc, CHI).hour, 8)
        self.assertIsNotNone(channel.to_local(utc).tzinfo)  # system zone when none is given


class Queue(unittest.TestCase):
    def test_items_are_open_questions_with_the_design_fields(self):
        qs = {"blocked-1": q("blocked", "Task T1 is blocked", hold=True, digest=True),
              "gate-2": q("gate", "layer-1 is ready", delivered=True),
              "capability-3": q("capability", "n8n down", hold=True),
              "replan-4": q("replan", status="answered")}
        items = channel.queue_items(qs)
        self.assertEqual([i["id"] for i in items], ["blocked-1", "gate-2", "capability-3"])
        for i in items:
            for key in ("id", "kind", "question", "default", "deadline", "status", "via", "code"):
                self.assertIn(key, i)
            self.assertEqual(i["status"], "open")
        by = {i["id"]: i for i in items}
        self.assertEqual(by["blocked-1"]["via"], "digest")
        self.assertEqual(by["gate-2"]["via"], "email")
        self.assertEqual(by["capability-3"]["via"], "held")
        self.assertEqual(by["blocked-1"]["question"], "Task T1 is blocked")
        self.assertEqual(by["gate-2"]["default"], channel.default_for("gate"))
        self.assertIsNone(by["gate-2"]["deadline"])

    def test_explicit_default_and_deadline_win(self):
        items = channel.queue_items({"x-1": q("blocked", default="skip it", deadline="2026-10-01T08:00:00")})
        self.assertEqual(items[0]["default"], "skip it")
        self.assertEqual(items[0]["deadline"], "2026-10-01T08:00:00")

    def test_write_queue_is_jsonl_and_ignores_junk(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "queue.jsonl"
            channel.write_queue(p, {"a-1": q("blocked"), "bad": "not a dict", "b-2": q("gate")})
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(x)["id"] for x in lines], ["a-1", "b-2"])
            channel.write_queue(p, {})
            self.assertEqual(p.read_text(encoding="utf-8"), "")
            self.assertEqual([x.name for x in Path(d).iterdir()], ["queue.jsonl"])  # no temp file left


class Digest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, 8, 5, tzinfo=CHI)
        self.tasks = [{"id": "T1", "title": "one", "status": "done"},
                      {"id": "T2", "title": "two", "status": "blocked"},
                      {"id": "T3", "title": "three", "status": "todo"}]

    def build(self, qs, **kw):
        args = dict(owner="ben@example.com", local_now=self.now, mail_used=(1, 4), mail_caps=(3, 10),
                    page_url="http://127.0.0.1:8765")
        args.update(kw)
        return channel.build_digest(qs, self.tasks, **args)

    def test_lists_open_questions_with_answer_lines_and_defaults(self):
        qs = {"blocked-1": q("blocked", "Task T2 is blocked: two", "details here", hold=True, digest=True),
              "replan-2": q("replan", "old", status="answered")}
        subject, body = self.build(qs)
        self.assertTrue(subject.startswith("[Forge] "))
        self.assertIn("digest", subject.lower())
        self.assertIn("1 open question", subject)
        self.assertNotIn("[Forge Q-", subject)  # the digest itself is never an answerable question
        self.assertIn("Task T2 is blocked: two", body)
        self.assertIn("details here", body)
        self.assertIn("[Forge Q-blocked-1 abcd1234]", body)
        self.assertIn("mailto:ben@example.com?subject=%5BForge%20Q-blocked-1%20abcd1234%5D", body)
        self.assertIn(channel.default_for("blocked"), body)
        self.assertNotIn("replan-2", body)
        self.assertIn("http://127.0.0.1:8765", body)
        self.assertIn("STOP", body)
        self.assertIn("1 done", body); self.assertIn("1 blocked", body); self.assertIn("1 todo", body)
        self.assertIn("T2", body)
        self.assertIn("4 of 10", body)

    def test_no_questions(self):
        subject, body = self.build({})
        self.assertIn("no open questions", subject.lower())
        self.assertIn("Nothing is waiting", body)

    def test_body_is_capped(self):
        qs = {f"blocked-{i}": q("blocked", "s" * 300, "b" * 5000) for i in range(100)}
        subject, body = self.build(qs)
        self.assertLessEqual(len(body), channel.BODY_CAP)
        self.assertLessEqual(len(subject), 300)
        self.assertIn("100 open questions", subject)
        self.assertNotIn("b" * 1001, body)  # each question body is excerpted


class DropFolder(unittest.TestCase):
    def test_round_trip_and_removal(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "in"
            channel.drop_answer(folder, "gate-1", "abcd1234", "y", "page")
            channel.drop_answer(folder, "blocked-2", "efgh5678", "use the other lib", "page")
            got = channel.take_answers(folder)
            self.assertEqual([(a["qid"], a["code"], a["answer"], a["source"]) for a in got],
                             [("gate-1", "abcd1234", "y", "page"), ("blocked-2", "efgh5678", "use the other lib", "page")])
            self.assertEqual(channel.take_answers(folder), [])
            self.assertEqual(list(folder.iterdir()), [])

    def test_missing_folder_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(channel.take_answers(Path(d) / "nope"), [])

    def test_junk_is_removed_and_never_returned(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "1-bad.json").write_text("{not json", encoding="utf-8")
            (folder / "2-list.json").write_text("[1, 2]", encoding="utf-8")
            (folder / "3-big.json").write_text(json.dumps({"qid": "a", "code": "b", "answer": "x" * 30000}),
                                               encoding="utf-8")
            (folder / "4-missing.json").write_text(json.dumps({"qid": "a"}), encoding="utf-8")
            (folder / "5-tmp.json.tmp").write_text("{}", encoding="utf-8")  # a write in progress: left alone
            (folder / "6-ok.json").write_text(json.dumps({"qid": "a-1", "code": "c", "answer": "n"}),
                                              encoding="utf-8")
            got = channel.take_answers(folder)
            self.assertEqual([(a["qid"], a["answer"], a["source"]) for a in got], [("a-1", "n", "unknown")])
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["5-tmp.json.tmp"])

    def test_limit_per_call_and_answer_cap(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for i in range(60):
                channel.drop_answer(folder, f"q-{i}", "c", "a" * 5000, "page")
            first = channel.take_answers(folder)
            self.assertEqual(len(first), 50)
            self.assertTrue(all(len(a["answer"]) <= 2000 for a in first))
            self.assertEqual(len(channel.take_answers(folder)), 10)

    def test_drop_rejects_bad_fields(self):
        with tempfile.TemporaryDirectory() as d:
            for qid, code in (("../x", "c"), ("", "c"), ("ok-1", "bad code!")):
                with self.assertRaises(ValueError):
                    channel.drop_answer(Path(d), qid, code, "y", "page")


if __name__ == "__main__":
    unittest.main()
