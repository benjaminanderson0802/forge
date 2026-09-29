"""Contract tests for the bootstrap conductor, driven entirely by local fakes."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.agents import FakeAgent
from core.bootstrap import Conductor, Team
from core.usage import Meter


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def py_test(path):
    return f'python -m unittest {path}'


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.work, self.state = root / "repo", root / "work", root / "state"
        self.repo.mkdir(); self.work.mkdir(); self.state.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Forge Test")
        git(self.repo, "config", "user.email", "ben@example.com")
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        git(self.repo, "add", "."); git(self.repo, "commit", "-m", "initial")
        self.mails, self.gh_calls = [], []
        self.messages = []

        def gh(args):
            self.gh_calls.append(list(args))
            return (0, "https://github.com/o/r/pull/7") if args[:2] == ["pr", "create"] else (0, "")

        self.gh = gh
        self.layer = "layer-1"
        self.c = None

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, script, provider):
        return FakeAgent(script, provider=provider)

    def make_conductor(self, agents=None, limits=None):
        agents = agents or {}
        noop = lambda prompt, cwd: ('{"status":"ok"}', 1)
        members = {
            "test_writer": self.agent(agents.get("test_writer", noop), "codex"),
            "builder": self.agent(agents.get("builder", noop), "claude"),
            "reviewer": self.agent(agents.get("reviewer", lambda p, c: ('{"verdict":"pass","reasons":[]}', 1)), "codex"),
            "troubleshooter": self.agent(agents.get("troubleshooter", lambda p, c: ('{"kind":"suggestion","notes":"try another approach"}', 1)), "claude"),
            "drift_keeper": self.agent(agents.get("drift_keeper", noop), "claude"),
            "planner": self.agent(agents.get("planner", lambda p, c: ('{"tasks":[]}', 1)), "claude"),
        }
        self.team = Team(**members)
        self.c = Conductor(self.repo, self.work, self.state, self.team,
                           limits or {"claude_daily_token_cap": 10**9, "codex_daily_token_cap": 10**9},
                           owner_email="ben@example.com", mailer=lambda s, b: self.mails.append((s, b)),
                           inbox=lambda: self.messages, gh=self.gh, judge_cmds=[], push=False)
        return self.c

    def task(self, **kw):
        d = {"id": "T1", "kind": "build", "title": "Implement feature", "section": "Feature value is 42.",
             "files_in_scope": ["feat.py"], "test_files": ["tests/core/test_feat.py"],
             "test_cmd": py_test("tests/core/test_feat.py")}
        d.update(kw)
        return d

    def init(self, *tasks, agents=None, limits=None):
        c = self.make_conductor(agents, limits)
        c.init_queue(self.layer, list(tasks or [self.task()]))
        return c

    def write_tests(self, prompt, cwd):
        p = cwd / "tests/core/test_feat.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("import unittest\nimport feat\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(feat.VALUE, 42)\n", encoding="utf-8")
        return '{"files":["tests/core/test_feat.py"]}', 1

    def build_feature(self, prompt, cwd):
        (cwd / "feat.py").write_text("VALUE = 42\n", encoding="utf-8")
        return '{"status":"done"}', 1

    def advance_to_build(self, task=None, agents=None):
        c = self.init(task or self.task(), agents=agents)
        self.assertEqual(c.step(), "worked")
        return c

    def branch_files(self):
        return set(git(self.repo, "ls-tree", "-r", "--name-only", self.layer).splitlines())


class BootstrapTests(Harness):
    def test_happy_path_commits_test_then_build_and_ledger_evidence(self):
        """Spec: Stage A acceptance, Stage B judges/review/merge, ledger evidence and run artifacts."""
        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        self.assertEqual(c.step(), "worked")
        q = json.loads((self.state / "queue.json").read_text())
        self.assertEqual(q["tasks"][0]["status"], "tests_ok")
        self.assertEqual(c.step(), "worked")
        self.assertEqual(c.step(), "worked")  # drift keeper follows reviewer pass
        self.assertEqual(json.loads((self.state / "queue.json").read_text())["tasks"][0]["status"], "done")
        self.assertTrue({"feat.py", "tests/core/test_feat.py"} <= self.branch_files())
        contracts = json.loads((self.state / "ledger/contracts.json").read_text())
        self.assertEqual(next(iter(contracts.values()))["status"], "done")
        runs = list((self.state / "runs").iterdir())
        self.assertTrue(runs)
        self.assertTrue(any((r / "prompt.md").exists() and (r / "output.json").exists() for r in runs))
        self.assertFalse((self.state / "KILL").exists())
        self.assertFalse(any("tamper" in s.lower() or "tamper" in b.lower() for s, b in self.mails))

    def test_r14_builder_edit_to_earlier_run_artifact_kills_and_emails(self):
        """R14: fingerprinting covers prior run artifacts under state/runs."""
        earlier_run = []
        def tamper(p, cwd):
            self.assertTrue(earlier_run, "test-writer run must exist before builder starts")
            (earlier_run[0] / "output.json").write_text("forged\n", encoding="utf-8")
            (cwd / "feat.py").write_text("VALUE = 42\n")
            return '{"status":"done"}', 1

        c = self.init(agents={"test_writer": self.write_tests, "builder": tamper})
        self.assertEqual(c.step(), "worked")
        earlier_run.extend((self.state / "runs").iterdir())
        self.assertTrue(earlier_run)
        c.step()
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertTrue((self.state / "KILL").exists())
        self.assertTrue(any("tamper" in s.lower() or "tamper" in b.lower() for s, b in self.mails))
        self.assertNotEqual(task["status"], "done")

    def test_r14_builder_rewrite_of_meter_kills_and_emails(self):
        """R14: fingerprinting covers state/meter.json."""
        def tamper(p, cwd):
            (self.state / "meter.json").write_text('{"claude":0,"codex":0}\n', encoding="utf-8")
            (cwd / "feat.py").write_text("VALUE = 42\n")
            return '{"status":"done"}', 1

        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": tamper})
        c.step()
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertTrue((self.state / "KILL").exists())
        self.assertTrue(any("tamper" in s.lower() or "tamper" in b.lower() for s, b in self.mails))
        self.assertNotEqual(task["status"], "done")

    def test_weak_tests_rejected_twice_then_blocked_and_emailed(self):
        """Spec: tests that already pass are weak; two rejected test attempts block and email."""
        def weak(p, cwd):
            path = cwd / "tests/core/test_feat.py"; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
            return '{"files":["tests/core/test_feat.py"]}', 1
        c = self.init(agents={"test_writer": weak})
        c.step(); c.step()
        t = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertEqual(t["status"], "blocked")
        self.assertTrue(any(n.startswith("tests rejected: weak") for n in t["notes"]))
        self.assertTrue(self.mails)

    def test_test_writer_outside_test_files_is_rejected_and_not_committed(self):
        """Spec: Stage A rejects all changes when the writer touches a file outside test_files."""
        def bad(p, cwd):
            (cwd / "stray.txt").write_text("stray")
            return '{"files":["stray.txt"]}', 1
        c = self.init(agents={"test_writer": bad})
        c.step()
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertTrue(any(n.startswith("tests rejected: wrote outside test_files") for n in task["notes"]))
        self.assertNotIn("stray.txt", self.branch_files())

    def test_builder_test_edit_is_reverted_and_never_merged(self):
        """Spec: builder changes to protected test_files are restored from the layer tip."""
        def edit_test(p, cwd):
            (cwd / "feat.py").write_text("VALUE = 42\n")
            (cwd / "tests/core/test_feat.py").write_text("# tampered\n")
            return '{"status":"done"}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": edit_test})
        c.step(); c.step()
        self.assertNotIn("# tampered", git(self.repo, "show", f"{self.layer}:tests/core/test_feat.py"))

    def test_builder_fake_test_edit_is_reverted_before_judges(self):
        """Spec: Stage B step 3 reverts builder edits to test_files before judges run."""
        def replace_test(p, cwd):
            (cwd / "feat.py").write_text("VALUE = 42\n")
            (cwd / "tests/core/test_feat.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n def test_value(self): self.assertTrue(True)\n"
            )
            return '{"status":"done"}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": replace_test})
        c.step()
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        committed_test = git(self.repo, "show", f"{self.layer}:tests/core/test_feat.py")
        self.assertNotEqual(task["status"], "done")
        self.assertIn("self.assertEqual(feat.VALUE, 42)", committed_test)

    def test_builder_out_of_scope_attempt_fails(self):
        """Spec: any builder edit beyond files_in_scope and test_files fails as out of scope."""
        def bad(p, cwd):
            (cwd / "oops.py").write_text("x=1\n"); return '{"status":"done"}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": bad})
        c.step()
        t = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertIn("out of scope", " ".join(t["notes"]))
        self.assertNotIn("oops.py", self.branch_files())

    def test_review_reasons_are_in_next_builder_prompt(self):
        """Spec: reviewer rejection reasons appear under REVIEW FEEDBACK on the next attempt."""
        def reviewer(p, cwd): return '{"verdict":"fail","reasons":["missing edge case"]}', 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": self.build_feature, "reviewer": reviewer})
        c.step(); c.step(); c.step()
        self.assertIn("REVIEW FEEDBACK:", c.team.builder.prompts[-1])
        self.assertIn("missing edge case", c.team.builder.prompts[-1])

    def test_zero_progress_troubleshooter_notes_and_dead_end_are_prompted(self):
        """Spec: repeated identical judge failures invoke troubleshooting and dead ends are persisted and prompted."""
        def failing_test(p, cwd):
            f = cwd / "tests/core/test_feat.py"; f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail('same judge failure')\n")
            return '{"files":["tests/core/test_feat.py"]}', 1
        def ts(p, cwd): return '{"kind":"dead_end","notes":"approach blocked","alternative":"use a table"}', 1
        c = self.init(agents={"test_writer": failing_test, "builder": lambda p,c: ('{"status":"ok"}',1), "troubleshooter": ts})
        c.step(); c.step(); c.step(); c.step()
        self.assertIn("TROUBLESHOOTER NOTES:", c.team.builder.prompts[-1])
        self.assertIn("approach blocked", c.team.builder.prompts[-1])
        self.assertIn("KNOWN DEAD ENDS:", c.team.builder.prompts[-1])
        self.assertIn('"alternative": "use a table"', (self.state / "dead_ends.jsonl").read_text())

    def test_block_after_troubleshoot_emails_and_queue_continues(self):
        """Spec: two failures after troubleshooting block a task, email a question, then next task runs."""
        # Intentionally deterministic failing accepted test.
        def writer(p, cwd):
            test_file = "tests/core/test_other.py" if "tests/core/test_other.py" in p else "tests/core/test_feat.py"
            f=cwd/test_file; f.parent.mkdir(parents=True,exist_ok=True)
            f.write_text("import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail('no')\n")
            return json.dumps({"files":[test_file]}),1
        def builder(p,cwd): return '{"status":"ok"}',1
        task2=self.task(id="T2", title="Second", files_in_scope=["other.py"], test_files=["tests/core/test_other.py"], test_cmd=py_test("tests/core/test_other.py"))
        c=self.init(self.task(),task2,agents={"test_writer":writer,"builder":builder})
        for _ in range(40):
            c.step()
            tasks=json.loads((self.state/"queue.json").read_text())["tasks"]
            if tasks[0]["status"] == "blocked":
                break
        for _ in range(40):
            tasks=json.loads((self.state/"queue.json").read_text())["tasks"]
            if tasks[1]["status"] == "tests_ok":
                break
            c.step()
        tasks=json.loads((self.state/"queue.json").read_text())["tasks"]
        self.assertEqual(tasks[0]["status"],"blocked")
        self.assertEqual(tasks[1]["status"],"tests_ok")
        self.assertTrue(self.mails)

    def test_builder_blocked_result_is_failed_attempt(self):
        """Spec: builder status blocked becomes a failed attempt and triggers troubleshooting rules."""
        c=self.advance_to_build(agents={"test_writer":self.write_tests,"builder":lambda p,c: (json.dumps({"status":"blocked","summary":"cannot proceed","tried":["route a","route b"],"error":"boom"}),1)})
        c.step()
        task=json.loads((self.state/"queue.json").read_text())["tasks"][0]
        self.assertNotEqual(task["status"],"blocked")
        self.assertIn("blocker: cannot proceed"," ".join(task["notes"]))

    def test_malformed_builder_output_is_failed_attempt_not_exception(self):
        """Spec: malformed required-schema agent output is a failed attempt, not an exception."""
        c=self.advance_to_build(agents={"test_writer":self.write_tests,"builder":lambda p,c: ('not json',1)})
        self.assertEqual(c.step(),"worked")
        self.assertTrue(json.loads((self.state/"queue.json").read_text())["tasks"][0]["fail_signatures"])

    def test_malformed_builder_output_does_not_mark_correct_feature_done(self):
        """Spec: malformed builder output is a failed attempt even when its file change is correct."""
        def malformed(p, cwd):
            (cwd / "feat.py").write_text("VALUE = 42\n")
            return "not json", 1
        c = self.advance_to_build(agents={"test_writer": self.write_tests, "builder": malformed})
        c.step()
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertNotEqual(task["status"], "done")
        self.assertTrue(task["fail_signatures"])

    def test_kill_and_paused_prevent_all_agent_calls(self):
        """Spec: KILL and PAUSED short circuit step before any agent runs."""
        c=self.init(); (self.state/"KILL").write_text("stop")
        self.assertEqual(c.step(),"killed")
        self.assertFalse(any(a.prompts for a in vars(self.team).values()))
        (self.state/"KILL").unlink(); (self.state/"PAUSED").write_text("pause")
        self.assertEqual(c.step(),"paused")
        self.assertFalse(any(a.prompts for a in vars(self.team).values()))

    def test_provider_cap_prevents_agent_calls(self):
        """Spec: any provider at or above its daily token cap halts work before agent calls."""
        c=self.init(limits={"claude_daily_token_cap":10,"codex_daily_token_cap":10**9})
        Meter(self.state).add("claude",10)
        self.assertEqual(c.step(),"capped")
        self.assertFalse(any(a.prompts for a in vars(self.team).values()))

    def test_drift_keeper_replan_pauses_and_emails(self):
        """Spec: drift keeper replan result creates PAUSED and emails a replan question."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature,"drift_keeper":lambda p,c: ('{"status":"replan","reasons":"design drift"}',1)})
        c.step(); c.step(); c.step()
        self.assertTrue((self.state/"PAUSED").exists())
        self.assertTrue(any("replan" in s.lower() for s,b in self.mails))

    def test_plan_task_commits_plan_and_appends_build_tasks(self):
        """Spec: reviewed plan task commits plan_file and appends normalized build tasks."""
        planfile="docs/superpowers/plans/plan.md"
        task={"id":"P1","kind":"plan","title":"Plan feature","section":"Plan it","plan_file":planfile,"status":"todo"}
        def planner(p,cwd):
            f=cwd/planfile; f.parent.mkdir(parents=True,exist_ok=True); f.write_text("plan\n")
            child={"id":"T2","title":"Build it","section":"Build it","files_in_scope":["feat.py"],"test_files":["tests/core/test_feat.py"],"test_cmd":py_test("tests/core/test_feat.py")}
            return json.dumps({"tasks":[child]}),1
        c=self.init(task,agents={"planner":planner})
        c.step()
        data=json.loads((self.state/"queue.json").read_text())
        self.assertEqual(data["tasks"][0]["status"],"done")
        self.assertEqual((data["tasks"][1]["kind"],data["tasks"][1]["status"]),("build","todo"))
        self.assertEqual(data["tasks"][1]["notes"],[])
        self.assertIn(planfile,self.branch_files())

    def test_plan_writer_outside_plan_file_is_rejected(self):
        """Spec: planner changes outside plan_file reject the plan attempt."""
        task={"id":"P1","kind":"plan","title":"Plan","section":"Plan","plan_file":"plan.md","status":"todo"}
        def planner(p,cwd):
            (cwd/"plan.md").write_text("plan"); (cwd/"extra.txt").write_text("extra")
            return '{"tasks":[]}',1
        c=self.init(task,agents={"planner":planner})
        c.step()
        self.assertNotIn("extra.txt",self.branch_files())
        self.assertNotEqual(json.loads((self.state/"queue.json").read_text())["tasks"][0]["status"],"done")

    def test_planner_cannot_write_extra_file_with_valid_task(self):
        """Spec: Plan tasks step 2 permits planner edits only to plan_file."""
        task={"id":"P1","kind":"plan","title":"Plan","section":"Plan","plan_file":"plan.md","status":"todo"}
        def planner(p,cwd):
            (cwd/"plan.md").write_text("plan")
            (cwd/"extra.txt").write_text("extra")
            child={"id":"T2","title":"Build it","section":"Build it","files_in_scope":["feat.py"],"test_files":["tests/core/test_feat.py"],"test_cmd":py_test("tests/core/test_feat.py")}
            return json.dumps({"tasks":[child]}),1
        c=self.init(task,agents={"planner":planner})
        c.step()
        tasks=json.loads((self.state/"queue.json").read_text())["tasks"]
        self.assertNotEqual(tasks[0]["status"],"done")
        self.assertEqual(len(tasks),1)
        self.assertNotIn("extra.txt",self.branch_files())

    def test_gate_opens_pr_and_emails_owner_approval_question(self):
        """Spec: when all queue tasks are done, gate creates a PR and asks owner to reply y."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature})
        c.step(); c.step(); c.step()
        self.assertEqual(c.step(),"gate")
        self.assertTrue(any(a[:2]==["pr","create"] for a in self.gh_calls))
        self.assertTrue(any("[Forge Q-" in s and "reply y" in s.lower() for s,b in self.mails))

    def test_owner_y_reply_approves_and_merges_but_other_sender_does_not(self):
        """Spec: only owner first-word y/yes replies to gate question label and merge the PR."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature})
        c.step(); c.step(); c.step(); c.step()
        subject=next(s for s,b in self.mails if "[Forge Q-" in s)
        before=len(self.gh_calls)
        self.messages[:]=[{"from":"other@example.com","subject":subject,"body":"y"}]
        c.step(); self.assertEqual(len(self.gh_calls),before)
        self.messages[:]=[{"from":"ben@example.com","subject":subject,"body":"Y please"}]
        c.step()
        self.assertTrue(any(a[:2]==["pr","edit"] and "human-approved" in a for a in self.gh_calls))
        self.assertTrue(any(a[:2]==["pr","merge"] for a in self.gh_calls))

    def test_gate_rejects_owner_reply_unless_first_word_is_y_or_yes(self):
        """Spec: Gate approval requires the owner's reply to begin with y or yes."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature})
        c.step(); c.step(); c.step(); c.step()
        subject=next(s for s,b in self.mails if "[Forge Q-" in s)
        self.messages[:]=[{"from":"ben@example.com","subject":subject,"body":"no, not yet"}]
        c.step()
        self.assertFalse(any(a[:2]==["pr","merge"] for a in self.gh_calls))
        self.messages[:]=[{"from":"ben@example.com","subject":subject,"body":"yes"}]
        c.step()
        self.assertTrue(any(a[:2]==["pr","merge"] for a in self.gh_calls))

    def test_stop_email_only_acts_for_owner(self):
        """Spec: STOP in an owner email creates KILL; messages from other senders never act."""
        c=self.init()
        self.messages[:]=[{"from":"other@example.com","subject":"stop","body":"STOP now"}]
        c.step(); self.assertFalse((self.state/"KILL").exists())
        self.messages[:]=[{"from":"ben@example.com","subject":"hello","body":"please STOP now"}]
        self.assertEqual(c.step(),"killed")
        self.assertTrue((self.state/"KILL").exists())

    def test_r1_unsafe_test_commands_rejected_and_planner_noted(self):
        """R1: unsafe shell commands and unlisted unittest paths are rejected."""
        for command in ['python -c "print(1)"', "del x", "python -m unittest tests/core/test_other.py"]:
            with self.subTest(command=command):
                c=self.make_conductor()
                with self.assertRaises(ValueError): c.init_queue(self.layer,[self.task(test_cmd=command)])
        task={"id":"P1","kind":"plan","title":"Plan","section":"Plan","plan_file":"plan.md","status":"todo"}
        def planner(p,cwd):
            (cwd/"plan.md").write_text("plan")
            child={"id":"T2","title":"Build","section":"Build","files_in_scope":["feat.py"],"test_files":["tests/core/test_feat.py"],"test_cmd":"python -c \"x\""}
            return json.dumps({"tasks":[child]}),1
        c=self.init(task,agents={"planner":planner}); c.step()
        self.assertTrue(any("unsafe test_cmd" in n for n in json.loads((self.state/"queue.json").read_text())["tasks"][0]["notes"]))

    def test_r2_reply_requires_qid_and_code_but_stop_does_not(self):
        """R2: qid and code authenticate replies; owner STOP remains code-free."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature})
        c.step(); c.step(); c.step(); c.step()
        qs=json.loads((self.state/"questions.json").read_text()); qid,q=next(iter(qs.items()))
        subject=next(s for s,b in self.mails if "[Forge Q-" in s)
        code=q.get("code","missing")
        no_code=f"Re: [Forge Q-{qid}] ..."
        wrong_code = "Z" * 8
        if wrong_code == code:
            wrong_code = code[:-1] + ("A" if code[-1] != "A" else "B")
        wrong_subject = f"Re: [Forge Q-{qid} {wrong_code}]"
        for bad in [no_code, wrong_subject]:
            before=len(self.gh_calls); self.messages[:]=[{"from":"ben@example.com","subject":bad,"body":"yes"}]; c.step()
            self.assertEqual(len(self.gh_calls),before)
        self.messages[:]=[{"from":"ben@example.com","subject":"STOP","body":""}]; c.step()
        self.assertTrue((self.state/"KILL").exists())
        self.messages[:]=[{"from":"ben@example.com","subject":subject,"body":"yes"}]; c.step()
        self.assertTrue(any(a[:2]==["pr","merge"] for a in self.gh_calls))

    def test_r3_failed_merge_keeps_gate_open_and_emails_error(self):
        """R3: failed gh merge leaves approval open and reports the error."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature})
        c.step(); c.step(); c.step(); c.step()
        qid,q=next(iter(json.loads((self.state/"questions.json").read_text()).items()))
        c.gh=lambda args: (1,"merge failed") if args[:2]==["pr","merge"] else (0,"")
        subj=next(s for s,b in self.mails if "[Forge Q-" in s)
        self.messages[:]=[{"from":"ben@example.com","subject":subj,"body":"yes"}]; c.step()
        self.assertEqual(json.loads((self.state/"questions.json").read_text())[qid]["status"],"open")
        self.assertTrue(any("error" in (s+b).lower() for s,b in self.mails))

    def test_r4_empty_and_timed_out_test_runs_are_rejected(self):
        """R4: acceptance needs a real failing unittest run that completes."""
        rejected=[]
        for source in ["import unittest\n", "import time; time.sleep(5)\n"]:
            def writer(p,cwd,source=source):
                f=cwd/"tests/core/test_feat.py"; f.parent.mkdir(parents=True,exist_ok=True); f.write_text(source)
                return '{"files":["tests/core/test_feat.py"]}',1
            c=self.init(agents={"test_writer":writer}, limits={"test_timeout_s":1,"claude_daily_token_cap":10**9,"codex_daily_token_cap":10**9}); c.step()
            t=json.loads((self.state/"queue.json").read_text())["tasks"][0]
            rejected.append(any(n.startswith("tests rejected: no real failing run") for n in t["notes"]))
        self.assertEqual(rejected,[True,True])

    def test_r5_troubleshooter_three_rounds(self):
        """R5: repeated failures allow three troubleshoot rounds."""
        c=self.advance_to_build(agents={"test_writer":self.write_tests,"builder":lambda p,c: ('{"status":"ok"}',1)})
        for _ in range(8): c.step()
        self.assertEqual(len(c.team.troubleshooter.prompts),3)

    def test_r5_blocker_easy_out(self):
        """R5: unsupported blocker is an easy out."""
        c=self.advance_to_build(agents={"test_writer":self.write_tests,"builder":lambda p,c: ('{"status":"blocked","summary":"x"}',1)})
        c.step(); t=json.loads((self.state/"queue.json").read_text())["tasks"][0]
        self.assertIn("blocker rejected: no evidence (easy out)"," ".join(t["notes"]))
        self.assertTrue((self.state/"easy_outs.jsonl").exists())

    def test_r6_malformed_drift_retries_then_pauses(self):
        """R6: unusable drift results keep the gate closed and pause after three tries."""
        c=self.init(agents={"test_writer":self.write_tests,"builder":self.build_feature,"drift_keeper":lambda p,c: ("not json",1)})
        c.step(); c.step()
        for _ in range(3): c.step()
        self.assertTrue((self.state/"PAUSED").exists())
        self.assertTrue(any("[Forge Q-" in s for s,b in self.mails))

    def test_r7_mail_and_inbox_failures_are_recovered(self):
        """R7: failed sends are retried and inbox exceptions are logged without escaping."""
        c=self.init(); calls=[0]
        def flaky(s,b):
            calls[0]+=1
            if calls[0]==1: raise RuntimeError("mail down")
            self.mails.append((s,b))
        c.mailer=flaky
        try: c._ask("blocked","subject","body",task="T1")
        except RuntimeError: pass
        initial=json.loads((self.state/"questions.json").read_text())
        initially_undelivered=next(iter(initial.values())).get("delivered") is False
        c.step()
        delivered_on_retry=next(iter(json.loads((self.state/"questions.json").read_text()).values())).get("delivered",False)
        c.inbox=lambda: (_ for _ in ()).throw(RuntimeError("inbox down"))
        self.assertIsInstance(c.step(),str)
        self.assertTrue(initially_undelivered)
        self.assertTrue(delivered_on_retry)
        self.assertTrue((self.state/"errors.log").exists())

    def test_r8_git_status_error_fails_attempt_without_escaping_step(self):
        """R8: git status failure is an attempt failure and never escapes step."""
        def writer(p,cwd):
            f=cwd/"tests/core/test_feat.py"; f.parent.mkdir(parents=True,exist_ok=True)
            f.write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.fail()\n")
            return '{"files":["tests/core/test_feat.py"]}',1
        c=self.init(agents={"test_writer":writer})
        real_run=subprocess.run
        def broken_status(args,*a,**kw):
            if args[:2]==["git","status"]: return subprocess.CompletedProcess(args,1,b"",b"broken git")
            return real_run(args,*a,**kw)
        try:
            with patch("core.bootstrap.subprocess.run",side_effect=broken_status): result=c.step()
        except Exception as exc: self.fail(f"step leaked git error: {exc}")
        self.assertEqual(result,"error")
        self.assertIn("git error"," ".join(json.loads((self.state/"queue.json").read_text())["tasks"][0]["notes"]))

    def test_r9_state_write_by_builder_kills_and_emails_tamper_question(self):
        """R9: builder state tampering triggers KILL and a tamper question before completion."""
        def tamper(p,cwd):
            (self.state/"forged.json").write_text("bad")
            (cwd/"feat.py").write_text("VALUE = 42\n")
            return '{"status":"done"}',1
        c=self.advance_to_build(agents={"test_writer":self.write_tests,"builder":tamper}); c.step()
        self.assertTrue((self.state/"KILL").exists())
        self.assertTrue(any("tamper" in s.lower() for s,b in self.mails))
        self.assertNotEqual(json.loads((self.state/"queue.json").read_text())["tasks"][0]["status"],"done")

    def test_r10_new_state_file_by_builder_kills_and_emails_tamper_question(self):
        """R10: creating a new state file during a builder run is detected as tampering."""
        def tamper(p,cwd):
            (self.state/"sneaky.tmp").write_text("unexpected")
            (cwd/"feat.py").write_text("VALUE = 42\n")
            return '{"status":"done"}',1
        c=self.advance_to_build(agents={"test_writer":self.write_tests,"builder":tamper})
        c.step()
        self.assertTrue((self.state/"KILL").exists())
        self.assertTrue(any("tamper" in s.lower() for s,b in self.mails))

    def test_r11_lock_is_exclusive_across_processes_and_reusable(self):
        """R11: only one process can hold the state lock, and it can be reacquired after close."""
        from core.bootstrap import acquire_lock
        lock_dir=self.state/"lock-test"
        first=acquire_lock(lock_dir)
        self.assertIsNotNone(first)
        code=("import sys; from pathlib import Path; from core.bootstrap import acquire_lock; "
              "h=acquire_lock(Path(sys.argv[1])); print('locked' if h is not None else 'busy'); "
              "h and h.close()")
        try:
            result=subprocess.run([sys.executable,"-c",code,str(lock_dir)],text=True,capture_output=True,check=True)
            self.assertEqual(result.stdout.strip(),"busy")
        finally:
            first.close()
        second=acquire_lock(lock_dir)
        self.assertIsNotNone(second)
        second.close()

    def test_r12_missing_heartbeat_directory_does_not_escape_run(self):
        """R12: an unwritable heartbeat path is handled by the run loop."""
        c=self.init()
        heartbeat=self.state/"missing"/"heartbeat"
        self.assertIsInstance(c.run(max_steps=3,idle_sleep_s=0,heartbeat=heartbeat,sleep=lambda s:None),str)

    def test_r13_stage_errors_back_off_and_notify_once_after_three(self):
        """R13: repeated stage errors are contained, back off, and send one alert after three."""
        def writer(p,cwd):
            f=cwd/"tests/core/test_feat.py"; f.parent.mkdir(parents=True,exist_ok=True)
            f.write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.fail()\n")
            return '{"files":["tests/core/test_feat.py"]}',1
        c=self.init(agents={"test_writer":writer})
        real_run=subprocess.run
        def broken_status(args,*a,**kw):
            if args[:2]==["git","status"]: return subprocess.CompletedProcess(args,1,b"",b"broken git")
            return real_run(args,*a,**kw)
        durations=[]
        try:
            with patch("core.bootstrap.subprocess.run",side_effect=broken_status):
                c.run(max_steps=4,idle_sleep_s=0,sleep=durations.append)
        except Exception as exc:
            self.fail(f"run leaked stage error: {exc}")
        self.assertEqual(len(durations),4)
        # With a zero idle interval the backoff durations are all zero; errors
        # still reach the third-consecutive-error notification threshold.
        self.assertEqual(durations,[0,0,0,0])
        alerts=[(s,b) for s,b in self.mails if "keeps hitting an error" in s.lower() or "keeps hitting an error" in b.lower()]
        self.assertEqual(len(alerts),1)


if __name__ == "__main__":
    unittest.main()


class R15BootstrapTests(Harness):
    def test_R15_nonempty_lock_baseline_prevents_agent_launch(self):
        """R15: a non-empty lock baseline fails the stage before launching the agent."""
        calls = []

        def writer(prompt, cwd):
            calls.append((prompt, cwd))
            return self.write_tests(prompt, cwd)

        c = self.init(agents={"test_writer": writer})
        status_before = json.loads((self.state / "queue.json").read_text())["tasks"][0]["status"]
        (self.state / "conductor.lock").write_bytes(b"x")
        result = c.step()
        self.assertFalse(calls, "a non-empty lock baseline must prevent agent launch")
        self.assertEqual(result, "error")
        self.assertIn("conductor.lock", (self.state / "errors.log").read_text(encoding="utf-8"))
        self.assertFalse((self.state / "KILL").exists())
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertEqual(task["status"], status_before)

    def test_R15_cli_step_respects_lock_and_closes_acquired_handle(self):
        """R15: CLI step reports busy or holds an acquired lock and closes it afterwards."""
        from core.bootstrap import acquire_lock, main

        self.make_conductor()
        real_init = Conductor.__init__

        def redirected_init(conductor, repo, work, state, *args, **kwargs):
            real_init(conductor, self.repo, self.work, self.state, *args, **kwargs)

        for acquired in (False, True):
            with self.subTest(acquired=acquired):
                handle = acquire_lock(self.state)
                self.assertIsNotNone(handle)
                lock_states = []
                stdout = io.StringIO()

                def lock(state):
                    lock_states.append(state)
                    return handle if acquired else None

                try:
                    with (
                        patch("core.agents.load_limits", return_value=self.c.limits),
                        patch("core.bootstrap.real_team", return_value=self.team),
                        patch("core.bootstrap.gmail_mailer", return_value=self.c.mailer),
                        patch("core.bootstrap.gmail_inbox", return_value=self.c.inbox),
                        patch("core.bootstrap.gh_cli", return_value=self.gh),
                        patch.object(Conductor, "__init__", new=redirected_init),
                        patch("core.bootstrap.acquire_lock", new=lock),
                        patch.object(Conductor, "step", return_value="worked") as step,
                        patch("sys.stdout", stdout),
                    ):
                        result = main(["step"])
                    self.assertEqual(result, 0)
                    if acquired:
                        step.assert_called_once_with()
                        self.assertTrue(handle.closed, "CLI step must close its acquired lock")
                    else:
                        step.assert_not_called()
                        self.assertIn("busy", stdout.getvalue())
                    self.assertEqual(len(lock_states), 1, "CLI step must acquire the conductor lock")
                finally:
                    handle.close()

    def test_R15_held_conductor_lock_allows_full_test_writer_step(self):
        """R15: the conductor's own OS lock must not break a test-writer step."""
        from core.bootstrap import acquire_lock

        c = self.init(agents={"test_writer": self.write_tests, "builder": self.build_feature})
        # Lock a real byte so Windows must enforce the read restriction.
        (self.state / "conductor.lock").write_bytes(b"0")
        handle = acquire_lock(self.state)
        self.assertIsNotNone(handle)
        try:
            result = c.step()
            errors = self.state / "errors.log"
            log = errors.read_text(encoding="utf-8") if errors.exists() else ""
            self.assertNotIn("stage error", log.lower())
            self.assertNotIn("PermissionError", log)
            self.assertEqual(result, "worked")
            task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
            self.assertEqual(task["status"], "tests_ok")
            self.assertIn("tests/core/test_feat.py", self.branch_files())
            self.assertFalse((self.state / "KILL").exists())
        finally:
            handle.close()

    def test_R15_agent_append_to_existing_conductor_lock_is_tamper(self):
        """R15: the lock file remains fingerprinted and agent appends trigger tamper."""
        from core.bootstrap import acquire_lock

        lock_path = self.state / "conductor.lock"

        def tamper(prompt, cwd):
            with lock_path.open("ab") as stream:
                stream.write(b"agent changed the lock\n")
            return self.write_tests(prompt, cwd)

        c = self.init(agents={"test_writer": tamper})
        handle = acquire_lock(self.state)
        self.assertIsNotNone(handle)
        handle.close()  # Permit the fake agent to append on Windows as well.
        self.assertTrue(lock_path.exists())
        status_before = json.loads((self.state / "queue.json").read_text())["tasks"][0]["status"]
        result = c.step()
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual(result, "killed")
        self.assertTrue(any("tamper" in subject.lower() and "conductor.lock" in body
                            for subject, body in self.mails))
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertEqual(task["status"], status_before)
        self.assertNotIn("tests/core/test_feat.py", self.branch_files())
        self.assertNotIn("stage error", (self.state / "errors.log").read_text().lower())

    def test_R15_non_lock_file_becoming_unreadable_is_tamper(self):
        """R15: an unreadable non-lock file triggers tamper rather than a stage error."""
        meter_path = self.state / "meter.json"
        real_read_bytes = Path.read_bytes
        agent_started = False
        denied_reads = []

        def read_bytes(path):
            if agent_started and path == meter_path:
                denied_reads.append(path)
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_bytes(path)

        def writer(prompt, cwd):
            nonlocal agent_started
            agent_started = True
            return self.write_tests(prompt, cwd)

        c = self.init(agents={"test_writer": writer})
        Meter(self.state).add("codex", 1)
        self.assertTrue(meter_path.read_bytes())
        status_before = json.loads((self.state / "queue.json").read_text())["tasks"][0]["status"]
        # Keep the denial active for the fingerprint taken after the agent returns.
        with patch.object(Path, "read_bytes", new=read_bytes):
            result = c.step()
        self.assertTrue(agent_started)
        self.assertTrue(denied_reads)
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual(result, "killed")
        self.assertTrue(any("tamper" in subject.lower() and "meter.json" in body
                            for subject, body in self.mails))
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertEqual(task["status"], status_before)
        self.assertNotIn("stage error", (self.state / "errors.log").read_text().lower())

    def test_R15_lock_mtime_change_alone_changes_fingerprint(self):
        """R15: the lock signature covers mtime even when its size and bytes are unchanged."""
        from core.bootstrap import acquire_lock

        c = self.make_conductor()
        handle = acquire_lock(self.state)
        self.assertIsNotNone(handle)
        handle.close()
        lock_path = self.state / "conductor.lock"
        before = c._fingerprint()
        self.assertIn("conductor.lock", before)
        stat = lock_path.stat()
        os.utime(lock_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        self.assertEqual(lock_path.stat().st_size, stat.st_size)
        self.assertNotEqual(lock_path.stat().st_mtime_ns, stat.st_mtime_ns)
        after = c._fingerprint()
        self.assertIn("conductor.lock", after)
        self.assertNotEqual(before["conductor.lock"], after["conductor.lock"])

    def test_R15_acquire_lock_truncates_existing_bytes_while_held(self):
        """R15: acquiring the lock empties existing bytes while the handle is held."""
        from core.bootstrap import acquire_lock

        lock_path = self.state / "conductor.lock"
        lock_path.write_bytes(b"junk")
        handle = acquire_lock(self.state)
        self.assertIsNotNone(handle)
        try:
            self.assertEqual(lock_path.stat().st_size, 0)
        finally:
            handle.close()

    def test_R15_unreadable_baseline_prevents_agent_launch(self):
        """R15: an unreadable baseline fails the stage before launching the agent."""
        meter_path = self.state / "meter.json"
        real_read_bytes = Path.read_bytes
        calls = []
        denied_reads = []

        def read_bytes(path):
            if path == meter_path:
                denied_reads.append(path)
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_bytes(path)

        def writer(prompt, cwd):
            calls.append((prompt, cwd))
            return self.write_tests(prompt, cwd)

        c = self.init(agents={"test_writer": writer})
        Meter(self.state).add("codex", 1)
        status_before = json.loads((self.state / "queue.json").read_text())["tasks"][0]["status"]
        # Deny reads throughout the step, including both fingerprints if launched.
        with patch.object(Path, "read_bytes", new=read_bytes):
            result = c.step()
        self.assertTrue(denied_reads)
        self.assertFalse(calls, "an unreadable baseline must prevent agent launch")
        self.assertEqual(result, "error")
        self.assertFalse((self.state / "KILL").exists())
        task = json.loads((self.state / "queue.json").read_text())["tasks"][0]
        self.assertEqual(task["status"], status_before)
        self.assertIn("meter.json", (self.state / "errors.log").read_text(encoding="utf-8"))

    def test_R15_lock_replacement_with_restored_size_and_mtime_is_tamper(self):
        """R15: replacing an empty lock is tamper even when its timestamps are restored."""
        from core.bootstrap import acquire_lock

        lock_path = self.state / "conductor.lock"
        replacement_stats = []

        def tamper(prompt, cwd):
            lock_path.unlink()
            lock_path.write_bytes(b"")
            os.utime(lock_path, ns=(original.st_atime_ns, original.st_mtime_ns))
            replacement_stats.append(lock_path.stat())
            return self.write_tests(prompt, cwd)

        c = self.init(agents={"test_writer": tamper})
        handle = acquire_lock(self.state)
        self.assertIsNotNone(handle)
        handle.close()  # Allow deletion and recreation on Windows.
        original = lock_path.stat()
        self.assertEqual(original.st_size, 0)
        result = c.step()
        self.assertEqual(len(replacement_stats), 1)
        replacement = replacement_stats[0]
        self.assertEqual(replacement.st_size, original.st_size)
        self.assertEqual(replacement.st_atime_ns, original.st_atime_ns)
        self.assertEqual(replacement.st_mtime_ns, original.st_mtime_ns)
        if replacement.st_ino == original.st_ino:
            self.skipTest("filesystem reused the inode; the empty replacement has an identical fingerprint")
        self.assertTrue((self.state / "KILL").exists())
        self.assertEqual(result, "killed")
        self.assertTrue(any("tamper" in subject.lower() and "conductor.lock" in body
                            for subject, body in self.mails))
