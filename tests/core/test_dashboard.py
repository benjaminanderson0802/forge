"""R64 contract tests: read-only live dashboard, with no real Forge state."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ledger import _hash

NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[2]
DEFAULTS = dict(planner=900, test_writer=600, builder=900, judge=900,
                reviewer=300, merge=600)
BUILD = 3300


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ForgeFixture:
    """File fixtures only; deliberately never instantiate a writing Ledger/Meter."""
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state/bootstrap"
        self.limits = dict(claude_daily_token_cap=1000, codex_daily_token_cap=2000,
                           agent_runs_per_day=50, agent_timeout_s=600)
        self.write("charter/limits.json", self.limits)
        self.phases = [dict(name="0. Core", done=True), dict(name="1. Loop", done=False),
                       dict(name="2. Audit", done=False)]
        self.write("docs/progress.json", {"phases": self.phases})
        self.raw("docs/specs/layer-1-design.md",
                 "# Design\n\n## 1. Behaviour\n- First requirement\n- Second requirement\n"
                 "- Third requirement\n\n## 2. Fourth requirement\n")
        self.queue([])
        self.write("state/bootstrap/questions.json", {})
        self.write("state/bootstrap/meter.json", {"2026-10-01": {"claude": 400, "codex": 50}})
        self.heartbeat()
        self.events([])
        self.serial = 0

    def raw(self, path, text):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def write(self, path, value):
        return self.raw(path, json.dumps(value))

    def lane_path(self, lane="main"):
        return "state/bootstrap" if lane == "main" else "state/lanes/" + lane

    def queue(self, tasks, lane="main", **extra):
        self.write(self.lane_path(lane) + "/queue.json",
                   dict(layer="layer-1", tasks=tasks, **extra))

    def heartbeat(self, age=10, phase="step", lane="main", last_status=None):
        path = "state/service/" + ("" if lane == "main" else lane + "/") + "heartbeat.json"
        self.write(path, dict(at=NOW.timestamp() - age, phase=phase, last_status=last_status))

    def second_lane(self, layer="layer-1"):
        self.write("state/lanes.json", ["p2"])
        self.write("state/lanes/p2/queue.json", dict(layer=layer, tasks=[]))
        self.heartbeat(lane="p2")

    def task(self, status="todo", id="T1", kind="build", covers=None):
        return dict(id=id, title="Title " + id, kind=kind, status=status, covers=covers or [])

    def add_run(self, role, ago, duration=None, lane="main", task="T1", title="A task",
            tokens=0, provider="claude", data=None):
        self.serial += 1
        start = NOW - timedelta(seconds=ago)
        rid = start.strftime("%Y%m%dT%H%M%S") + f"-{role}-{self.serial:08x}"
        folder = self.lane_path(lane) + "/runs/" + rid
        self.raw(folder + "/prompt.md", "Instructions\n" +
                 (f"TASK {task}: {title}\n" if task is not None else "Probe readiness\n"))
        if duration is not None:
            p = self.write(folder + "/output.json", dict(ok=True, error=None, text="",
                           data=data, tokens=tokens, provider=provider))
            end = (start + timedelta(seconds=duration)).timestamp()
            os.utime(p, (end, end))
        return rid

    def events(self, ids, lane="main", torn=False, broken=False):
        prev, lines = "genesis", []
        for i, cid in enumerate(ids):
            body = dict(proposal_id=f"pass-{i}", action="pass", contract_id=cid,
                        identity="ci", role="ci", payload={}, result_status="done", prev=prev)
            event = dict(body, hash=_hash(body))
            prev = event["hash"]
            lines.append(event)
        if broken and lines:
            lines[0]["payload"] = {"tampered": True}
        text = "".join(json.dumps(e) + "\n" for e in lines)
        return self.raw(self.lane_path(lane) + "/ledger/events.jsonl",
                        text + ('{"action":' if torn else ""))

    def snap(self):
        return importlib.import_module("core.dashboard").snapshot(self.root, NOW)

    def main_lane(self):
        return self.snap()["lanes"][0]

    def listing(self):
        return {p.relative_to(self.root).as_posix():
                (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns)
                for p in self.root.rglob("*")}


class DashboardTests(ForgeFixture, unittest.TestCase):
    def test_snapshot_schema_and_lane_order(self):
        self.second_lane("layer-2")
        self.write("state/lanes.json", ["p2", "main", "p2"])
        self.write("state/bootstrap/questions.json", {
            "gate-1": dict(kind="gate", status="open", code="gatecode", subject="Ready"),
            "old-2": dict(kind="blocked", status="answered", code="oldcode", subject="Old")})
        s = self.snap()
        self.assertLessEqual({"generated_at", "lanes", "project", "tokens", "timeline", "stage_medians",
                              "attempts"}, s.keys())
        self.assertEqual(instant(s["generated_at"]), NOW)
        self.assertEqual([l["name"] for l in s["lanes"]], ["main", "p2"])
        self.assertEqual([l["layer"] for l in s["lanes"]], ["layer-1", "layer-2"])
        self.assertEqual(s["lanes"][0]["open_questions"], 1)
        self.assertTrue(s["lanes"][1]["conductor"]["alive"])

    def test_service_heartbeat_boundary_and_exited(self):
        for age, phase, alive in [(180, "step", True), (181, "step", False), (0, "exited", False)]:
            with self.subTest(age=age, phase=phase):
                self.heartbeat(age, phase)
                self.assertEqual(self.main_lane()["conductor"], dict(alive=alive, heartbeat_age_s=age))

    def test_fallback_heartbeat_boundary(self):
        (self.root / "state/service/heartbeat.json").unlink()
        for age, alive in [(900, True), (901, False)]:
            with self.subTest(age=age):
                self.raw("state/bootstrap/conductor.heartbeat", f"123 {NOW.timestamp() - age}")
                self.assertEqual(self.main_lane()["conductor"], dict(alive=alive, heartbeat_age_s=age))

    def test_service_heartbeat_takes_precedence_over_fallback(self):
        self.raw("state/bootstrap/conductor.heartbeat", f"123 {NOW.timestamp()}")
        self.heartbeat(181)
        self.assertFalse(self.main_lane()["conductor"]["alive"])

    def test_no_heartbeat_is_stopped(self):
        (self.root / "state/service/heartbeat.json").unlink()
        lane = self.main_lane()
        self.assertEqual(lane["conductor"], dict(alive=False, heartbeat_age_s=None))
        self.assertEqual(lane["state"], "stopped")

    def test_stopped_beats_pause_running_and_caps(self):
        self.add_run("planner", 30)
        self.raw("state/bootstrap/PAUSED", "pause")
        self.heartbeat(last_status="capped")
        for flag in ["state/bootstrap/KILL", "state/shared/KILL", None]:
            with self.subTest(flag=flag):
                p = self.raw(flag, "stop") if flag else None
                if not flag:
                    self.heartbeat(181)
                self.assertEqual(self.main_lane()["state"], "stopped")
                if p:
                    p.unlink()

    def test_paused_beats_running_and_running_beats_capped(self):
        self.add_run("planner", 30)
        self.heartbeat(last_status="capped")
        p = self.raw("state/bootstrap/PAUSED", "pause")
        self.assertEqual(self.main_lane()["state"], "paused")
        p.unlink()
        self.assertEqual(self.main_lane()["state"], "running")

    def test_idle_and_service_capped(self):
        self.assertEqual(self.main_lane()["state"], "idle")
        self.heartbeat(last_status="capped")
        self.assertEqual(self.main_lane()["state"], "capped")

    def test_caps_require_every_provider_at_cap_or_held(self):
        self.write("state/bootstrap/meter.json", {"2026-10-01": dict(claude=1000, codex=0)})
        self.assertEqual(self.main_lane()["state"], "idle")
        self.write("state/bootstrap/holds.json", {"codex": (NOW + timedelta(hours=1)).isoformat()})
        self.assertEqual(self.main_lane()["state"], "capped")
        self.write("state/bootstrap/holds.json", {})
        self.write("state/bootstrap/meter.json", {"2026-10-01": dict(claude=1000, codex=2000)})
        self.assertEqual(self.main_lane()["state"], "capped")

    def test_current_is_newest_unfinished_run_with_task_and_elapsed(self):
        self.add_run("planner", 500)
        self.add_run("reviewer", 300, 10)
        rid = self.add_run("test_writer", 120, task="T7", title="Title: with colon")
        cur = self.main_lane()["current"]
        self.assertEqual({k: cur[k] for k in ("role", "task_id", "task_title", "run_id", "elapsed_s")},
                         dict(role="test_writer", task_id="T7", task_title="Title: with colon",
                              run_id=rid, elapsed_s=120))
        self.assertEqual(instant(cur["started"]), NOW - timedelta(seconds=120))

    def test_older_unfinished_run_is_abandoned_after_newer_finished_run(self):
        abandoned = self.add_run("test_writer", 500, task="old")
        self.add_run("reviewer", 300, 100, task="new")
        self.assertIsNone(self.main_lane()["current"])
        rid = self.add_run("builder", 120, 100, task="new")
        cur = self.main_lane()["current"]
        self.assertEqual((cur["role"], cur["task_id"], cur["run_id"], cur["elapsed_s"]),
                         ("judge", "new", rid, 20))
        rows = self.snap()["timeline"]["lanes"]["main"]
        self.assertFalse(next(r for r in rows if r["run_id"] == abandoned)["running"])

    def test_hyphenated_probe_role_has_no_task(self):
        self.add_run("probe-claude", 30, task=None)
        cur = self.main_lane()["current"]
        self.assertEqual(cur["role"], "probe-claude")
        self.assertIsNone(cur["task_id"])

    def test_dead_conductor_abandons_unfinished_run(self):
        self.add_run("builder", 60)
        self.heartbeat(181)
        self.assertIsNone(self.main_lane()["current"])

    def test_judging_starts_at_finished_builder_end_only_while_alive(self):
        self.add_run("builder", 300, 200, task="T9", title="Built task")
        cur = self.main_lane()["current"]
        self.assertEqual((cur["role"], cur["task_id"], cur["task_title"], cur["elapsed_s"]),
                         ("judge", "T9", "Built task", 100))
        self.assertEqual(instant(cur["started"]), NOW - timedelta(seconds=100))
        self.assertEqual(self.main_lane()["state"], "running")
        self.heartbeat(181)
        self.assertIsNone(self.main_lane()["current"])

    def test_default_stage_medians(self):
        self.assertEqual(self.snap()["stage_medians"],
                         {k: dict(median_s=v, n=0, source="default") for k, v in DEFAULTS.items()})

    def test_history_medians_pool_lanes_and_ignore_nonpositive_durations(self):
        self.second_lane()
        for role in ("planner", "test_writer", "builder", "reviewer"):
            self.add_run(role, 5000, 100)
            self.add_run(role, 4000, 300, lane="p2")
            self.add_run(role, 3000, 0)
            self.add_run(role, 2000, -10)
        self.add_run("merge", 1000, 20)
        med = self.snap()["stage_medians"]
        for role in ("planner", "test_writer", "builder", "reviewer"):
            self.assertEqual(med[role], dict(median_s=200, n=2, source="history"))
        self.assertEqual(med["merge"], dict(median_s=600, n=0, source="default"))

    def test_judge_gaps_use_next_run_of_any_role_in_same_lane_under_six_hours(self):
        self.second_lane()
        self.add_run("builder", 70000, 100)
        self.add_run("probe-claude", 48300, 10)  # exactly 6 h: excluded
        self.add_run("builder", 40000, 100)
        self.add_run("reviewer", 18000, 100)  # 21900 s: excluded
        self.add_run("builder", 15000, 100)
        self.add_run("planner", 14700, 100)  # next run gives a 200 s sample
        self.add_run("reviewer", 14500, 100)
        self.add_run("builder", 10000, 100)
        self.add_run("reviewer", 9800, 100, lane="p2")  # cannot pair across lanes
        self.add_run("reviewer", 9500, 100)  # 400 s
        self.add_run("builder", 5000, 100, lane="p2")
        self.add_run("reviewer", 4100, 100, lane="p2")  # 800 s
        self.add_run("builder", 3000, 100)
        self.add_run("builder", 2300, 100)  # retry: 600 s sample
        self.add_run("probe-claude", 2200, 10)  # zero gap: excluded
        self.add_run("builder", 1000, 100)
        self.add_run("probe-codex", 950, 10)  # negative gap: excluded
        self.assertEqual(self.snap()["stage_medians"]["judge"], dict(median_s=500, n=4, source="history"))

    def test_attempts_default_without_builder_task_history(self):
        self.add_run("reviewer", 1000, 100)
        self.add_run("builder", 500, 100, task=None)
        self.assertEqual(self.snap()["attempts"], dict(median=1.0, n=0, source="default"))

    def test_attempts_history_is_median_builder_runs_per_task_across_lanes(self):
        self.second_lane()
        # Four task samples: 1, 2, 5, 8 runs, so median 3.5 (not their mean).
        for task, count, lane in [("A", 1, "main"), ("B", 2, "main"),
                                  ("C", 5, "p2"), ("D", 8, "p2")]:
            for i in range(count):
                self.add_run("builder", 10000 - self.serial * 200, 100, lane=lane, task=task)
        self.add_run("reviewer", 100, 50, task="unrelated")
        self.add_run("builder", 30, 10, task=None)
        self.assertEqual(self.snap()["attempts"], dict(median=3.5, n=4, source="history"))

    def attempt_history(self):
        # Five historical tasks keep the median at four even with a target task.
        for i in range(5):
            self.finished_attempts(f"history-{i}", 4)

    def finished_attempts(self, task, count):
        for _ in range(count):
            ago = 100000 - self.serial * 500
            self.add_run("builder", ago, 100, task=task)
            self.add_run("probe-claude", ago - 300, 10, task=None)
        # Every builder sample is 100 s and every judge sample is 200 s.

    def check_attempt_eta(self, status, role, base_eta):
        self.attempt_history()
        for done, extra in [(2, 1 if role != "judge" else 2), (5, 0)]:
            with self.subTest(finished_builders=done):
                task = f"target-{done}"
                self.queue([self.task(status, task)])
                self.finished_attempts(task, done - (role == "judge"))
                if role == "judge":
                    self.add_run("builder", 140, 100, task=task)
                elif role:
                    self.add_run(role, 40, task=task)
                else:
                    self.add_run("probe-claude", 20, 10, task=None)
                row = self.main_lane()["tasks"][0]
                self.assertEqual(row["stage"], role or ("test_writer" if status == "todo" else "builder"))
                self.assertEqual(self.snap()["attempts"]["median"], 4)
                self.assertEqual(row["eta_s"], base_eta + extra * (100 + 200))
                if extra:
                    self.assertIn("attempt", row["eta_basis"].lower())
                    self.assertRegex(row["eta_basis"].lower(), r"median\s+4\b")

    def test_todo_eta_adds_only_remaining_attempts(self):
        self.check_attempt_eta("todo", None, 1800)

    def test_tests_ok_eta_adds_only_remaining_attempts(self):
        self.check_attempt_eta("tests_ok", None, 1200)

    def test_running_builder_eta_adds_only_remaining_attempts(self):
        self.check_attempt_eta("tests_ok", "builder", 1160)

    def test_judging_eta_adds_only_remaining_attempts(self):
        self.check_attempt_eta("tests_ok", "judge", 1060)

    def test_checkpoint_unplanned_work_uses_historical_attempts(self):
        self.attempt_history()
        self.queue([self.task(kind="plan")])
        cp = self.main_lane()["checkpoint"]
        self.assertEqual(cp["eta_s"], 1200 + 6 * (600 + 4 * (100 + 200) + 300 + 600))
        self.assertIn("attempt", cp["eta_basis"].lower())
        self.assertIn("4", cp["eta_basis"])

    def test_task_stages_and_default_etas_by_status(self):
        cases = [("plan", "todo", "planner", 1200), ("build", "todo", "test_writer", BUILD),
                 ("build", "tests_ok", "builder", 2700), ("build", "merge_pending", "merge", 600),
                 ("build", "done", None, 0), ("build", "blocked", None, None)]
        tasks = [self.task(status, str(i), kind) for i, (kind, status, _, _) in enumerate(cases)]
        self.queue(tasks + [self.task("superseded", "old")])
        got = self.main_lane()["tasks"]
        self.assertEqual([t["id"] for t in got], [t["id"] for t in tasks])
        for t, original, (_, _, stage, eta) in zip(got, tasks, cases):
            self.assertEqual((t["title"], t["kind"], t["status"]),
                             (original["title"], original["kind"], original["status"]))
            self.assertEqual((t["stage"], t["eta_s"]), (stage, eta))
            if eta:
                self.assertIn("estimate", t["eta_basis"].lower())

    def test_running_stage_overrides_status_and_subtracts_elapsed_with_floor(self):
        self.queue([self.task(), self.task(id="T2")])
        self.add_run("builder", 200)
        tasks = self.main_lane()["tasks"]
        self.assertEqual((tasks[0]["stage"], tasks[0]["eta_s"]), ("builder", 2500))
        self.assertEqual(tasks[1]["eta_s"], BUILD)
        # Keep the conductor alive for the later observation.
        self.heartbeat(age=-1000)
        s = importlib.import_module("core.dashboard").snapshot(self.root, NOW + timedelta(seconds=1000))
        self.assertEqual(s["lanes"][0]["tasks"][0]["eta_s"], 1800)

    def coverage_fixture(self):
        self.queue([self.task("done", "T1", covers=["1.1", "1.2"]),
                    self.task("todo", "T2", covers=["1.2"]),
                    self.task("done", "T3", covers=["1.3"]),
                    self.task("superseded", "old", covers=["1.1"])])
        self.events(["T1"])

    def test_checkpoint_counts_and_event_backed_requirement_statuses(self):
        self.coverage_fixture()
        cp = self.main_lane()["checkpoint"]
        self.assertEqual((cp["layer"], cp["tasks_done"], cp["tasks_total"]), ("layer-1", 2, 3))
        self.assertTrue(cp["spec_file"].replace("\\", "/").endswith("docs/specs/layer-1-design.md"))
        self.assertEqual((cp["covered"], cp["partial"], cp["requirements_total"]), (1, 1, 4))
        self.assertEqual([(r["id"], r["status"]) for r in cp["requirements"]],
                         [("1.1", "covered"), ("1.2", "partial"), ("1.3", "open"), ("2", "unclaimed")])
        self.assertEqual(cp["requirements"][0]["text"], "First requirement")
        self.assertEqual(cp["eta_s"], BUILD)

    def test_broken_chain_invalidates_all_coverage_credit(self):
        self.coverage_fixture()
        self.events(["T1", "T3"], broken=True)
        cp = self.main_lane()["checkpoint"]
        self.assertEqual((cp["covered"], cp["partial"]), (0, 0))
        self.assertEqual([r["status"] for r in cp["requirements"]], ["open", "open", "open", "unclaimed"])

    def test_ledger_pass_without_queue_done_does_not_credit_coverage(self):
        self.queue([self.task("todo", covers=["1.1"])])
        self.events(["T1"])
        cp = self.main_lane()["checkpoint"]
        self.assertEqual(cp["covered"], 0)
        self.assertEqual(cp["requirements"][0]["status"], "open")

    def test_torn_tail_is_skipped_without_any_filesystem_writes(self):
        self.coverage_fixture()
        self.second_lane()
        self.events(["T1"], torn=True)
        self.add_run("planner", 100, 50)
        before = self.listing()
        self.assertEqual(self.main_lane()["checkpoint"]["covered"], 1)
        self.snap()
        self.assertEqual(self.listing(), before)

    def test_missing_spec_has_unknown_coverage_and_empty_requirements(self):
        (self.root / "docs/specs/layer-1-design.md").unlink()
        cp = self.main_lane()["checkpoint"]
        for key in ("covered", "partial", "requirements_total"):
            self.assertIsNone(cp[key])
        self.assertEqual(cp["requirements"], [])

    def test_spec_selection_queue_then_limits_then_default(self):
        self.raw("docs/specs/alternate.md", "## 9. Alternate\n")
        self.limits["spec_file"] = "docs/specs/alternate.md"
        self.write("charter/limits.json", self.limits)
        self.assertEqual(self.main_lane()["checkpoint"]["requirements"][0]["id"], "9")
        self.queue([], spec_file="docs/specs/layer-1-design.md")
        self.assertEqual(self.main_lane()["checkpoint"]["requirements_total"], 4)

    def test_unplanned_work_uses_default_six_tasks_and_excludes_blocked(self):
        self.queue([self.task(kind="plan"), self.task("blocked", "B"), self.task("done", "D")])
        cp = self.main_lane()["checkpoint"]
        self.assertEqual(cp["eta_s"], 1200 + 6 * BUILD)
        self.assertIn("estimate", cp["eta_basis"].lower())
        self.assertIn("6", cp["eta_basis"])

    def test_unplanned_work_uses_median_planner_task_count_across_lanes(self):
        self.second_lane()
        self.add_run("planner", 10000, 900, data={"tasks": [{"id": str(i)} for i in range(2)]})
        self.add_run("planner", 8000, 900, lane="p2", data={"tasks": [{"id": str(i)} for i in range(4)]})
        self.queue([self.task(kind="plan")])
        self.assertEqual(self.main_lane()["checkpoint"]["eta_s"], 1200 + 3 * BUILD)

    def test_checkpoint_eta_divides_only_by_running_lanes_on_layer(self):
        self.second_lane()
        self.queue([self.task("merge_pending")])
        self.add_run("probe-claude", 20, task=None)
        self.add_run("probe-codex", 20, lane="p2", task=None)
        self.assertEqual(self.main_lane()["checkpoint"]["eta_s"], 300)
        self.heartbeat(181, lane="p2")
        self.assertEqual(self.main_lane()["checkpoint"]["eta_s"], 600)
        self.heartbeat(lane="p2")
        self.write("state/lanes/p2/queue.json", dict(layer="layer-2", tasks=[]))
        self.assertEqual(self.main_lane()["checkpoint"]["eta_s"], 600)

    def test_project_uses_fractional_coverage_and_current_phase_eta(self):
        self.coverage_fixture()
        p = self.snap()["project"]
        self.assertEqual(p["phases"], self.phases)
        self.assertEqual((p["phases_done"], p["phases_total"], p["current_phase"]), (1, 3, "1. Loop"))
        self.assertAlmostEqual(p["current_fraction"], 1.5 / 4)
        self.assertAlmostEqual(p["overall"], (1 + 1.5 / 4) / 3)
        self.assertEqual(p["eta_s"], BUILD)
        self.assertIn("estimate", p["eta_basis"].lower())
        self.assertIn("later", p["eta_basis"].lower())

    def test_project_falls_back_to_task_fraction_without_spec(self):
        (self.root / "docs/specs/layer-1-design.md").unlink()
        self.queue([self.task("done"), self.task(id="T2"), self.task("superseded", "old")])
        self.assertEqual(self.snap()["project"]["current_fraction"], 0.5)

    def test_project_eta_combines_lane_work_and_running_lane_divisor(self):
        self.second_lane("layer-2")
        self.queue([self.task("merge_pending")])
        self.write("state/lanes/p2/queue.json", dict(layer="layer-2", tasks=[self.task("todo")]))
        self.add_run("probe-claude", 20, task=None)
        self.add_run("probe-codex", 20, lane="p2", task=None)
        self.assertEqual(self.snap()["project"]["eta_s"], (600 + BUILD) / 2)

    def test_single_lane_tokens_caps_and_utc_reset(self):
        t = self.snap()["tokens"]
        self.assertEqual(t["day"], "2026-10-01")
        self.assertEqual(instant(t["resets_at"]), datetime(2026, 10, 2, tzinfo=timezone.utc))
        for provider, used, cap in [("claude", 400, 1000), ("codex", 50, 2000)]:
            p = t["providers"][provider]
            self.assertEqual((p["used"], p["cap"], p["by_lane"]), (used, cap, {"main": used}))
            self.assertAlmostEqual(p["fraction"], used / cap)
            self.assertEqual(p["burn_per_h"], 0)
            self.assertIsNone(p["time_to_cap_s"])

    def test_shared_meter_sums_lanes_instead_of_double_counting_legacy(self):
        self.second_lane()
        self.write("state/shared/meter/main.json", {"2026-10-01": dict(claude=200, codex=30)})
        self.write("state/shared/meter/p2.json", {"2026-10-01": dict(claude=300, codex=70),
                                                 "2026-09-30": dict(claude=9999)})
        p = self.snap()["tokens"]["providers"]
        self.assertEqual(p["claude"]["used"], 500)
        self.assertEqual(p["claude"]["by_lane"], dict(main=200, p2=300))
        self.assertEqual(p["codex"]["used"], 100)
        self.assertEqual(p["codex"]["by_lane"], dict(main=30, p2=70))

    def test_burn_uses_end_time_provider_and_all_lanes(self):
        self.second_lane()
        self.add_run("planner", 7200, 6000, tokens=100)  # old start, recent end
        self.add_run("reviewer", 2000, 100, tokens=200, lane="p2")
        self.add_run("builder", 5000, 100, tokens=9999)  # ended outside hour
        self.add_run("test_writer", 500, 100, tokens=50, provider="codex")
        self.add_run("planner", 30, tokens=9999)  # unfinished: no billed output
        p = self.snap()["tokens"]["providers"]
        self.assertEqual(p["claude"]["burn_per_h"], 300)
        self.assertEqual(p["codex"]["burn_per_h"], 50)
        self.assertEqual(p["claude"]["time_to_cap_s"], 7200)

    def test_time_to_cap_is_zero_at_cap_and_unknown_without_cap(self):
        self.write("state/bootstrap/meter.json", {"2026-10-01": dict(claude=1000)})
        self.assertEqual(self.snap()["tokens"]["providers"]["claude"]["time_to_cap_s"], 0)
        self.limits.pop("claude_daily_token_cap")
        self.write("charter/limits.json", self.limits)
        self.add_run("planner", 100, 50, tokens=100)
        self.assertIsNone(self.snap()["tokens"]["providers"]["claude"]["time_to_cap_s"])

    def test_holds_and_daily_run_count(self):
        self.second_lane()
        until = NOW + timedelta(hours=1)
        self.write("state/bootstrap/holds.json", dict(claude=until.isoformat(), codex=NOW.isoformat()))
        self.add_run("planner", 10)
        self.add_run("reviewer", 20, 5, lane="p2")
        self.add_run("planner", 86400, 5)
        t = self.snap()["tokens"]
        self.assertEqual((t["runs_today"], t["runs_cap"]), (2, 50))
        self.assertEqual(instant(t["providers"]["claude"]["held_until"]), until)
        self.assertIsNone(t["providers"]["codex"]["held_until"])

    def test_shared_holds_use_latest_unexpired_hold(self):
        until = NOW + timedelta(hours=2)
        self.write("state/shared/holds/main.json", dict(claude=(NOW + timedelta(hours=1)).isoformat()))
        self.write("state/shared/holds/p2.json", dict(claude=until.isoformat(), codex=NOW.isoformat()))
        p = self.snap()["tokens"]["providers"]
        self.assertEqual(instant(p["claude"]["held_until"]), until)
        self.assertIsNone(p["codex"]["held_until"])

    def test_timeline_window_order_and_running_fields(self):
        recent = self.add_run("reviewer", 1000, 100, tokens=42, provider="codex")
        old = self.add_run("planner", 50000, 100)
        overlap = self.add_run("planner", 44000, 2000)
        running = self.add_run("probe-claude", 30, task=None)
        t = self.snap()["timeline"]
        self.assertEqual(instant(t["from"]), NOW - timedelta(hours=12))
        self.assertEqual(instant(t["to"]), NOW)
        rows = t["lanes"]["main"]
        self.assertEqual([r["run_id"] for r in rows], [overlap, recent, running])
        self.assertNotIn(old, [r["run_id"] for r in rows])
        self.assertEqual((rows[1]["role"], rows[1]["task_id"], rows[1]["ok"],
                          rows[1]["tokens"], rows[1]["provider"]), ("reviewer", "T1", True, 42, "codex"))
        self.assertEqual(instant(rows[1]["end"]), NOW - timedelta(seconds=900))
        self.assertEqual(instant(rows[2]["start"]), NOW - timedelta(seconds=30))
        self.assertIsNone(rows[2]["end"])
        self.assertTrue(rows[2]["running"])
        self.assertFalse(rows[1]["running"])

    def test_empty_root_is_read_only_and_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = importlib.import_module("core.dashboard").snapshot(root, NOW)
            self.assertIsInstance(result, dict)
            self.assertEqual(list(root.rglob("*")), [])

    def test_corrupt_and_partial_json_everywhere_never_raises_or_writes(self):
        self.second_lane()
        self.add_run("planner", 100, 50)
        self.raw("state/bootstrap/runs/garbage-name/prompt.md", "garbage")
        self.write("state/bootstrap/holds.json", {})
        self.write("state/shared/meter/main.json", {})
        self.write("state/shared/holds/main.json", {})
        paths = list(self.root.rglob("*.json"))
        for bad in ('{"unfinished":', 'null', '[]', '{"tasks": [null, 7], "phases": [false]}'):
            with self.subTest(bad=bad):
                for path in paths:
                    path.write_bytes(bad.encode())
                self.raw("state/bootstrap/ledger/events.jsonl", '{broken\n{"torn":')
                before = self.listing()
                self.assertIsInstance(self.snap(), dict)
                self.assertEqual(self.listing(), before)

    def test_unreadable_lane_still_appears_with_unknown_layer(self):
        self.second_lane()
        self.raw("state/lanes/p2/queue.json", "{partial")
        lane = self.snap()["lanes"][1]
        self.assertEqual(lane["name"], "p2")
        self.assertIsNone(lane["layer"])

    def test_cli_prints_json_for_explicit_root(self):
        importlib.import_module("core.dashboard")  # missing feature is an import error, not a JSON error
        proc = subprocess.run([sys.executable, "-m", "core.dashboard", "--json", "--root", str(self.root)],
                              cwd=REPO, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        s = json.loads(proc.stdout)
        self.assertLessEqual({"lanes", "tokens", "project", "timeline"}, s.keys())
        self.assertEqual(s["lanes"][0]["layer"], "layer-1")


if __name__ == "__main__":
    unittest.main()


class ReviewRound1(ForgeFixture, unittest.TestCase):
    """R64 review round 1: unreadable is unknown, never zero; stale runs; reads never block a writer."""

    def test_unparseable_queue_is_unknown_not_empty(self):
        self.raw("state/bootstrap/queue.json", '{"layer": "layer-1", "tasks": [')
        s = self.snap()
        cp = s["lanes"][0]["checkpoint"]
        self.assertIsNone(cp["tasks_total"])
        self.assertIsNone(cp["tasks_done"])
        self.assertIsNone(cp["eta_s"])
        self.assertIsNone(s["project"]["eta_s"])
        self.assertIsNone(s["project"]["current_fraction"])

    def test_missing_queue_is_empty(self):
        (self.root / "state/bootstrap/queue.json").unlink()
        cp = self.main_lane()["checkpoint"]
        self.assertEqual((cp["tasks_done"], cp["tasks_total"]), (0, 0))

    def test_unparseable_questions_is_unknown(self):
        self.raw("state/bootstrap/questions.json", "{broken")
        self.assertIsNone(self.main_lane()["open_questions"])

    def test_bad_meter_numbers_are_unknown_and_never_crash(self):
        for value in ("1e309", '"damaged"', "-5"):
            with self.subTest(value=value):
                self.raw("state/bootstrap/meter.json", '{"2026-10-01": {"claude": %s, "codex": 50}}' % value)
                tok = self.snap()["tokens"]["providers"]
                self.assertIsNone(tok["claude"]["used"])
                self.assertEqual(tok["codex"]["used"], 50)

    def test_run_far_past_the_agent_timeout_is_abandoned(self):
        self.add_run("builder", 600 * 2 + 301)
        self.assertIsNone(self.main_lane()["current"])
        self.add_run("reviewer", 60)
        self.assertEqual(self.main_lane()["current"]["role"], "reviewer")

    def test_judging_is_not_inferred_hours_later(self):
        self.add_run("builder", 7 * 3600, 100)
        self.assertIsNone(self.main_lane()["current"])

    def test_conductor_write_retries_a_rename_refused_by_a_reader(self):
        from unittest import mock
        from core import bootstrap
        target = self.raw("state/bootstrap/queue.json", "{}")
        tmp = self.raw("state/bootstrap/queue.json.tmp", '{"tasks": []}')
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, "sharing violation (a reader has it open)")
            real(src, dst)
        with mock.patch.object(bootstrap.os, "replace", side_effect=flaky), mock.patch.object(bootstrap.time, "sleep"):
            bootstrap.replace_retry(tmp, target)
        self.assertEqual(len(calls), 3)
        self.assertEqual(target.read_bytes(), b'{"tasks": []}')

    def test_conductor_write_gives_up_after_its_retries(self):
        from unittest import mock
        from core import bootstrap
        with mock.patch.object(bootstrap.os, "replace", side_effect=PermissionError(13, "x")), \
                mock.patch.object(bootstrap.time, "sleep"):
            with self.assertRaises(PermissionError):
                bootstrap.replace_retry(self.root / "a", self.root / "b")

    def test_snapshot_reads_run_through_the_shared_reader(self):
        d = importlib.import_module("core.dashboard")
        target = self.raw("state/bootstrap/queue.json", '{"x": 1}')
        with d.open_shared(target) as f:
            self.assertEqual(f.read(), b'{"x": 1}')
