"""R66 contracts, written before implementation. All external services are fakes."""
import hashlib
import importlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core import bootstrap, channel, drift, lanes
from core.agents import FakeAgent
from core.bootstrap import Conductor, Team
from core.finalize import ApprovedMerges


NOW = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
REPO_LINE = r"C:\Users\benja\Forge"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True, timeout=5).stdout.strip()


def good_kit(**changes):
    kit = dict(why="The automatic attempts ran out", category="exhausted",
               where="PowerShell", paste="python -m pip install keyring",
               expect="The check becomes green")
    kit.update(changes)
    return kit


class BlockerFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "bootstrap"
        self.state.mkdir()
        self.now = NOW
        # Import in setup so the pre-R66 conductor tests also run in the red run.
        self.mod = importlib.import_module("core.blockers")
        self.store = self.mod.Blockers(self.state, clock=lambda: self.now)

    def test_constants(self):
        for name, value in dict(
                BEN_ONLY=("money", "account", "credentials", "message", "legal", "unpark", "hardware", "admin"),
                STATUSES=("fixing", "ready_for_ben", "fixed"), NO_SCRIPT=("credentials", "admin", "unpark"),
                SUMMARY_CAP=200, PASTE_CAP=500, FIXED_KEEP=50, OUTBOX_KEEP=50, FIX_TIMEOUT_S=600).items():
            self.assertEqual(getattr(self.mod, name), value, name)

    def test_secret_detection_and_ordinary_password_phrase(self):
        for text in ("password=hunter2", "token: abc123def", "ghp_" + "a" * 36,
                     "github_pat_" + "a" * 30, "sk-" + "a" * 32,
                     "xoxb-123-456-abc", "-----BEGIN PRIVATE KEY-----"):
            with self.subTest(secret=text[:15]):
                self.assertTrue(self.mod.has_secret(text))
        for text in ("python -m pip install keyring", "the app password",
                     r"cd C:\Users\benja\Forge; python -m core.bootstrap retry --lane main b-1"):
            self.assertFalse(self.mod.has_secret(text), text)

    def test_kit_validation_boundaries_and_command_quotes(self):
        for kit in (good_kit(), good_kit(paste='cmd /c "gh pr reopen 12"'),
                    good_kit(paste="x" * 500), good_kit(where="Win + R", category="account")):
            self.assertEqual(self.mod.validate_kit(kit), [])
        bad = [good_kit(category="build"), good_kit(where="Terminal"),
               good_kit(paste="one\ntwo"), good_kit(paste="one\rtwo"), good_kit(paste="x" * 501),
               good_kit(paste=r'cd "C:\Users\benja\Forge"'), good_kit(paste=r"cd 'C:\x'")]
        for field in ("why", "paste", "expect"):
            kit = good_kit()
            del kit[field]
            bad.append(kit)
        for field in ("why", "category", "where", "paste", "expect", "script"):
            bad.append(good_kit(**{field: "password=hunter2"}))
        for category in ("credentials", "admin", "unpark"):
            bad.append(good_kit(category=category, script="Write-Output ok"))
        for kit in bad:
            with self.subTest(kit=kit):
                self.assertTrue(self.mod.validate_kit(kit))

    def test_record_lifecycle_deduplication_and_persistence(self):
        rec = self.store.open("blocked", "T1", "first\r\n" + "x" * 220)
        self.assertEqual((rec["id"], rec["lane"], rec["kind"], rec["key"]),
                         ("b-1", "main", "blocked", "T1"))
        self.assertEqual((rec["status"], rec["attempts"]), ("fixing", 0))
        self.assertEqual(len(rec["summary"]), 200)
        self.assertNotIn("\n", rec["summary"])
        self.assertNotIn("\r", rec["summary"])
        self.assertRegex(rec["code"], r"^[A-Za-z0-9_-]{8}$")
        for field in ("created_at", "updated_at"):
            self.assertEqual(datetime.fromisoformat(rec[field]), NOW)
        self.now += timedelta(seconds=1)
        updated = self.store.open("blocked", "T1", "refreshed")
        self.assertEqual(updated["id"], rec["id"])
        self.assertEqual(updated["summary"], "refreshed")
        self.assertEqual(datetime.fromisoformat(updated["updated_at"]), self.now)
        self.assertEqual(self.store.attempt(rec["id"])["attempts"], 1)
        self.assertEqual(self.store.attempt(rec["id"])["attempts"], 2)
        ready = self.store.ready_for_ben(rec["id"], good_kit())
        self.assertEqual((ready["status"], ready["category"], ready["kit"]),
                         ("ready_for_ben", "exhausted", good_kit()))
        again = self.store.back_to_fixing(rec["id"])
        self.assertEqual((again["status"], again["attempts"]), ("fixing", 0))
        fixed = self.store.fixed(rec["id"])
        self.assertEqual(fixed["status"], "fixed")
        self.assertEqual(datetime.fromisoformat(fixed["fixed_at"]), self.now)
        self.assertIsNone(self.store.find("blocked", "T1"))
        self.assertEqual(self.store.active(), [])
        new = self.store.open("blocked", "T1", "again")
        self.assertEqual(new["id"], "b-2")
        loaded = self.mod.Blockers(self.state)
        self.assertEqual(loaded.get("b-2"), new)
        self.assertEqual(loaded.find("blocked", "T1"), new)
        disk = json.loads((self.state / "blockers.json").read_text(encoding="utf-8"))
        self.assertEqual(disk, {"seq": 2, "items": loaded.all()})

    def test_lane_ids_are_independent(self):
        other = self.mod.Blockers(self.root / "lanes/p2", lane="p2", clock=lambda: NOW)
        self.assertEqual(other.open("sync", "sync", "conflict")["id"], "p2-b-1")
        self.assertEqual(self.store.open("sync", "sync", "conflict")["id"], "b-1")

    def test_invalid_ready_transition_is_atomic(self):
        rec = self.store.open("blocked", "T1", "blocked")
        before = (self.state / "blockers.json").read_bytes()
        with self.assertRaises(ValueError):
            self.store.ready_for_ben(rec["id"], good_kit(script="token: abc123def"))
        self.assertEqual(self.store.get(rec["id"]), rec)
        self.assertEqual((self.state / "blockers.json").read_bytes(), before)
        self.assertFalse((self.state / "fixkits" / (rec["id"] + ".ps1")).exists())

    def test_script_is_lf_and_hash_is_of_actual_bytes(self):
        rec = self.store.open("gate", "7", "merge failed")
        ready = self.store.ready_for_ben(rec["id"], good_kit(script="Write-Output one\r\nWrite-Output two\r\n"))
        kit = ready["kit"]
        self.assertEqual(kit["script_file"], "fixkits/b-1.ps1")
        data = (self.state / kit["script_file"]).read_bytes()
        self.assertEqual(data, b"Write-Output one\nWrite-Output two\n")
        self.assertEqual(kit["script_sha256"], hashlib.sha256(data).hexdigest())

    def test_pruning_keeps_all_active_and_newest_fifty_fixed(self):
        active = self.store.open("blocked", "active", "leave me")
        fixed = []
        for n in range(53):
            rec = self.store.open("blocked", str(n), str(n))
            self.now += timedelta(seconds=1)
            self.store.fixed(rec["id"])
            fixed.append(rec["id"])
        self.assertEqual(set(self.store.all()), {active["id"], *fixed[-50:]})
        self.assertEqual([r["id"] for r in self.store.active()], [active["id"]])

    def test_mail_and_retry_kit_contracts(self):
        rec = self.store.open("blocked", "T1", "Build failed")
        subject, body = self.mod.opened_mail(rec, "http://127.0.0.1:8765")
        self.assertEqual(subject, "[Forge] Blocked: Build failed — fixing it myself, nothing for you to do")
        for not_needed in (False, True):
            line = (f"cd {REPO_LINE}; python -m core.bootstrap retry --lane main b-1" +
                    (" --not-needed" if not_needed else ""))
            self.assertEqual(self.mod.retry_line(REPO_LINE, "main", "b-1", not_needed=not_needed), line)
            kit = self.mod.retry_kit(REPO_LINE, "main", "b-1", "Needs another try", "Task resumes",
                                     not_needed=not_needed)
            self.assertEqual(self.mod.validate_kit(kit), [])
            self.assertEqual((kit["category"], kit["where"], kit["paste"], kit["script"]),
                             ("exhausted", "PowerShell", line, line))
        rec = self.store.ready_for_ben("b-1", good_kit(script="Write-Output one\nWrite-Output two"))
        subject, body = self.mod.opened_mail(rec, "http://127.0.0.1:8765")
        self.assertEqual(subject, "[Forge] Needs you (~2 min): Build failed")
        for value in (rec["kit"]["why"], "PowerShell", rec["kit"]["script"], rec["kit"]["expect"],
                      "http://127.0.0.1:8765", "Do it", "reply STOP"):
            self.assertIn(value, body)
        self.assertIn(rec["kit"]["paste"], body.splitlines())
        self.assertNotIn("reply y", body.lower())
        self.assertNotIn("reply with guidance", body.lower())
        self.assertEqual(self.mod.fixed_mail(self.store.fixed("b-1"))[0], "[Forge] Fixed: Build failed")

    def ready_script(self, lane="main"):
        store = self.mod.Blockers(lanes.state_dir(self.root, lane), lane=lane, clock=lambda: NOW)
        rec = store.open("blocked", "T1", "retry")
        return store.ready_for_ben(rec["id"], good_kit(script="Write-Output test"))

    def wait_finished(self, bid):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = self.mod.run_status(self.root, bid)
            if result and result.get("finished_at"):
                return result
            time.sleep(0.02)
        self.fail("fake runner did not finish within four seconds")

    def test_run_fix_background_exclusion_log_tail_and_lane_paths(self):
        rec = self.ready_script("p2")
        entered, release = threading.Event(), threading.Event()
        calls = []

        def runner(script, log, timeout_s):
            calls.append((script, log, timeout_s, threading.get_ident()))
            entered.set()
            if not release.wait(4):
                return 99
            log.write_bytes(("x" * 4500 + "THE END").encode())
            return 7

        bid, sha = rec["id"], rec["kit"]["script_sha256"]
        self.assertEqual(self.mod.fixruns_dir(self.root), self.root / "channel/fixruns")
        self.assertIsNone(self.mod.run_status(self.root, "missing"))
        try:
            self.assertTrue(self.mod.run_fix(self.root, "p2", bid, sha, runner=runner)[0])
            self.assertTrue(entered.wait(1))
            running = self.mod.run_status(self.root, bid)
            self.assertIsNone(running["finished_at"])
            self.assertTrue(running["started_at"])
            self.assertEqual(running["sha"], sha)
            self.assertEqual(running["log"], "")
            started, reason = self.mod.run_fix(self.root, "p2", bid, sha, runner=runner)
            self.assertFalse(started)
            self.assertTrue(reason)
        finally:
            release.set()
            self.wait_finished(bid)
        result = self.mod.run_status(self.root, bid)
        self.assertEqual(result["exit"], 7)
        self.assertEqual(result["log"], ("x" * 4500 + "THE END")[-4000:])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], (self.root / "lanes/p2" / rec["kit"]["script_file"],
                                      self.root / "channel/fixruns" / (bid + ".log"), 600))
        self.assertNotEqual(calls[0][3], threading.get_ident())
        self.assertTrue((self.root / "channel/fixruns" / (bid + ".json")).is_file())

    def test_run_fix_refuses_unknown_wrong_lane_not_ready_no_script_bad_hash_and_missing_file(self):
        runner = Mock(side_effect=AssertionError("refused run reached runner"))
        rec = self.ready_script()
        bid, sha = rec["id"], rec["kit"]["script_sha256"]
        for lane, rid, digest in (("main", "missing", sha), ("p2", bid, sha), ("main", bid, "0" * 64)):
            started, reason = self.mod.run_fix(self.root, lane, rid, digest, runner=runner)
            self.assertFalse(started)
            self.assertTrue(reason)
        path = self.state / rec["kit"]["script_file"]
        path.write_bytes(b"changed")
        self.assertFalse(self.mod.run_fix(self.root, "main", bid, sha, runner=runner)[0])
        # Even the current file hash must match the stored hash.
        self.assertFalse(self.mod.run_fix(self.root, "main", bid, hashlib.sha256(b"changed").hexdigest(), runner=runner)[0])
        path.unlink()
        self.assertFalse(self.mod.run_fix(self.root, "main", bid, sha, runner=runner)[0])
        for status in ("fixing", "fixed"):
            rec = self.ready_script()
            getattr(self.store, "back_to_fixing" if status == "fixing" else "fixed")(rec["id"])
            self.assertFalse(self.mod.run_fix(self.root, "main", rec["id"], rec["kit"]["script_sha256"], runner=runner)[0])
        rec = self.store.open("capability", "gmail", "credentials")
        self.store.ready_for_ben(rec["id"], good_kit(category="credentials"))
        self.assertFalse(self.mod.run_fix(self.root, "main", rec["id"], sha, runner=runner)[0])
        runner.assert_not_called()

    def test_retry_request_only_writes_drop_folder_for_ready_records(self):
        for lane in ("main", "p2"):
            rec = self.ready_script(lane)
            state = lanes.state_dir(self.root, lane)
            before = {p.relative_to(state): p.read_bytes() for p in state.rglob("*") if p.is_file()}
            for not_needed in (False, True):
                code, message = bootstrap.retry_request(self.root, lane, rec["id"], not_needed=not_needed, wait_s=0)
                self.assertEqual(code, 0)
                self.assertTrue(message)
                answers = channel.take_answers(lanes.channel_dir(self.root, lane) / "in")
                self.assertEqual(len(answers), 1)
                self.assertEqual({k: answers[0][k] for k in ("qid", "code", "answer", "source")},
                                 dict(qid=rec["id"], code=rec["code"], source="fix kit",
                                      answer="not needed" if not_needed else "retry"))
            self.assertEqual(before, {p.relative_to(state): p.read_bytes() for p in state.rglob("*") if p.is_file()})
            store = self.mod.Blockers(state, lane=lane)
            for status in ("fixing", "fixed"):
                getattr(store, "back_to_fixing" if status == "fixing" else "fixed")(rec["id"])
                self.assertEqual(bootstrap.retry_request(self.root, lane, rec["id"], wait_s=0)[0], 2)
            self.assertEqual(bootstrap.retry_request(self.root, lane, "unknown", wait_s=0)[0], 2)
            self.assertEqual(channel.take_answers(lanes.channel_dir(self.root, lane) / "in"), [])


class ConductorFixture(unittest.TestCase):
    """Small standalone version of test_bootstrap's real-git/fake-Team harness."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo, self.work = self.root / "repo", self.root / "work"
        self.state = self.repo / "state/bootstrap"
        self.repo.mkdir()
        self.work.mkdir()
        self.state.mkdir(parents=True)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Forge Test")
        git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "README.md").write_bytes(b"initial\n")
        (self.repo / ".gitignore").write_bytes(b"state/\n")
        spec = self.repo / "docs/specs/layer-1-design.md"
        spec.parent.mkdir(parents=True)
        spec.write_bytes(b"# Spec\n\n## 1. Alpha\n\n- First requirement\n- Second requirement\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")
        self.now, self.mails, self.gh_calls = NOW, [], []
        self.pr_state, self.merge_exit = "OPEN", 0
        self.layer = "layer-1"
        guard = patch("core.agents.launch", side_effect=AssertionError("real agent launched"))
        guard.start()
        self.addCleanup(guard.stop)

    def gh(self, args):
        self.gh_calls.append(list(args))
        if args[:2] == ["pr", "create"]:
            return 0, "https://github.com/o/r/pull/7"
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps({"state": self.pr_state})
        if args[:2] == ["pr", "merge"]:
            return self.merge_exit, "merge unavailable" if self.merge_exit else ""
        return 0, ""

    def conductor(self, self_fix=True, agents=None, checks=None):
        scripts = dict(agents or {})
        scripts.setdefault("reviewer", lambda p, c: ('{"verdict":"pass","reasons":[]}', 1))
        members = {role: FakeAgent(scripts.get(role, lambda p, c: ('{"status":"ok"}', 1)),
                                  provider="codex" if role in ("reviewer", "test_writer") else "claude")
                   for role in Team.__dataclass_fields__}
        limits = dict(claude_daily_token_cap=10**9, codex_daily_token_cap=10**9,
                      mail_per_hour=100, mail_per_day=100, self_fix_attempts=2,
                      replan_recuts=2, sync_resolve_tries=2)
        if self_fix:
            limits["self_fix"] = True
        healthy = {name: lambda: (True, "ok") for name in
                   ("git", "github", "gmail", "docker", "n8n", "ollama", "python_libs", "browser")}
        healthy.update(checks or {})
        probes = {name: FakeAgent(lambda p, c: ("ok", 0), provider=name) for name in ("claude", "codex")}
        c = Conductor(self.repo, self.work, self.state, Team(**members), limits,
                      owner_email="ben@example.com", mailer=lambda s, b: self.mails.append((s, b)),
                      inbox=lambda: [], gh=self.gh, clock=lambda: self.now, judge_cmds=[], push=False,
                      checks=healthy, probes=probes)
        c.init_queue(self.layer, [self.task("T1")])
        return c

    @staticmethod
    def task(tid):
        return dict(id=tid, kind="build", title="Build " + tid, section="Implement the requirement",
                    files_in_scope=[f"m_{tid.lower()}.py"], test_files=[f"tests/core/test_{tid.lower()}.py"],
                    test_cmd=f"python -m unittest tests/core/test_{tid.lower()}.py", covers=["1.1"])

    def store(self):
        return importlib.import_module("core.blockers").Blockers(self.state, clock=lambda: self.now)

    def questions(self, c, kind=None):
        return {k: v for k, v in c._read("questions.json", {}).items() if kind is None or v["kind"] == kind}

    def only_blocker(self, kind):
        records = [r for r in self.store().all().values() if r["kind"] == kind]
        self.assertEqual(len(records), 1, records)
        return records[0]


class TaskBlockers(ConductorFixture):
    def test_policy_absent_preserves_legacy_blocked_question(self):
        c = self.conductor(self_fix=False)
        c._block("T1", "cannot build")
        self.assertEqual(c._task("T1")["status"], "blocked")
        self.assertEqual(len(self.questions(c, "blocked")), 1)
        self.assertFalse((self.state / "blockers.json").exists())

    def test_auto_reopen_resets_counters_then_exhausts_without_question(self):
        c = self.conductor()
        for commit, status in ((None, "todo"), (git(self.repo, "rev-parse", "HEAD"), "tests_ok")):
            c._update("T1", tests_commit=commit, fails_since=3, troubleshot=True, troubleshoots=3,
                      test_rejects=2, plan_rejects=2, focus_s=1200, fail_signatures=["same"])
            c._block("T1", "repeated failure")
            t = c._task("T1")
            self.assertEqual(t["status"], status)
            self.assertEqual(t["auto_reopens"], 1 if commit is None else 2)
            for field in ("fails_since", "troubleshoots", "test_rejects", "plan_rejects", "focus_s"):
                self.assertEqual(t[field], 0, field)
            self.assertFalse(t["troubleshot"])
            self.assertEqual(t["fail_signatures"], [])
            self.assertIn("AUTO-REOPEN", " ".join(t["trouble_notes"]))
            self.assertEqual(self.only_blocker("blocked")["status"], "fixing")
        c._block("T1", "still broken")
        rec = self.only_blocker("blocked")
        self.assertEqual(c._task("T1")["status"], "blocked")
        self.assertEqual((rec["status"], rec["category"]), ("ready_for_ben", "exhausted"))
        self.assertEqual(rec["kit"]["paste"],
                         f"cd {self.repo}; python -m core.bootstrap retry --lane main {rec['id']}")
        self.assertEqual(self.questions(c, "blocked"), {})
        c._update("T1", status="done")
        c._blockers_tick()
        self.assertEqual(self.store().get(rec["id"])["status"], "fixed")

    def test_parked_contract_requires_email_unpark_without_script(self):
        c = self.conductor()
        c._block("T1", "ledger parked the contract")
        rec = self.only_blocker("blocked")
        self.assertEqual(c._task("T1")["status"], "blocked")
        self.assertEqual(c._task("T1").get("auto_reopens", 0), 0)
        self.assertEqual((rec["status"], rec["category"]), ("ready_for_ben", "unpark"))
        self.assertEqual(rec["kit"]["where"], "Win + R")
        self.assertTrue(rec["kit"]["paste"].startswith("https://mail.google.com/mail/"))
        self.assertFalse(rec["kit"].get("script_file"))
        self.assertFalse(rec["kit"].get("script"))
        qs = self.questions(c, "unpark")
        self.assertEqual(len(qs), 1)
        self.assertTrue(next(iter(qs.values()))["auto"])

    def test_drop_retry_requires_matching_code_and_ready_status(self):
        c = self.conductor()
        c._update("T1", auto_reopens=2)
        c._block("T1", "exhausted")
        rec = self.only_blocker("blocked")
        before = c._task("T1")
        channel.drop_answer(c.channel_in, rec["id"], "wrongcod", "retry", "fix kit")
        c._take_channel_answers()
        self.assertEqual(c._task("T1"), before)
        self.assertEqual(self.store().get(rec["id"]), rec)
        channel.drop_answer(c.channel_in, rec["id"], rec["code"], "retry", "fix kit")
        c._take_channel_answers()
        self.assertEqual((c._task("T1")["status"], c._task("T1")["auto_reopens"]), ("todo", 0))
        after = self.store().get(rec["id"])
        self.assertEqual((after["status"], after["attempts"]), ("fixing", 0))
        c._update("T1", auto_reopens=1)
        channel.drop_answer(c.channel_in, rec["id"], rec["code"], "retry", "fix kit")
        c._take_channel_answers()
        self.assertEqual(c._task("T1")["auto_reopens"], 1)
        self.assertEqual(self.store().get(rec["id"]), after)


class GateBlockers(ConductorFixture):
    def test_gate_labels_and_auto_merges_and_watch_is_rate_limited(self):
        c = self.conductor()
        c._update("T1", status="done")
        c._gate()
        self.assertIn(["pr", "edit", "7", "--add-label", "human-approved"], self.gh_calls)
        self.assertIn(["pr", "merge", "7", "--merge", "--delete-branch", "--auto"], self.gh_calls)
        qs = self.questions(c, "gate")
        self.assertEqual(len(qs), 1)
        qid, q = next(iter(qs.items()))
        self.assertEqual(q["status"], "open")
        for field in ("auto", "hold", "delivered"):
            self.assertTrue(q[field])
        self.assertNotIn(qid, [item["id"] for item in channel.queue_items(qs)])
        c._blockers_tick()
        self.assertTrue(any("passed its gate" in s for s, _ in self.mails))
        self.assertFalse(any("reply y" in s.lower() for s, _ in self.mails))
        views = lambda: sum(a[:2] == ["pr", "view"] for a in self.gh_calls)
        count = views()
        c._blockers_tick()
        self.assertEqual(views(), count)
        self.pr_state = "MERGED"
        self.now += timedelta(seconds=300)
        c._blockers_tick()
        self.assertEqual(views(), count + 1)
        self.assertEqual(self.questions(c)[qid]["status"], "answered")
        self.assertTrue(any("is merged into main" in s for s, _ in self.mails))

    def test_failed_auto_merge_uses_bounded_watch_retries(self):
        self.merge_exit = 1
        c = self.conductor()
        c._update("T1", status="done")
        c._gate()
        self.assertEqual(self.only_blocker("gate")["status"], "fixing")
        for attempt in range(2):
            self.now += timedelta(seconds=301)
            c._blockers_tick()
            self.assertEqual(self.only_blocker("gate")["status"], "fixing" if attempt == 0 else "ready_for_ben")
        self.assertEqual(sum(a[:2] == ["pr", "merge"] for a in self.gh_calls), 3)
        self.assertEqual(self.only_blocker("gate")["category"], "exhausted")


class PlanningBlockers(ConductorFixture):
    def pending(self, answers):
        c = self.conductor()
        c.manager = FakeAgent(lambda p, cwd: (json.dumps(answers.pop(0)), 1), provider="claude")
        c._refresh_readiness()
        d = drift.adopt([], [], False, 0)
        drift.new_replan(d, ["coverage stalled"], "drift keeper")
        drift.save(self.state, d)
        return c

    def test_two_rejections_recut_and_plain_code_rejects_oversized_proposal(self):
        answers = [{"tasks": []}, {"tasks": []}]
        c = self.pending(answers)
        c._replan_stage()
        c._replan_stage()
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(self.questions(c, "replan"), {})
        rp = drift.load(self.state)["replan"]
        self.assertEqual((rp["recuts"], rp["attempts"], rp["max_tasks"]), (1, 0, 3))
        self.assertIn("AUTO RE-CUT 1", " ".join(rp["reasons"]))
        self.assertEqual(self.only_blocker("replan")["status"], "fixing")
        answers.append({"tasks": [self.task(f"M{i}") for i in range(4)], "reasons": ["cover remaining work"]})
        c._replan_stage()
        self.assertIn("too many tasks for a re-cut", " ".join(drift.load(self.state)["replan"]["notes"]))
        self.assertEqual([t["id"] for t in c._queue()["tasks"]], ["T1"])

    def test_two_recuts_exhaust_to_a_fix_kit_without_pausing(self):
        c = self.pending([{"tasks": []} for _ in range(6)])
        for n in range(6):
            c._replan_stage()
            self.assertFalse((self.state / "PAUSED").exists())
            self.assertEqual(self.questions(c, "replan"), {})
            if n == 3:
                rp = drift.load(self.state)["replan"]
                self.assertEqual((rp["recuts"], rp["max_tasks"], rp["attempts"]), (2, 2, 0))
        self.assertIsNone(drift.load(self.state)["replan"])
        self.assertEqual(self.only_blocker("replan")["status"], "ready_for_ben")

    def test_three_unusable_drift_results_create_fix_kit_without_question(self):
        c = self.conductor(agents={"drift_keeper": lambda p, cwd: ("not JSON", 1)})
        c._refresh_readiness()
        for _ in range(3):
            c._drift_check()
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertEqual(self.questions(c), {})
        self.assertEqual(self.only_blocker("drift")["status"], "ready_for_ben")


class MailCapabilityMigration(ConductorFixture):
    def test_outbox_sends_opened_and_fixed_once(self):
        c = self.conductor()
        c._block("T1", "cannot build")
        self.assertTrue((self.state / "outbox.json").exists())
        rec = self.only_blocker("blocked")
        c._blockers_tick()
        c._blockers_tick()
        expected = importlib.import_module("core.blockers").opened_mail(rec, "http://127.0.0.1:8765")[0]
        self.assertEqual([s for s, _ in self.mails], [expected])
        c._update("T1", status="done")
        c._blockers_tick()
        c._blockers_tick()
        self.assertEqual([s for s, _ in self.mails], [expected, "[Forge] Fixed: " + rec["summary"]])

    def test_fixed_before_delivery_discards_both_emails(self):
        c = self.conductor()
        c.limits["mail_per_hour"] = 0
        c._block("T1", "cannot build")
        c._blockers_tick()
        self.assertEqual(self.mails, [])
        c._update("T1", status="done")
        c._blockers_tick()
        c.limits["mail_per_hour"] = 100
        self.now += timedelta(hours=2)
        c._blockers_tick()
        self.assertEqual(self.mails, [])

    def test_kill_prevents_outbox_delivery(self):
        c = self.conductor()
        (self.state / "KILL").write_bytes(b"stop\n")
        c._block("T1", "cannot build")
        c._blockers_tick()
        self.assertEqual(self.mails, [])

    def test_gmail_without_password_gets_credentials_kit_and_internal_question(self):
        c = self.conductor(checks={"gmail": lambda: (False, "no app password in Windows Credential Manager (forge-gmail)")})
        cap_map = c._refresh_readiness()
        c._route_capabilities(cap_map)
        rec = self.only_blocker("capability")
        self.assertEqual(rec["key"], "gmail")
        self.assertEqual((rec["status"], rec["category"]), ("ready_for_ben", "credentials"))
        self.assertFalse(rec["kit"].get("script"))
        self.assertFalse(rec["kit"].get("script_file"))
        qs = list(self.questions(c, "capability").values())
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["condition"], "no_password")
        self.assertTrue(qs[0]["auto"])
        c._capability_mail_check()
        self.assertFalse(any("[Forge Q-" in s for s, _ in self.mails))

    def test_migration_removes_only_forge_pause_and_runs_once(self):
        c = self.conductor()
        c._write("questions.json", {
            "replan-1": dict(kind="replan", status="open", code="abcdefgh", subject="replan", body="help"),
            "gate-2": dict(kind="gate", status="open", code="ijklmnop", subject="gate", body="report", pr="7")})
        (self.state / "PAUSED").write_bytes(b"a re-plan needs Ben\n")
        c._blockers_tick()
        self.assertFalse((self.state / "PAUSED").exists())
        self.assertNotEqual(self.questions(c)["replan-1"]["status"], "open")
        self.assertTrue(self.questions(c)["gate-2"]["auto"])
        self.assertTrue((self.state / "r66.json").exists())
        qs = self.questions(c)
        qs["replan-3"] = dict(kind="replan", status="open", code="qrstuvwx", subject="later", body="later")
        c._write("questions.json", qs)
        (self.state / "PAUSED").write_bytes(b"a re-plan needs Ben\n")
        c._blockers_tick()
        self.assertTrue((self.state / "PAUSED").exists())
        self.assertEqual(self.questions(c)["replan-3"]["status"], "open")

    def test_migration_preserves_bens_pause(self):
        c = self.conductor()
        c._write("questions.json", {"replan-1": dict(kind="replan", status="open", code="abcdefgh",
                                                     subject="replan", body="help")})
        (self.state / "PAUSED").write_bytes(b"paused by Ben\n")
        c._blockers_tick()
        self.assertEqual((self.state / "PAUSED").read_bytes(), b"paused by Ben\n")
        self.assertNotEqual(self.questions(c)["replan-1"]["status"], "open")


class SyncBlockers(ConductorFixture):
    def conflict(self, resolver):
        # Every remote is a temporary local bare repository, never a network URL.
        origin = self.root / "origin.git"
        git(self.root, "init", "--bare", "-b", "main", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "main")
        c = self.conductor(agents={"troubleshooter": resolver})
        c._refresh_readiness()
        (c.wt / "README.md").write_bytes(b"layer version\n")
        git(c.wt, "commit", "-qam", "layer change")
        (self.repo / "README.md").write_bytes(b"main version\n")
        git(self.repo, "commit", "-qam", "main change")
        git(self.repo, "push", "-q", "origin", "main")
        return c, git(c.wt, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "HEAD")

    def test_troubleshooter_resolution_is_reviewed_and_registered_merge(self):
        seen = []

        def resolve(prompt, cwd):
            seen.append(Path(cwd))
            (Path(cwd) / "README.md").write_bytes(b"both versions reconciled\n")
            git(cwd, "add", "README.md")
            return '{"kind":"fix","notes":"resolved conflict"}', 1

        c, old, main = self.conflict(resolve)
        c._sync_with_main()
        self.assertEqual(self.questions(c, "merge"), {})
        self.assertEqual(self.only_blocker("sync")["status"], "fixing")
        self.now += timedelta(seconds=601)
        c._sync_with_main()
        tip = git(c.wt, "rev-parse", "HEAD")
        self.assertEqual(git(c.wt, "show", "-s", "--format=%P", tip).split(), [old, main])
        self.assertTrue(seen)
        self.assertNotEqual(seen[0].resolve(), c.wt.resolve())
        self.assertTrue(c.team.reviewer.prompts)
        approved = ApprovedMerges(self.state).get(tip)
        self.assertEqual((approved["kind"], approved["resolved_by"]), ("main_sync", "troubleshooter"))
        self.assertEqual(self.only_blocker("sync")["status"], "fixed")

    def test_conflict_markers_rejected_even_when_staged_and_budget_exhausts(self):
        def unresolved(prompt, cwd):
            (Path(cwd) / "README.md").write_bytes(b"<<<<<<< layer\nstill broken\n=======\nmain\n>>>>>>> main\n")
            git(cwd, "add", "README.md")
            return '{"kind":"fix","notes":"claimed success"}', 1

        c, old, main = self.conflict(unresolved)
        c._sync_with_main()
        for attempt in range(2):
            self.now += timedelta(seconds=601)
            c._sync_with_main()
            self.assertEqual(git(c.wt, "rev-parse", "HEAD"), old)
            self.assertEqual(git(c.wt, "status", "--porcelain"), "")
            self.assertEqual(self.only_blocker("sync")["attempts"], attempt + 1)
        self.assertEqual(len(c.team.troubleshooter.prompts), 2)
        self.assertEqual(self.only_blocker("sync")["status"], "ready_for_ben")
        self.assertEqual(self.questions(c, "merge"), {})


if __name__ == "__main__":
    unittest.main()
