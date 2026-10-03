"""T2Cb: score wiring at real conductor boundaries, using only fake agents."""
import copy
import html
import json
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

from core import channel, ledger, scores, status_page
from tests.core.test_blocker_claims import BlockerHarness, DOCKER_MISSING, claim


ZERO = "YOUR RECORD (last 7 days): false claims 0, easy-outs 0, overturned 0"
DEFAULT = "Forge keeps building; the role's record stays in its prompts."


class ScoreHarness(BlockerHarness):
    def make_conductor(self, *args, **kwargs):
        c = super().make_conductor(*args, **kwargs)
        c.clock = lambda: self.now
        c.meter.clock = c.clock
        c.local_tz = timezone.utc
        return c

    def data(self):
        return json.loads((self.state / "scores.json").read_text(encoding="utf-8"))

    def counts(self, window="total"):
        return self.data()[window]["roles"]["builder"]["counts"]

    def alarms(self, c):
        return [q for q in c._read("questions.json", {}).values()
                if q.get("kind") == "false_claims"]

    def channel_limits(self, **extra):
        return dict(claude_daily_token_cap=10**9, codex_daily_token_cap=10**9,
                    digest_hour=8, quiet_start=0, quiet_end=0, **extra)

    def assert_run(self, c, role, expected=1):
        events = [e for e in self.events(c, "agent_run") if e["payload"]["role"] == role]
        self.assertEqual(len(events), expected)
        for e in events:
            self.assertEqual(e["identity"], "forge-core")
            self.assertEqual(e["payload"]["at"], self.now.isoformat())
            folder = self.state / "runs" / e["payload"]["run_id"]
            self.assertTrue((folder / "output.json").is_file())
        return events


class RunRecords(ScoreHarness):
    def test_test_writer_records_only_after_completion_and_probes_do_not_count(self):
        observed = []
        def writer(prompt, cwd):
            observed.append(self.events(self.c, "agent_run"))
            return self.write_tests(prompt, cwd)
        c = self.init(agents={"test_writer": writer})
        self.assertEqual(c.step(), "worked")
        self.assertEqual(observed, [[]])
        events = self.assert_run(c, "test_writer")
        self.assertEqual(len(self.events(c, "agent_run")), 1)
        folders = list((self.state / "runs").iterdir())
        self.assertTrue(any("probe-" in p.name for p in folders))
        self.assertEqual([p.name for p in folders if "-test_writer-" in p.name],
                         [events[0]["payload"]["run_id"]])
        self.assertEqual(self.data()["total"]["roles"]["test_writer"]["runs"], 1)

    def test_blocker_reviewer_is_a_counted_run(self):
        c = self.blocked_build(claim(), docker=DOCKER_MISSING)
        c.step()
        self.assertEqual(len(c.team.reviewer.prompts), 1)
        self.assert_run(c, "reviewer")
        self.assertNotIn("YOUR RECORD", c.team.reviewer.prompts[0])

    def test_cap_refuses_launch_without_recording(self):
        c = self.init(agents={"test_writer": self.write_tests},
                      limits=self.channel_limits())
        c.step()
        self.assert_run(c, "test_writer")  # positive control: records are enabled
        before = self.events(c, "agent_run")
        c.meter.add("claude", c.limits["claude_daily_token_cap"])
        self.assertEqual(c.step(), "capped")
        self.assertEqual(self.events(c, "agent_run"), before)
        self.assertEqual(c.team.builder.prompts, [])

    def check_discarded(self, kind):
        def builder(prompt, cwd):
            if kind == "stop":
                (self.state / "PAUSED").write_text("stop", encoding="utf-8")
            elif kind == "tamper":
                (self.state / "unexpected.txt").write_text("tampered", encoding="utf-8")
            else:
                raise RuntimeError("You've hit your usage limit. Try again later.")
            return '{"status":"done"}', 1
        c = self.init(agents={"test_writer": self.write_tests, "builder": builder})
        c.step()
        self.assert_run(c, "test_writer")
        before = self.events(c, "agent_run")
        result = c.step()
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertEqual(self.events(c, "agent_run"), before)
        self.assertEqual(result, {"stop": "paused", "tamper": "killed", "hold": "capped"}[kind])

    def test_mid_run_stop_is_not_recorded(self):
        self.check_discarded("stop")

    def test_stopped_before_launch_records_nothing(self):
        c = self.init(agents={"test_writer": self.write_tests})
        c.step()
        self.assert_run(c, "test_writer")
        before = self.events(c, "agent_run")
        (self.state / "PAUSED").write_text("stop", encoding="utf-8")
        self.assertEqual(c.step(), "paused")
        self.assertEqual(c.team.builder.prompts, [])
        self.assertEqual(self.events(c, "agent_run"), before)

    def test_unready_provider_records_no_builder_run(self):
        c = self.init(agents={"test_writer": self.write_tests})
        c.step()
        self.assert_run(c, "test_writer")
        before = self.events(c, "agent_run")
        self.now += timedelta(days=1)
        self.probes["claude"].script = lambda p, w: ("please log in", 0)
        self.assertEqual(c.step(), "not_ready")
        self.assertEqual(c.team.builder.prompts, [])
        self.assertEqual(self.events(c, "agent_run"), before)

    def test_tampered_run_is_not_recorded(self):
        self.check_discarded("tamper")

    def test_provider_limit_hold_is_not_recorded(self):
        self.check_discarded("hold")


class VerdictsAndPrompts(ScoreHarness):
    def test_false_claim_refresh_and_same_step_troubleshooter_sees_fresh_scores(self):
        seen = []
        def trouble(prompt, cwd):
            seen.append((prompt, self.data()))
            return '{"kind":"suggestion","notes":"try again"}', 1
        c = self.init(agents={"test_writer": self.write_tests,
                              "builder": lambda p, w: ('{"status":"done"}', 1),
                              "troubleshooter": trouble})
        c.step()
        c.step()
        self.assertEqual(self.counts()["false_claims"], 1)
        self.assertIn(ZERO, c.team.builder.prompts[0])
        c.step()
        self.assertIn("YOUR RECORD (last 7 days): false claims 1, easy-outs 0, overturned 0",
                      c.team.builder.prompts[1])
        self.assertEqual(self.counts()["false_claims"], 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1]["total"]["roles"]["builder"]["counts"]["false_claims"], 2)
        self.assertIn(ZERO, seen[0][0])
        self.assertNotIn("YOUR RECORD", c.team.test_writer.prompts[0])
        for role in ("reviewer", "test_writer", "planner"):
            self.assertNotIn("YOUR RECORD", c._prompt_blocks(role))

    def test_rejected_blocker_refreshes_easy_out_and_next_prompt(self):
        c = self.blocked_build({"status": "blocked"})
        c.step()
        self.assertEqual(self.counts()["easy_outs"], 1)
        self.assertIn("blocker rejected: no evidence (easy out)", c._task("T1")["notes"])
        c.step()
        self.assertIn("YOUR RECORD (last 7 days): false claims 0, easy-outs 1, overturned 0",
                      c.team.builder.prompts[1])

    def test_successful_finalization_pass_immediately_refreshes(self):
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        c.step()
        pass_seen_at_write = []
        write = scores.write_scores
        def observe_write(state, data):
            pass_seen_at_write.append(bool(self.events(c, "pass")))
            return write(state, data)
        with patch.object(scores, "write_scores", side_effect=observe_write):
            for _ in range(8):
                self.now += timedelta(minutes=1)
                (self.state / "scores.json").write_text('{"stale":true}', encoding="utf-8")
                c.step()
                passed = self.events(c, "pass")
                if passed:
                    break
        self.assertEqual(len(passed), 1)
        self.assertIn(True, pass_seen_at_write, "scores must be written after the pass event exists")
        self.assertEqual(c._task("T1")["status"], "done")
        self.assertEqual(passed[0]["payload"]["at"], self.now.isoformat())
        self.assertEqual(self.data(), scores.scores(ledger=c._ledger(), state=self.state, now=self.now))
        self.assertEqual(self.counts()["false_claims"], 0)

    def test_scoring_errors_do_not_stop_stage_and_prompt_falls_back_to_zero(self):
        c = self.init(agents={"test_writer": self.write_tests})
        with patch.object(scores, "scores", side_effect=OSError("x" * 800)), patch.object(c, "_log") as log:
            self.assertEqual(c.step(), "worked")
            self.assertIn(ZERO, c._prompt_blocks("builder"))
            self.assertEqual(c._score_digest_lines(), [])
        errors = [a.args[0] for a in log.call_args_list if a.args[0].startswith("scores error:")]
        self.assertTrue(errors)
        self.assertTrue(all(len(e) <= 500 for e in errors))


class ApplyWiring(ScoreHarness):
    def test_stamps_copies_preserves_existing_time_and_refreshes_duplicates(self):
        c = self.init()
        received = []
        class RecordingLedger:
            def apply(self, proposal, identity):
                received.append((proposal, identity))
                return {"status": "duplicate"}
            def events(self):
                return []
        fake = RecordingLedger()
        with patch.object(c, "_ledger", return_value=fake):
            for action in ("audit", "challenge", "pass", "fail", "run_report", "agent_run"):
                for payload in ({"value": [1]}, {"at": "kept"}, {"at": None}):
                    with self.subTest(action=action, payload=payload):
                        original = copy.deepcopy(payload)
                        (self.state / "scores.json").write_text('{"stale":true}', encoding="utf-8")
                        self.assertTrue(c._apply("p", action, "T1", "forge-core", payload))
                        delivered, identity = received[-1]
                        self.assertEqual(identity, "forge-core")
                        self.assertEqual(delivered["payload"], dict(original, at=original.get("at", self.now.isoformat())))
                        self.assertEqual(payload, original)
                        if "at" not in original:
                            self.assertIsNot(delivered["payload"], payload)
                        self.assertEqual(self.data()["generated_at"], self.now.isoformat())
            for action in ("create", "claim", "submit", "test_run"):
                payload = {"value": 1}
                c._apply("p", action, "T1", "forge-core", payload)
                self.assertEqual(received[-1][0]["payload"], payload)
                self.assertNotIn("at", payload)
        self.assertNotIn("audit", ledger.ACTIONS)
        self.assertNotIn("challenge", ledger.ACTIONS)
        self.assertFalse(hasattr(c.team, "auditor"))
        self.assertFalse(hasattr(c.team, "challenger"))

    def test_rejected_apply_does_not_refresh_and_record_errors_are_swallowed(self):
        c = self.init()
        with patch.object(c, "_ledger") as factory, patch.object(c, "_refresh_scores") as refresh:
            factory.return_value.apply.side_effect = ledger.Rejected("refused")
            self.assertFalse(c._apply("p", "fail", "T1", "forge-core"))
            refresh.assert_not_called()
        with patch.object(c, "_apply", side_effect=OSError("disk unavailable")), patch.object(c, "_log") as log:
            c._record_run("builder", "run-1")
            self.assertTrue(log.called)


class Alarms(ScoreHarness):
    def test_default_threshold_restart_throttle_expiry_and_continued_work(self):
        limits = self.channel_limits()
        agents = {"test_writer": self.write_tests,
                  "builder": lambda p, w: ('{"status":"blocked"}', 1)}
        c = self.init(self.task(), self.task(id="T2", title="Next feature"),
                      agents=agents, limits=limits)
        c.step()
        original = copy.deepcopy(c.limits)
        for n in range(1, 4):
            c.step()
            self.assertEqual(self.counts()["easy_outs"], n)
            self.assertEqual(len(self.alarms(c)), int(n == 3))
        q = self.alarms(c)[0]
        self.assertEqual(q["status"], "open")
        self.assertFalse(q.get("hold", False))
        self.assertFalse(q.get("halt", False))
        self.assertIn(DEFAULT, q["body"])
        self.assertIn("builder", q["subject"])
        self.assertIn("3", q["subject"])
        self.assertIn("24", q["body"])
        self.assertIn("7", q["body"])
        self.assertIn("false claims", q["body"])
        self.assertIn("easy-outs", q["body"])
        self.assertEqual(c._read("score_alarms.json", {})["builder"], self.now.isoformat())
        c = self.make_conductor(agents=agents, limits=limits)
        c.step()
        self.assertEqual(len(c.team.builder.prompts), 1)
        self.assertEqual(self.counts()["easy_outs"], 4)
        self.assertEqual(len(self.alarms(c)), 1)
        self.now += timedelta(hours=25)
        for n in range(1, 4):
            # The normal retry budget can block T1; T2 then supplies further
            # real builder attempts without altering retry state for the test.
            for _ in range(4):
                c.step()
                if self.counts("last_24_hours")["easy_outs"] >= n:
                    break
            self.assertEqual(self.counts("last_24_hours")["easy_outs"], n)
            self.assertEqual(len(self.alarms(c)), 1 + int(n == 3))
        self.assertEqual(c.limits, original)
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertFalse((self.state / "KILL").exists())

    def test_custom_threshold_two(self):
        c = self.blocked_build({"status": "blocked"}, limits=self.channel_limits(false_claim_alarm=2))
        c.step()
        self.assertEqual(self.alarms(c), [])
        c.step()
        self.assertEqual(len(self.alarms(c)), 1)

    def test_invalid_thresholds_default_to_three_and_alarm_is_reserved_before_asking(self):
        c = self.init(limits=self.channel_limits())
        def data(n):
            return scores.score_events([{"action": "run_report", "payload": {
                "at": self.now.isoformat(), "easy_out": {}}} for _ in range(n)], self.now)
        asked = []
        def ask(kind, subject, body):
            asked.append((kind, subject, body))
            self.assertEqual(c._read("score_alarms.json", {})["builder"], self.now.isoformat())
            raise OSError("mail unavailable")
        for value in (True, False, 0, -1, "2", 2.5, None):
            with self.subTest(value=value):
                c.limits["false_claim_alarm"] = value
                c._write("score_alarms.json", {"builder": "unparseable"})
                asked.clear()
                with patch.object(c, "_ask", side_effect=ask), patch.object(c, "_scores_now", return_value=data(2)):
                    c._refresh_scores()
                self.assertEqual(asked, [])
                with patch.object(c, "_ask", side_effect=ask), patch.object(c, "_scores_now", return_value=data(3)):
                    c._refresh_scores()
                    c._refresh_scores()
                self.assertEqual(len(asked), 1)
                self.assertEqual(asked[0][0], "false_claims")


class Outputs(ScoreHarness):
    def test_channel_default_and_digest_insertion_preserve_legacy_bytes(self):
        self.assertIn("false_claims", channel.INSTANT_KINDS)
        self.assertEqual(channel.default_for("false_claims"), DEFAULT)
        kwargs = dict(owner="owner@example.com", local_now=self.now, mail_used=(1, 2),
                      mail_caps=(6, 30), page_url="http://localhost/", extra_lines=["Blockers: none"])
        tasks = [{"status": "tests_ok"}]
        expected = ("[Forge] Daily digest 2026-09-30: no open questions",
                    "Forge daily digest for Wednesday 30 September 2026, 08:00.\n\n"
                    "Nothing is waiting on you.\n\nBlockers: none\nTasks: 1 tests_ok.\n"
                    "Email used: 1 of 6 this hour, 2 of 30 today.\n"
                    "Status page (on your PC): http://localhost/\n"
                    "To stop everything: reply STOP to any Forge email.")
        self.assertEqual(channel.build_digest({}, tasks, **kwargs), expected)
        self.assertEqual(channel.build_digest({}, tasks, score_lines=None, **kwargs), expected)
        lines = ["Scores (last 7 days):", "- builder: 1 runs"]
        subject, body = channel.build_digest({}, tasks, score_lines=lines, **kwargs)
        self.assertEqual(subject, expected[0])
        self.assertEqual(body, expected[1].replace("Tasks:", "\n".join(lines) + "\nTasks:"))

    def test_conductor_digest_includes_scores_but_scores_do_not_trigger_idle_digest(self):
        c = self.init(limits=self.channel_limits())
        c._channel_tick()
        bodies = [b for s, b in self.mails if "Daily digest" in s]
        self.assertEqual(len(bodies), 1)
        self.assertIn("Scores (last 7 days):", bodies[0])
        self.now += timedelta(days=1)
        c._record_run("builder", "digest-only-run")
        self.assertTrue(c._score_digest_lines())
        c._channel_tick()
        self.assertEqual(len([s for s, b in self.mails if "Daily digest" in s]), 1)

    def test_status_page_scores_missing_corrupt_and_escaped(self):
        c = self.init()
        page = status_page.render(self.state, c.limits, self.now)
        self.assertIn("<h2>Scores</h2>", page)
        self.assertIn("<p class=muted>No scores yet.</p>", page)
        data = scores.score_events([{"action": "agent_run", "payload": {
            "role": role, "at": self.now.isoformat(), "run_id": role}}
            for role in ("builder", '<script>alert("x")</script>&')], self.now)
        scores.write_scores(self.state, data)
        page = status_page.render(self.state, c.limits, self.now)
        self.assertLess(page.index("<h2>Usage today</h2>"), page.index("<h2>Scores</h2>"))
        for line in scores.digest_lines(data):
            self.assertIn("<li>" + html.escape(line, quote=True) + "</li>", page)
        self.assertNotIn('<script>alert("x")</script>', page)
        (self.state / "scores.json").write_text("{broken", encoding="utf-8")
        page = status_page.render(self.state, c.limits, self.now)
        self.assertIn("<h2>Scores</h2>", page)
        self.assertIn("<p class=bad>Scores couldn't be read right now.</p>", page)
        self.assertIn("<h1>Forge status</h1>", page)


if __name__ == "__main__":
    unittest.main()
