"""R60 lanes: several conductors side by side, each with its own queue, layer branch and worktrees, sharing one set of
caps (token meter, holds, runs per day, mail budget) and one kill switch. Driven by local fakes and, for the shared
lock, real processes."""
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import lanes, service, status_page
from core.agents import FakeAgent
from core.bootstrap import Conductor, Team
from tests.core.test_bootstrap import HEALTHY_CHECKS, git, healthy_probes, py_test

ROOT = Path(__file__).resolve().parents[2]
OWNER = "ben@example.com"
CAPS = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9}
TEST_SRC = "import unittest\nimport feat\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(feat.VALUE, 42)\n"


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class LaneHarness(unittest.TestCase):
    """A Forge root in a temp folder: one repo, state/ with the real lane layout, a work root per lane."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.work_root, self.sroot = root / "repo", root / "work", root / "state"
        self.repo.mkdir()
        self.shared = lanes.shared_dir(self.sroot)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Forge Test")
        git(self.repo, "config", "user.email", OWNER)
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.mails = {}      # lane -> [(subject, body)]
        self.messages = []   # Ben's mailbox, read only by the main lane
        self.cs = {}

    def tearDown(self):
        self.tmp.cleanup()

    def inbox(self):
        out, self.messages[:] = list(self.messages), []
        return out

    def writer(self, tokens=1, during=None):
        def script(prompt, cwd):
            p = cwd / "tests/core/test_feat.py"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(TEST_SRC, encoding="utf-8")
            if during:
                during(cwd)
            return '{"files":["tests/core/test_feat.py"]}', tokens
        return script

    def lane(self, name, *, test_writer=None, limits=None, layer=None):
        noop = lambda p, c: ('{"status":"ok"}', 1)
        team = Team(test_writer=FakeAgent(test_writer or self.writer(), provider="codex"),
                    builder=FakeAgent(noop, provider="claude"),
                    reviewer=FakeAgent(lambda p, c: ('{"verdict":"pass","reasons":[]}', 1), provider="codex"),
                    troubleshooter=FakeAgent(lambda p, c: ('{"kind":"suggestion","notes":"x"}', 1), provider="claude"),
                    drift_keeper=FakeAgent(noop, provider="claude"),
                    planner=FakeAgent(lambda p, c: ('{"tasks":[]}', 1), provider="claude"))
        state = lanes.state_dir(self.sroot, name)
        work = lanes.work_dir(self.work_root, name)
        work.mkdir(parents=True, exist_ok=True)
        self.mails[name] = []
        inbox = self.inbox if name == lanes.MAIN else \
            lanes.routed_inbox(lanes.state_dir(self.sroot, lanes.MAIN), name, state)
        c = Conductor(self.repo, work, state, team, dict(limits or CAPS), owner_email=OWNER,
                      mailer=lambda s, b, _n=name: self.mails[_n].append((s, b)), inbox=inbox,
                      gh=lambda args: (0, ""), judge_cmds=[], push=False, checks=dict(HEALTHY_CHECKS),
                      probes=healthy_probes(), shared=self.shared, lane=name)
        c.init_queue(layer or f"layer-{name}", [self.task()])
        lanes.register(self.sroot, name)
        self.cs[name] = c
        return c

    @staticmethod
    def task(**kw):
        d = {"id": "T1", "kind": "build", "title": "Implement feature", "section": "Feature value is 42.",
             "files_in_scope": ["feat.py"], "test_files": ["tests/core/test_feat.py"],
             "test_cmd": py_test("tests/core/test_feat.py")}
        d.update(kw)
        return d

    def status(self, c, tid="T1"):
        return c._task(tid)["status"]

    def questions(self, c):
        return c._read("questions.json", {})

    def shared_meter(self):
        return service.shared_meter(self.shared, lambda: datetime.now(timezone.utc))


class LayoutAndQueues(LaneHarness):
    def test_each_lane_has_its_own_state_queue_branch_and_worktree(self):
        a, b = self.lane("main"), self.lane("b")
        self.assertEqual(a.state, self.sroot / "bootstrap")
        self.assertEqual(b.state, self.sroot / "lanes" / "b")
        self.assertEqual(b.work, self.work_root / "lanes" / "b")
        self.assertEqual(a.step(), "worked")
        self.assertEqual(self.status(a), "tests_ok")
        self.assertEqual(self.status(b), "todo", "lane b's queue is independent of main's")
        self.assertEqual(b.step(), "worked")
        self.assertEqual(self.status(b), "tests_ok")
        self.assertEqual(json.loads((b.state / "queue.json").read_text())["layer"], "layer-b")
        self.assertIn("tests/core/test_feat.py", git(self.repo, "ls-tree", "-r", "--name-only", "layer-b"))
        self.assertTrue((self.work_root / "lanes" / "b" / "layer-b").is_dir())
        self.assertTrue((self.work_root / "layer-main").is_dir())
        self.assertEqual(lanes.listed(self.sroot), ["main", "b"])

    def test_the_shared_meter_sums_both_lanes_and_no_lane_keeps_its_own(self):
        a = self.lane("main", test_writer=self.writer(tokens=300))
        b = self.lane("b", test_writer=self.writer(tokens=45))
        self.assertEqual(a.step(), "worked")
        self.assertEqual(b.step(), "worked")
        self.assertEqual(self.shared_meter().used_today("codex"), 345)
        self.assertEqual(a.meter.used_today("codex"), 345)
        self.assertEqual(b.meter.used_today("codex"), 345)
        self.assertFalse((a.state / "meter.json").exists())
        self.assertFalse((b.state / "meter.json").exists())

    def test_runs_per_day_are_counted_across_lanes(self):
        a, b = self.lane("main"), self.lane("b")
        a.step()
        b.step()
        mine = lambda c: sum(1 for d in (c.state / "runs").iterdir())  # noqa: E731
        self.assertEqual(self.shared_meter().runs_today(), mine(a) + mine(b))
        self.assertGreater(mine(b), 0)


class SharedCapsAndKill(LaneHarness):
    def test_the_token_cap_stops_both_lanes(self):
        limits = {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 100}
        a = self.lane("main", test_writer=self.writer(tokens=100), limits=limits)
        calls = []
        b = self.lane("b", test_writer=lambda p, c: calls.append(1) or ('{"files":[]}', 1), limits=limits)
        self.assertEqual(a.step(), "worked")
        self.assertEqual(a.step(), "capped")
        self.assertEqual(b.step(), "capped", "lane b never ran an agent, but the shared cap is used up")
        self.assertEqual(calls, [])

    def test_the_runs_per_day_cap_stops_both_lanes(self):
        a, b = self.lane("main"), self.lane("b")
        self.assertEqual(a.step(), "worked")
        used = self.shared_meter().runs_today()
        a.limits["agent_runs_per_day"] = b.limits["agent_runs_per_day"] = used
        self.assertEqual(b.step(), "capped")
        self.assertEqual(a.step(), "capped")

    def test_a_hold_in_one_lane_holds_the_provider_in_every_lane(self):
        a, b = self.lane("main"), self.lane("b")
        a.meter.hold("codex", datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertTrue((self.shared / "holds" / "main.json").exists())
        self.assertEqual(b.step(), "capped")

    def test_global_kill_stops_every_lane(self):
        calls = []
        a = self.lane("main", test_writer=lambda p, c: calls.append("main") or ('{}', 1))
        b = self.lane("b", test_writer=lambda p, c: calls.append("b") or ('{}', 1))
        (self.shared / "KILL").write_text("stop\n")
        self.assertEqual(a.step(), "killed")
        self.assertEqual(b.step(), "killed")
        self.assertEqual(calls, [])
        self.assertEqual(a.run(max_steps=3, sleep=lambda s: None), "killed")

    def test_a_lane_kill_stops_only_that_lane(self):
        a, b = self.lane("main"), self.lane("b")
        (b.state / "KILL").write_text("stop b\n")
        self.assertEqual(b.step(), "killed")
        self.assertEqual(a.step(), "worked")
        self.assertEqual(self.status(b), "todo")

    def test_global_kill_during_a_run_is_a_stop_not_tampering(self):
        a = self.lane("main", test_writer=self.writer(during=lambda cwd: (self.shared / "KILL").write_text("x\n")))
        self.assertEqual(a.step(), "killed")
        self.assertFalse((a.state / "KILL").exists(), "a stop is not a tamper alarm")
        self.assertFalse([q for q in self.questions(a).values() if q["kind"] == "tamper"])
        self.assertEqual(self.status(a), "todo")

    def test_the_mail_budget_is_shared(self):
        limits = dict(CAPS, mail_per_hour=3, mail_per_day=30)
        a, b = self.lane("main", limits=limits), self.lane("b", limits=limits)
        self.assertTrue(a._send("one", "x"))
        self.assertTrue(b._send("two", "x"))
        self.assertTrue(a._send("three", "x"))
        self.assertFalse(b._send("four", "x"), "the hourly budget counts every lane's mail")
        sent = [x for n in ("main", "b") for x in json.loads((self.shared / "mail" / f"{n}.json").read_text())["sent"]]
        self.assertEqual(len(sent), 3, "each lane writes only its own mail file; the budget sums them")
        self.assertFalse((a.state / "mail_log.json").exists())


class TamperAcrossLanes(LaneHarness):
    def test_no_false_alarm_when_another_lane_writes_shared_files_during_a_run(self):
        """Lane b runs a whole step (an agent run, meter, run records, its queue) and sends mail while main's agent
        is running. None of that is main's tampering."""
        b = self.lane("b", test_writer=self.writer(tokens=7))

        def other_lane_works(cwd):
            self.assertEqual(b.step(), "worked")
            self.assertTrue(b._send("[Forge] from lane b", "hello"))
            b.meter.hold("claude", datetime.now(timezone.utc) + timedelta(minutes=10))

        a = self.lane("main", test_writer=self.writer(tokens=5, during=other_lane_works))
        self.assertEqual(a.step(), "worked")
        self.assertEqual(self.status(a), "tests_ok")
        self.assertEqual(self.status(b), "tests_ok")
        self.assertFalse((a.state / "KILL").exists())
        self.assertFalse((self.shared / "KILL").exists())
        self.assertGreaterEqual(self.shared_meter().used_today("codex"), 12)

    def test_an_agent_that_lowers_the_shared_meter_is_tampering(self):
        a = self.lane("main")
        a.meter.add("claude", 5000)

        def lower(cwd):
            m = self.shared / "meter" / "main.json"
            data = json.loads(m.read_text())
            data[today()]["claude"] = 10
            m.write_text(json.dumps(data))

        a.team.test_writer = FakeAgent(self.writer(during=lower), provider="codex")
        self.assertEqual(a.step(), "killed")
        self.assertTrue((a.state / "KILL").exists())
        self.assertTrue((self.shared / "KILL").exists(), "lanes share caps: a tamper alarm stops them all")
        tamper = [q for q in self.questions(a).values() if q["kind"] == "tamper"]
        self.assertEqual(len(tamper), 1)
        self.assertIn("shared/meter/main.json", tamper[0]["body"])

    def test_an_agent_that_deletes_the_shared_meter_or_a_hold_or_mail_is_tampering(self):
        for what in ("meter", "hold", "mail"):
            with self.subTest(what=what):
                self.tearDown()
                self.setUp()
                a = self.lane("main")
                a.meter.add("claude", 10)
                a.meter.hold("gemini", datetime.now(timezone.utc) + timedelta(seconds=30))  # no team member
                self.assertTrue(a._send("[Forge] x", "y"))

                def strike(cwd, what=what):
                    if what == "meter":
                        (self.shared / "meter" / "main.json").unlink()
                    elif what == "hold":
                        (self.shared / "holds" / "main.json").write_text("{}")
                    else:
                        (self.shared / "mail" / "main.json").write_text('{"sent": [], "ids": []}')

                a.team.test_writer = FakeAgent(self.writer(during=strike), provider="codex")
                self.assertEqual(a.step(), "killed")
                self.assertTrue((self.shared / "KILL").exists())

    def test_a_new_file_in_the_shared_folder_is_tampering(self):
        a = self.lane("b")
        a.team.test_writer = FakeAgent(self.writer(during=lambda cwd: (self.shared / "x.json").write_text("{}")),
                                       provider="codex")
        self.assertEqual(a.step(), "killed")
        self.assertTrue((a.state / "KILL").exists())

    def test_editing_another_lanes_queue(self):
        """Caught when that lane has an agent running (its own fingerprint); not caught by the editing lane, which
        can't tell an agent's write to another lane's state from that lane's conductor (the known limit, R60)."""
        def edit_main_queue(cwd):
            q = self.sroot / "bootstrap" / "queue.json"
            data = json.loads(q.read_text())
            data["tasks"][0]["status"] = "done"
            q.write_text(json.dumps(data))

        b = self.lane("b", test_writer=self.writer(during=edit_main_queue))
        a = self.lane("main", test_writer=self.writer(during=lambda cwd: self.assertEqual(b.step(), "worked")))
        self.assertEqual(a.step(), "killed", "main's agent was running: main's fingerprint catches the edit")
        self.assertTrue((a.state / "KILL").exists())
        self.assertIn("queue.json", next(q for q in self.questions(a).values() if q["kind"] == "tamper")["body"])
        self.assertFalse((b.state / "KILL").exists(), "known limit: lane b's own check can't see it")

    def test_the_fingerprint_leaves_out_exactly_other_lanes_accounting_and_the_lock(self):
        main, b = self.lane("main"), self.lane("b")
        main.meter.add("codex", 1)
        main._send("[Forge] x", "y")
        b.meter.add("codex", 1)
        b._send("[Forge] x", "y")
        b.meter.hold("gemini", datetime.now(timezone.utc) + timedelta(minutes=5))
        (self.shared / "inbox_seen.json").write_text("[]")
        (self.shared / "KILL").write_text("x")
        keys = b._fingerprint()
        self.assertEqual(sorted(k for k in keys if k.startswith("shared/")),
                         ["shared/KILL", "shared/holds/b.json", "shared/mail/b.json", "shared/meter/b.json",
                          "shared/migrated.json"], "own accounting is fingerprinted; other lanes' is not")
        self.assertIn("shared/inbox_seen.json", main._fingerprint(), "inbox_seen.json is main's own file")
        self.assertTrue((self.shared / "shared.lock").exists())
        self.assertIn("queue.json", keys)
        self.assertFalse([k for k in keys if k.startswith("bootstrap") or "lanes/" in k])


class Migration(LaneHarness):
    def old_state(self):
        boot = self.sroot / "bootstrap"
        boot.mkdir(parents=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        (boot / "meter.json").write_text(json.dumps({today(): {"claude": 1234, "codex": 99}}))
        (boot / "holds.json").write_text(json.dumps({"claude": until}))
        (boot / "mail_log.json").write_text(json.dumps({"sent": [now], "ids": ["<a@forge.local>"]}))
        (boot / "inbox_seen.json").write_text(json.dumps(["<m1>"]))
        return boot, now

    def test_first_run_copies_todays_counts_into_shared_and_never_resets_them(self):
        boot, now = self.old_state()
        self.assertTrue(lanes.migrate(self.sroot))
        for old, new in (("meter.json", "meter/main.json"), ("holds.json", "holds/main.json"),
                         ("mail_log.json", "mail/main.json"), ("inbox_seen.json", "inbox_seen.json")):
            self.assertEqual((self.shared / new).read_bytes(), (boot / old).read_bytes(), new)
        self.assertTrue((self.shared / "migrated.json").exists())
        a = self.lane("main")
        self.assertEqual(a.meter.used_today("claude"), 1234)
        self.assertIsNotNone(a.meter.held("claude"))
        self.assertEqual(a._mail_log()["sent"], [now], "the mail budget carries over")
        a.meter.add("claude", 6)
        self.assertFalse(lanes.migrate(self.sroot), "migration runs once")
        self.assertEqual(a.meter.used_today("claude"), 1240, "a second start never overwrites the shared meter")

    def test_no_old_state_starts_an_empty_shared_meter(self):
        self.assertTrue(lanes.migrate(self.sroot))
        self.assertEqual(json.loads((self.shared / "meter" / "main.json").read_text()), {})

    def test_a_missing_shared_meter_after_migration_fails_closed_not_reset(self):
        """Review fix 6: once migrated.json exists, a missing meter is an accounting error (nothing launches; Ben is
        asked once), never a silent re-migration that resets the day's usage."""
        self.old_state()
        calls = []
        a = self.lane("main", test_writer=lambda p, c: calls.append(1) or ('{}', 1))
        a.meter.add("claude", 6)
        (self.shared / "meter" / "main.json").unlink()
        (self.sroot / "bootstrap" / "meter.json").write_text(json.dumps({today(): {"claude": 1}}))
        fresh = self.lane("p2")  # a new conductor start: migrate() runs again
        self.assertFalse((self.shared / "meter" / "main.json").exists(), "never re-migrated")
        self.assertEqual(fresh.step(), "not_ready")
        self.assertEqual(calls, [])
        qs = [q for q in self.questions(fresh).values() if q["kind"] == "accounting"]
        self.assertEqual(len(qs), 1)
        self.assertEqual(fresh.step(), "not_ready")
        self.assertEqual(len([q for q in self.questions(fresh).values() if q["kind"] == "accounting"]), 1,
                         "Ben is asked once")


class Routing(LaneHarness):
    def ask_blocked(self, c):
        q = c._queue()
        q["tasks"][0]["status"] = "blocked"
        c._save_queue(q)
        return c._ask("blocked", "Task T1 is blocked", "details", task="T1")

    def reply(self, c, qid, body, sender=OWNER):
        code = self.questions(c)[qid]["code"]
        self.messages.append({"from": sender, "subject": f"Re: [Forge Q-{qid} {code}] Task T1 is blocked",
                              "body": body})

    def test_a_lane_prefixes_its_question_ids(self):
        a, b = self.lane("main"), self.lane("p2")
        self.assertEqual(self.ask_blocked(a), "blocked-1")
        self.assertEqual(self.ask_blocked(b), "p2-blocked-1")
        self.assertTrue(self.mails["p2"][0][0].startswith("[Forge Q-p2-blocked-1 "))

    def test_main_routes_an_answer_to_the_lane_that_asked(self):
        a, b = self.lane("main"), self.lane("p2")
        qid = self.ask_blocked(b)
        self.reply(b, qid, "Try the other library.")
        a._handle_inbox()
        self.assertEqual(self.status(b), "blocked", "main only hands the reply over")
        self.assertNotIn(qid, self.questions(a))
        b._handle_inbox()
        self.assertEqual(self.questions(b)[qid]["status"], "answered")
        self.assertEqual(self.status(b), "todo")
        self.assertIn("Ben: Try the other library.", b._task("T1")["trouble_notes"])
        b._handle_inbox()  # read once only
        self.assertEqual(len([n for n in b._task("T1")["trouble_notes"] if n.startswith("Ben:")]), 1)

    def test_routed_answers_still_need_the_owner_and_the_code(self):
        a, b = self.lane("main"), self.lane("p2")
        qid = self.ask_blocked(b)
        self.reply(b, qid, "from a stranger", sender="mallory@example.com")
        self.messages.append({"from": OWNER, "subject": f"[Forge Q-{qid} WRONGCOD] x", "body": "wrong code"})
        a._handle_inbox()
        b._handle_inbox()
        self.assertEqual(self.questions(b)[qid]["status"], "open")
        routed = json.loads((a.state / "routed" / "p2.json").read_text())
        self.assertEqual(len(routed), 1, "a stranger's mail is never routed")

    def test_main_answers_its_own_questions_as_before(self):
        a, _ = self.lane("main"), self.lane("p2")
        qid = self.ask_blocked(a)
        self.reply(a, qid, "go on")
        a._handle_inbox()
        self.assertEqual(self.questions(a)[qid]["status"], "answered")
        self.assertFalse((a.state / "routed").exists())

    def test_stop_by_email_writes_the_global_kill(self):
        a, b = self.lane("main"), self.lane("p2")
        self.messages.append({"from": OWNER, "subject": "STOP", "body": ""})
        self.assertEqual(a.step(), "killed")
        self.assertTrue((self.shared / "KILL").exists())
        self.assertEqual(b.step(), "killed")

    def test_only_the_main_lane_reads_email(self):
        a, b = self.lane("main"), self.lane("p2")
        self.messages.append({"from": OWNER, "subject": "STOP", "body": ""})
        b._handle_inbox()
        self.assertEqual(len(self.messages), 1, "lane p2 never touched Ben's mailbox")
        self.assertFalse((self.shared / "KILL").exists())

    def test_qid_lane(self):
        names = ["main", "p2", "b"]
        self.assertEqual(lanes.qid_lane("p2-blocked-3", names), "p2")
        self.assertIsNone(lanes.qid_lane("blocked-3", names))
        self.assertIsNone(lanes.qid_lane("zz-blocked-3", names), "an unlisted lane gets nothing")
        self.assertIsNone(lanes.qid_lane("main-blocked-3", names))


class Names(unittest.TestCase):
    def test_lane_names(self):
        for ok in ("main", "p2", "b", "layer2_ui"):
            self.assertIsNone(lanes.name_problem(ok), ok)
        for bad in ("", "P2", "p-2", "2p", "gate", "blocked", "wake", "shared", "a" * 25, "../x"):
            self.assertIsNotNone(lanes.name_problem(bad), bad)

    def test_register_and_layer_owner(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            lanes.register(root, "p2")
            lanes.register(root, "p2")
            lanes.register(root, "b")
            self.assertEqual(lanes.listed(root), ["main", "p2", "b"])
            (root / "lanes" / "p2").mkdir(parents=True)
            (root / "lanes" / "p2" / "queue.json").write_text(json.dumps({"layer": "layer-2", "tasks": []}))
            self.assertEqual(lanes.layer_owner(root, "layer-2", "b"), "p2")
            self.assertIsNone(lanes.layer_owner(root, "layer-2", "p2"))
            with self.assertRaises(ValueError):
                lanes.register(root, "Bad-Name")


class SharedLocking(unittest.TestCase):
    def test_two_processes_incrementing_the_meter_lose_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(ROOT)!r})
                from pathlib import Path
                from core import lanes
                from core.usage import Meter
                shared = Path(sys.argv[1])
                m = Meter(shared, lock=lanes.SharedLock(shared), lane=sys.argv[2])
                for _ in range(200):
                    m.add("claude", 1)
                    lanes.update_json(shared / "counter.json", lambda d: d.__setitem__("n", d.get("n", 0) + 1),
                                      {{}}, lanes.SharedLock(shared))
            """)
            procs = [subprocess.Popen([sys.executable, "-c", script, t, lane]) for lane in ("main", "b")]
            for p in procs:
                self.assertEqual(p.wait(180), 0)
            shared = Path(t)
            self.assertEqual(service.shared_meter(shared, lambda: datetime.now(timezone.utc)).used_today("claude"), 400)
            self.assertEqual(json.loads((shared / "counter.json").read_text())["n"], 400)
            self.assertFalse([f for f in shared.rglob("*") if f.name.startswith(lanes.TMP_PREFIX)])

    def test_the_lock_is_reentrant_in_one_thread_and_exclusive_across_handles(self):
        with tempfile.TemporaryDirectory() as t:
            lock = lanes.SharedLock(Path(t))
            with lock:
                with lanes.SharedLock(Path(t)):
                    pass
                probe = textwrap.dedent(f"""
                    import sys
                    sys.path.insert(0, {str(ROOT)!r})
                    from pathlib import Path
                    from core import lanes
                    try:
                        with lanes.SharedLock(Path(sys.argv[1]), timeout_s=0.3):
                            print("got")
                    except TimeoutError:
                        print("busy")
                """)
                out = subprocess.run([sys.executable, "-c", probe, t], capture_output=True, text=True, timeout=60)
                self.assertEqual(out.stdout.strip(), "busy")
            out = subprocess.run([sys.executable, "-c", probe, t], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.stdout.strip(), "got")


class ServiceAndPage(LaneHarness):
    def test_the_watchdog_checks_every_listed_lane(self):
        self.lane("main"), self.lane("p2")
        cmds = []
        res = service.watchdog_all(self.repo.parent, {"heartbeat_stale_s": 600}, lock_free=lambda: True,
                                   run=lambda args: cmds.append(args) or 0)
        self.assertEqual(res, {"main": "started", "p2": "started"})
        self.assertIn(["schtasks", "/Run", "/TN", "Forge conductor"], cmds)
        self.assertIn(["schtasks", "/Run", "/TN", "Forge conductor p2"], cmds)

    def test_a_fresh_lane_heartbeat_is_left_alone_and_global_kill_stops_all(self):
        self.lane("main"), self.lane("p2")
        import time
        service._write_json(lanes.service_dir(self.sroot, "p2") / "heartbeat.json",
                            {"pid": 1, "at": time.time(), "phase": "step", "since": time.time()})
        cmds = []
        run = lambda args: cmds.append(args) or 0  # noqa: E731
        res = service.watchdog_all(self.repo.parent, {}, lock_free=lambda: True, run=run)
        self.assertEqual(res["p2"], "ok")
        (self.shared / "KILL").write_text("x")
        cmds.clear()
        res = service.watchdog_all(self.repo.parent, {}, lock_free=lambda: True, run=run)
        self.assertEqual(res, {"main": "stopped", "p2": "stopped"})
        self.assertEqual(cmds, [])

    def test_service_stop_writes_the_global_kill(self):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["stop", "--reason", "test"], forge=self.repo.parent), 0)
        self.assertTrue((self.shared / "KILL").exists())
        self.assertTrue((self.sroot / "bootstrap" / "KILL").exists())

    def test_service_kill_check_sees_the_global_kill(self):
        a = self.lane("p2")
        svc = service.Service(a, lanes.service_dir(self.sroot, "p2"), {})
        self.assertFalse(svc._killed())
        (self.shared / "KILL").write_text("x")
        self.assertTrue(svc._killed())
        snap = service.snapshot(a.state, lanes.service_dir(self.sroot, "p2"), CAPS, shared=self.shared)
        self.assertTrue(snap["kill"])

    def test_the_status_page_shows_every_lane_and_the_shared_usage(self):
        a = self.lane("main", test_writer=self.writer(tokens=1_500_000))
        b = self.lane("p2", test_writer=self.writer(tokens=2_500_000))
        a.step()
        b.step()
        b._send("[Forge] hi", "x")
        page = status_page.render(a.state, CAPS, shared=self.shared)
        self.assertIn("Lane p2", page)
        self.assertIn("layer-p2", page)
        self.assertIn("Now: <strong>T1</strong>", page)
        self.assertIn("4.0M of", page, "the usage bar sums both lanes")
        self.assertIn("1 of 30 today", page, "lane p2's email counts against the shared budget")
        snap = service.snapshot(b.state, lanes.service_dir(self.sroot, "p2"), CAPS, shared=self.shared)
        self.assertEqual(snap["caps"]["codex"]["used"], 4_000_000)
        self.assertEqual(snap["current"], "T1")


if __name__ == "__main__":
    unittest.main()


class ReviewRound1(LaneHarness):
    """R60 review round 1 (Codex): each test fails on the code before its fix."""

    def ready_pair(self):
        a, b = self.lane("main"), self.lane("b")
        self.assertEqual(a.step(), "worked")  # readiness evidence for both providers now exists
        self.assertEqual(b.step(), "worked")
        return a, b

    def test_fix1_the_last_run_of_the_day_is_admitted_once_across_lanes(self):
        """With one run left, lane b admits itself while lane a is between its cap check and its launch. Only one
        agent may run, and the day's runs never exceed the cap."""
        import threading
        from core.bootstrap import Capped
        a, b = self.ready_pair()
        ran = []
        for c, name in ((a, "a"), (b, "b")):
            c.team.drift_keeper = FakeAgent(lambda p, cwd, n=name: ran.append(n) or ('{"status":"ok"}', 1),
                                            provider="claude")
        cap = self.shared_meter().runs_today() + 1
        a.limits["agent_runs_per_day"] = b.limits["agent_runs_per_day"] = cap
        results, threads = [], []

        def attempt(c):
            try:
                c._call("drift_keeper", "check", None)
                return "ran"
            except Capped:
                return "capped"

        real_over = a.meter.over

        def over(provider, limits):
            res = real_over(provider, limits)
            if not threads:  # lane b's whole admission happens right after lane a's check
                t = threading.Thread(target=lambda: results.append(attempt(b)))
                threads.append(t)
                t.start()
                t.join(2)
            return res

        a.meter.over = over
        results.append(attempt(a))
        for t in threads:
            t.join(60)
        self.assertEqual(len(ran), 1, f"both lanes launched: {ran}")
        self.assertEqual(sorted(results), ["capped", "ran"])
        self.assertLessEqual(self.shared_meter().runs_today(), cap)

    def test_fix2_another_lanes_increment_written_back_lower_is_caught(self):
        """Lane main's agent copies the shared meter, lane b meters 50 tokens meanwhile, and the agent writes its
        stale copy back. Main's own before/after can't see it; the shared total must still never drop silently."""
        b = self.lane("b")
        b.meter.add("claude", 10)
        stale = {}

        def during(cwd):
            stale.update({p: p.read_bytes() for p in self.shared.rglob("*.json") if "meter" in p.as_posix()})
            b.meter.add("claude", 50)  # lane b's conductor meters a run
            for p, raw in stale.items():  # the agent puts back what it copied
                p.write_bytes(raw)

        a = self.lane("main", test_writer=self.writer(during=during))
        before_total = self.shared_meter().used_today("claude")
        status_a = a.step()
        status_b = b.step()
        alarmed = (self.shared / "KILL").exists()
        lost = self.shared_meter().used_today("claude") < before_total + 50
        self.assertTrue(alarmed or not lost, "50 tokens of lane b's usage vanished without a tamper alarm")
        self.assertEqual(status_b, "killed")
        tamper = [q for q in self.questions(b).values() if q["kind"] == "tamper"]
        self.assertTrue(tamper and "meter/b.json" in tamper[0]["body"])
        self.assertIn(status_a, ("worked", "killed"))

    def test_fix2_an_agent_lowering_another_lanes_counter_below_its_snapshot_is_caught_by_the_runner(self):
        b = self.lane("b")
        b.meter.add("claude", 500)

        def lower(cwd):
            (self.shared / "meter" / "b.json").write_text(json.dumps({today(): {"claude": 1}}))

        a = self.lane("main", test_writer=self.writer(during=lower))
        self.assertEqual(a.step(), "killed")
        tamper = [q for q in self.questions(a).values() if q["kind"] == "tamper"]
        self.assertIn("shared/meter/b.json", tamper[0]["body"])

    def test_fix3_unreadable_shared_accounting_admits_nothing_and_is_never_overwritten(self):
        calls = []
        a = self.lane("main", test_writer=lambda p, c: calls.append(1) or ('{"files":[]}', 1))
        b = self.lane("b")
        b.meter.add("claude", 7)
        bad = sorted(p for p in self.shared.rglob("*.json") if "meter" in p.as_posix())
        for p in bad:
            p.write_text("{not json")
        self.assertEqual(a.step(), "not_ready")
        self.assertEqual(calls, [], "no agent may launch while usage can't be counted")
        self.assertTrue(a.meter.over("codex", a.limits), "the caps fail closed")
        self.assertEqual(a.step(), "not_ready")
        qs = [q for q in self.questions(a).values() if q["kind"] == "accounting"]
        self.assertEqual(len(qs), 1, "Ben is asked once")
        self.assertTrue(any("accounting" in s.lower() or "usage" in s.lower() for s, _ in self.mails["main"]))
        for p in bad:
            self.assertEqual(p.read_text(), "{not json", f"{p.name} was overwritten")
        self.assertIn("accounting can't be trusted", (a.state / "errors.log").read_text())

    def test_fix3_an_agent_corrupting_another_lanes_accounting_is_tampering(self):
        b = self.lane("b")
        b._send("[Forge] x", "y")
        a = self.lane("main", test_writer=self.writer(
            during=lambda cwd: (self.shared / "mail" / "b.json").write_text("garbage")))
        self.assertEqual(a.step(), "killed")

    def _main_cli(self, argv):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from core import bootstrap
        out = io.StringIO()
        patches = [patch("core.bootstrap.__file__", str(self.repo / "core" / "bootstrap.py")),
                   patch("core.agents.load_limits", return_value=dict(CAPS)),
                   patch("core.bootstrap.real_team", return_value=self.lane_team()),
                   patch("core.bootstrap.real_checks", return_value=dict(HEALTHY_CHECKS)),
                   patch("core.bootstrap.real_probes", side_effect=lambda limits: healthy_probes()),
                   patch("core.bootstrap.real_manager", return_value=None),
                   patch("core.bootstrap.gmail_mailer", return_value=lambda s, b: None),
                   patch("core.bootstrap.gmail_inbox", return_value=lambda: []),
                   patch("core.bootstrap.gh_cli", return_value=lambda args: (0, ""))]
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out):
                rc = bootstrap.main(argv + ["--work", str(self.work_root)])
        finally:
            for p in patches:
                p.stop()
        return rc, out.getvalue()

    def lane_team(self):
        noop = lambda p, c: ('{"status":"ok"}', 1)  # noqa: E731
        return Team(*[FakeAgent(noop, provider=pr) for pr in ("codex", "claude", "codex", "claude", "claude",
                                                             "claude")])

    def test_fix4_init_never_replaces_a_queue_with_tasks_unless_forced(self):
        tasks = Path(self.tmp.name) / "tasks.json"
        tasks.write_text(json.dumps([self.task()]))
        other = Path(self.tmp.name) / "other.json"
        other.write_text(json.dumps([self.task(id="X9")]))
        qfile = self.repo / "state" / "lanes" / "p2" / "queue.json"
        self.assertEqual(self._main_cli(["init", "--lane", "p2", "--layer", "phase-2", "--tasks", str(tasks)])[0], 0)
        self.assertEqual(json.loads(qfile.read_text())["tasks"][0]["id"], "T1")
        rc, out = self._main_cli(["init", "--lane", "p2", "--layer", "phase-2", "--tasks", str(other)])
        self.assertEqual(rc, 2)
        self.assertIn("--force", out)
        self.assertEqual(json.loads(qfile.read_text())["tasks"][0]["id"], "T1", "the queue was replaced")
        rc, _ = self._main_cli(["init", "--layer", "layer-1", "--tasks", str(tasks)])  # main's own, empty: fine
        self.assertEqual(rc, 0)
        rc, _ = self._main_cli(["init", "--layer", "phase-2b", "--tasks", str(other)])  # main has tasks now
        self.assertEqual(rc, 2)
        rc, _ = self._main_cli(["init", "--lane", "p2", "--layer", "phase-2", "--tasks", str(other), "--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(qfile.read_text())["tasks"][0]["id"], "X9")

    def test_fix4_the_phase_2_start_command_names_its_lane(self):
        text = (ROOT / "docs" / "specs" / "phase-2-design.md").read_text(encoding="utf-8")
        for line in [ln for ln in text.splitlines() if "core.bootstrap init" in ln]:
            self.assertIn("--lane p2", line)
            self.assertIn("--spec docs/specs/phase-2-design.md", line)

    def test_fix5_main_stopped_alone_still_reads_mail_for_the_other_lanes(self):
        a, b = self.lane("main"), self.lane("p2")
        q = b._queue()
        q["tasks"][0]["status"] = "blocked"
        b._save_queue(q)
        qid = b._ask("blocked", "Task T1 is blocked", "details", task="T1")
        mine = a._ask("blocked", "main's own", "details", task="T1")
        (a.state / "KILL").write_text("Ben stopped main only\n")
        code = self.questions(b)[qid]["code"]
        self.messages += [{"from": OWNER, "subject": f"Re: [Forge Q-{qid} {code}] x", "body": "use plan B"},
                          {"from": OWNER, "subject": f"Re: [Forge Q-{mine} {self.questions(a)[mine]['code']}] x",
                           "body": "later"}]
        self.assertEqual(a.step(), "killed")
        self.assertEqual(self.messages, [], "main read the mailbox although its own KILL is set")
        b._handle_inbox()
        self.assertEqual(self.questions(b)[qid]["status"], "answered", "the answer reached lane p2")
        self.assertEqual(self.questions(a)[mine]["status"], "open", "main answers nothing while stopped")
        self.assertEqual(len(a._read("inbox_pending.json", [])), 1, "main's own reply waits for its restart")
        self.assertFalse((self.shared / "KILL").exists())
        self.messages.append({"from": OWNER, "subject": "STOP", "body": ""})
        self.assertEqual(a.step(), "killed")
        self.assertTrue((self.shared / "KILL").exists(), "an owner STOP still stops every lane")
        self.assertEqual(b.step(), "killed")

    def test_fix5_main_stopped_alone_keeps_its_loop_and_the_watchdog_keeps_it_up(self):
        a, _ = self.lane("main"), self.lane("p2")
        (a.state / "KILL").write_text("main only\n")
        sleeps = []
        self.assertEqual(a.run(max_steps=3, sleep=sleeps.append), "killed")
        self.assertEqual(len(sleeps), 3, "the loop kept going as the mailbox reader")
        res = service.watchdog_all(self.repo.parent, {"heartbeat_stale_s": 600}, lock_free=lambda: True,
                                   run=lambda args: 0)
        self.assertEqual(res["main"], "started")
        self.assertTrue(service.Service(a, lanes.service_dir(self.sroot, "main"), {})._killed() is False)
        (self.shared / "KILL").write_text("all\n")
        self.assertEqual(a.run(max_steps=3, sleep=sleeps.append), "killed")
        self.assertEqual(len(sleeps), 3, "the global KILL ends the loop at once")
        self.assertEqual(service.watchdog_all(self.repo.parent, {}, lock_free=lambda: True,
                                              run=lambda args: 0)["main"], "stopped")
