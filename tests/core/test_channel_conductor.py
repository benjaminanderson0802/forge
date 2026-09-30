"""Layer 1E: the conductor's side of Ben's channel (D-021 to D-024), with all mail faked.

The channel policy (digest routing, quiet hours) is on only when limits carry `digest_hour`; R18-R40 keep holding.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

try:
    from tests.core.test_bootstrap import Harness
except ImportError:  # run from inside tests/core
    from test_bootstrap import Harness

from core import channel

CHI = timezone(timedelta(hours=-5))
LIM = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9, "mail_per_hour": 3, "mail_per_day": 10,
       "digest_hour": 8, "quiet_start": 23, "quiet_end": 7}


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=CHI)


class ChannelHarness(Harness):
    def conductor(self, limits=None, when=None):
        c = self.make_conductor(limits=dict(LIM if limits is None else limits))
        self.now = when or at(1, 12)
        c.clock = lambda: self.now
        c.local_tz = CHI
        return c

    def qs(self):
        return json.loads((self.state / "questions.json").read_text(encoding="utf-8"))

    def queue_lines(self):
        p = self.state / "queue.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def tasks(self, *statuses):
        self.c._save_queue({"layer": "layer-1", "tasks": [
            {"id": f"T{i}", "kind": "build", "title": f"task {i}", "status": s, "notes": [], "trouble_notes": [],
             "fail_signatures": []} for i, s in enumerate(statuses, 1)]})

    def owner_mail(self, subject, body):
        self.messages.append({"from": "Ben <ben@example.com>", "subject": subject, "body": body})


class PolicyOff(ChannelHarness):
    def test_without_digest_hour_every_kind_is_still_emailed_at_once(self):
        lim = {k: v for k, v in LIM.items() if k not in ("digest_hour", "quiet_start", "quiet_end")}
        c = self.conductor(limits=lim, when=at(1, 2))  # 02:00 would be quiet if the policy were on
        c._ask("blocked", "Task T1 is blocked", "details")
        self.assertEqual(len(self.mails), 1)
        self.assertTrue(self.mails[0][0].startswith("[Forge Q-blocked-1 "))
        self.assertEqual([i["id"] for i in self.queue_lines()], ["blocked-1"])  # the queue view is always kept
        c._channel_tick()
        self.assertEqual(len(self.mails), 1)  # and no digest


class Routing(ChannelHarness):
    def test_non_instant_kinds_wait_for_the_digest(self):
        c = self.conductor()
        c._ask("blocked", "Task T1 is blocked", "details")
        c._ask("merge", "Merge of T2 is blocked", "details")
        c._handle_inbox()  # the R7 retry never sends a digest item on its own
        self.assertEqual(self.mails, [])
        q = self.qs()["blocked-1"]
        self.assertTrue(q["hold"]); self.assertTrue(q["digest"]); self.assertFalse(q["delivered"])
        self.assertEqual([(i["id"], i["via"]) for i in self.queue_lines()], [("blocked-1", "digest"), ("merge-2", "digest")])

    def test_instant_kinds_go_at_once_with_their_default(self):
        c = self.conductor()
        c._ask("gate", "layer-1 is ready: reply y to approve", "report", pr="7")
        self.assertEqual(len(self.mails), 1)
        self.assertIn("[Forge Q-gate-1 ", self.mails[0][0])
        self.assertIn("If you don't answer: " + channel.default_for("gate"), self.mails[0][1])

    def test_quiet_hours_hold_instant_mail_until_seven(self):
        c = self.conductor(when=at(1, 2))
        c._ask("replan", "Forge paused", "reasons")
        self.assertEqual(self.mails, [])
        self.assertFalse(self.qs()["replan-1"]["delivered"])
        self.now = at(1, 6, 59); c._handle_inbox()
        self.assertEqual(self.mails, [])
        self.now = at(1, 7, 0); c._handle_inbox()
        self.assertEqual(len(self.mails), 1)
        self.assertTrue(self.qs()["replan-1"]["delivered"])

    def test_quiet_hours_do_not_count_against_the_budget(self):
        c = self.conductor(when=at(1, 23, 30))
        self.assertFalse(c._send("[Forge] x", "y"))
        self.assertEqual(c._read("mail_log.json", {}).get("sent", []), [])

    def test_a_halt_alert_is_an_emergency_and_goes_at_night(self):
        c = self.conductor(when=at(1, 3))
        (self.state / "KILL").write_text("x")
        c._ask("tamper", "Forge stopped", "files changed", halt=True)
        self.assertEqual(len(self.mails), 1)

    def test_notices_wait_for_morning_and_are_not_lost(self):
        c = self.conductor(when=at(1, 2))
        self.assertFalse(c._notice_once("start", "[Forge] conductor started", "body"))
        self.assertNotIn("start", c._read("notices.json", {}))
        self.now = at(1, 7, 30)
        self.assertTrue(c._notice_once("start", "[Forge] conductor started", "body"))
        self.assertEqual(len(self.mails), 1)


class Digest(ChannelHarness):
    def test_one_digest_a_day_at_eight(self):
        c = self.conductor(when=at(1, 12))
        self.tasks("todo")
        c._ask("blocked", "Task T1 is blocked", "details")
        self.now = at(2, 7, 59); c._channel_tick()
        self.assertEqual(self.mails, [])
        self.now = at(2, 8, 0); c._channel_tick()
        self.assertEqual(len(self.mails), 1)
        subject, body = self.mails[0]
        self.assertTrue(subject.startswith("[Forge] Daily digest 2026-10-02"))
        code = self.qs()["blocked-1"]["code"]
        self.assertIn(f"[Forge Q-blocked-1 {code}]", body)
        for t in (at(2, 8, 30), at(2, 12), at(2, 22, 59)):
            self.now = t; c._channel_tick()
        self.assertEqual(len(self.mails), 1)
        self.now = at(3, 8, 1); c._channel_tick()
        self.assertEqual(len(self.mails), 2)  # still open: one reminder the next morning

    def test_no_digest_when_there_is_nothing_new(self):
        c = self.conductor(when=at(1, 8))
        c._channel_tick()
        self.assertEqual(self.mails, [])
        self.tasks("todo", "done")
        self.now = at(2, 8); c._channel_tick()
        self.assertEqual(len(self.mails), 1)  # progress to report
        self.now = at(3, 8); c._channel_tick()
        self.assertEqual(len(self.mails), 1)  # nothing changed, nothing open

    def test_digest_goes_through_the_budget(self):
        c = self.conductor(when=at(1, 8))
        c._ask("blocked", "Task T1 is blocked", "details")
        stamp = self.now.isoformat()
        c._write("mail_log.json", {"sent": [stamp] * 3, "ids": []})
        c._channel_tick()
        self.assertEqual(self.mails, [])
        self.now = at(1, 9, 1); c._channel_tick()  # the hour has passed and the retry wait is over
        self.assertEqual(len(self.mails), 1)

    def test_a_failed_attempt_counts_and_waits_an_hour(self):
        c = self.conductor(when=at(1, 8))
        c._ask("blocked", "Task T1 is blocked", "details")
        tries = []

        def broken(subject, body):
            tries.append(subject)
            raise OSError("smtp down")
        c.mailer = broken
        c._channel_tick()
        self.now = at(1, 8, 30); c._channel_tick()
        self.assertEqual(len(tries), 1)
        self.assertEqual(len(c._read("mail_log.json", {})["sent"]), 1)  # R25: the attempt counted
        self.now = at(1, 9, 1); c._channel_tick()
        self.assertEqual(len(tries), 2)

    def test_nothing_while_killed_or_quiet(self):
        c = self.conductor(when=at(1, 8))
        c._ask("blocked", "Task T1 is blocked", "details")
        (self.state / "KILL").write_text("x")
        c._channel_tick()
        self.assertEqual(self.mails, [])
        (self.state / "KILL").unlink()
        c.limits["digest_hour"] = 0
        self.now = at(2, 1); c._channel_tick()  # due, but quiet
        self.assertEqual(self.mails, [])

    def test_step_sends_the_digest(self):
        c = self.conductor(when=at(1, 12))
        c._ask("blocked", "Task T1 is blocked", "details")
        self.now = at(2, 8, 5)
        c.step()
        self.assertEqual(len(self.mails), 1)
        self.assertIn("Daily digest", self.mails[0][0])

    def test_stalled_sends_the_digest_early_at_most_every_12_hours(self):
        c = self.conductor(when=at(1, 8))
        self.tasks("done", "blocked")
        c._channel_tick()  # today's digest: progress to report, nothing open
        self.assertEqual(len(self.mails), 1)
        self.now = at(1, 13)
        c._ask("blocked", "Task T2 is blocked", "details")
        c._channel_tick()  # nothing can run and Ben hasn't heard: early digest
        self.assertEqual(len(self.mails), 2)
        self.assertIn("blocked-1", self.mails[1][1])
        self.now = at(1, 15)
        c._ask("blocked", "Task T3 is blocked", "details")
        c._channel_tick()
        self.assertEqual(len(self.mails), 2)  # within 12 hours of the last early digest
        self.now = at(2, 1, 30); c._channel_tick()  # 12 hours later, but quiet
        self.assertEqual(len(self.mails), 2)
        self.now = at(2, 8); c._channel_tick()
        self.assertEqual(len(self.mails), 3)

    def test_no_early_digest_while_work_can_run(self):
        c = self.conductor(when=at(1, 13))
        self.tasks("todo", "blocked")
        c._write("digest.json", {"sent_day": "2026-10-01"})
        c._ask("blocked", "Task T2 is blocked", "details")
        c._channel_tick()
        self.assertEqual(self.mails, [])

    def test_mail_volume_is_bounded(self):
        """Three days, a new digest-kind question every 10 minutes, a tick every 10 minutes."""
        for statuses, per_day in ((("todo", "blocked"), 1), (("done", "blocked"), 3)):
            self.mails.clear()
            for f in self.state.iterdir():
                if f.is_file():
                    f.unlink()
            c = self.conductor(when=at(1, 0))
            self.tasks(*statuses)
            days = {}
            t = at(1, 0)
            while t < at(4, 0):
                self.now = t
                c._ask("blocked", f"blocked at {t.isoformat()}", "details")
                before = len(self.mails)
                c._channel_tick()
                days[t.day] = days.get(t.day, 0) + len(self.mails) - before
                t += timedelta(minutes=10)
            for d, n in days.items():
                self.assertLessEqual(n, per_day, (statuses, d))
            self.assertGreaterEqual(sum(days.values()), 3, statuses)


class Answers(ChannelHarness):
    def test_digest_items_can_be_answered_by_email(self):
        c = self.conductor()
        self.tasks("blocked")
        c._ask("blocked", "Task T1 is blocked", "details", task="T1")
        code = self.qs()["blocked-1"]["code"]
        self.owner_mail(f"[Forge Q-blocked-1 {code}] answer", "use the other library")
        c._handle_inbox()
        self.assertEqual(self.qs()["blocked-1"]["status"], "answered")
        self.assertEqual(self.queue_lines(), [])

    def test_owner_address_in_a_display_name_is_not_the_owner(self):
        """From: "ben@example.com" <evil@x.example> is someone else: no STOP, no answer."""
        c = self.conductor()
        c._ask("gate", "layer-1 is ready", "report", pr="7")
        code = self.qs()["gate-1"]["code"]
        for frm in ('"ben@example.com" <evil@x.example>', "ben@example.com via <evil@x.example>",
                    "evil@x.example, ben@example.com"):
            self.messages[:] = [{"from": frm, "subject": f"[Forge Q-gate-1 {code}] y", "body": "y"},
                                {"from": frm, "subject": "STOP", "body": "STOP"}]
            c._handle_inbox()
            self.assertFalse((self.state / "KILL").exists(), frm)
            self.assertEqual(self.qs()["gate-1"]["status"], "open", frm)
        self.messages[:] = [{"from": "Ben <BEN@example.com>", "subject": "STOP", "body": ""}]
        c._handle_inbox()
        self.assertTrue((self.state / "KILL").exists())

    def test_a_dropped_answer_is_checked_like_an_email_reply(self):
        c = self.conductor()
        c._ask("gate", "layer-1 is ready", "report", pr="7")
        code = self.qs()["gate-1"]["code"]
        folder = c.channel_in
        channel.drop_answer(folder, "gate-1", "wrongcod", "y", "page")
        channel.drop_answer(folder, "nosuch-9", code, "y", "page")
        c._handle_inbox()
        self.assertEqual(self.qs()["gate-1"]["status"], "open")
        self.assertEqual(self.gh_calls, [])
        channel.drop_answer(folder, "gate-1", code, "y", "page")
        c._handle_inbox()
        self.assertEqual(self.qs()["gate-1"]["status"], "answered")
        self.assertEqual([a[:2] for a in self.gh_calls], [["pr", "edit"], ["pr", "merge"]])
        self.assertEqual(list(folder.iterdir()), [])

    def test_a_dropped_stop_halts_and_keeps_the_rest(self):
        c = self.conductor()
        c._ask("gate", "layer-1 is ready", "report", pr="7")
        code = self.qs()["gate-1"]["code"]
        channel.drop_answer(c.channel_in, "gate-1", code, "STOP", "page")
        channel.drop_answer(c.channel_in, "gate-1", code, "y", "page")
        c._handle_inbox()
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(len(list(c.channel_in.iterdir())), 1)  # kept for after the restart

    def test_dropped_answers_wait_while_killed(self):
        c = self.conductor()
        c._ask("gate", "layer-1 is ready", "report", pr="7")
        code = self.qs()["gate-1"]["code"]
        (self.state / "KILL").write_text("x")
        channel.drop_answer(c.channel_in, "gate-1", code, "y", "page")
        self.assertEqual(c.step(), "killed")
        c._take_channel_answers()
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(len(list(c.channel_in.iterdir())), 1)

    def test_drop_folder_is_outside_the_fingerprinted_state(self):
        c = self.conductor()
        self.assertNotIn(self.state.resolve(), c.channel_in.resolve().parents)


class StopDuringAgentRun(Harness):
    """Defect D (reported by 1E): pressing the status page's Stop while an agent runs was logged as tampering.
    Under R42/R49 a KILL that appears mid-run is a clean stop, whoever writes it."""

    def test_status_page_stop_during_a_builder_run_is_a_clean_stop(self):
        import http.client
        import threading
        from core import status_page

        srv = status_page.make_server(self.state, self.state.parent / "channel", {}, port=0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def press_stop():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/stop", body=b"", headers={"Host": f"127.0.0.1:{port}",
                                                             "Origin": f"http://127.0.0.1:{port}"})
            status = conn.getresponse().status
            conn.close()
            return status

        def builder(prompt, cwd):
            self.build_feature(prompt, cwd)
            self.assertEqual(press_stop(), 303)  # Ben presses Stop on the page while the builder works
            return '{"status":"done"}', 1

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": builder})
        before = c._task("T1")
        self.assertEqual(c.step(), "killed")
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual(c._task("T1"), before)  # nothing recorded: no failure, no signature
        self.assertEqual(c._ledger().contracts()["T1"]["status"], "open")  # the claim was released
        qs = json.loads((self.state / "questions.json").read_text()) if (self.state / "questions.json").exists() else {}
        self.assertEqual([q for q in qs.values() if q.get("kind") == "tamper"], [])
        self.assertEqual(self.mails, [])
        log = (self.state / "errors.log").read_text(encoding="utf-8")
        self.assertIn("stop requested during builder run", log)
        self.assertNotIn("TAMPER", log)
        self.assertEqual(c.run(max_steps=2, sleep=lambda s: None), "killed")


if __name__ == "__main__":
    unittest.main()
