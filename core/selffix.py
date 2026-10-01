"""R66 self-fix hooks for the bootstrap conductor (docs/specs/bootstrap-conductor.md, R66 to R66i).

A mixin, so `core/bootstrap.py` keeps only small hook calls. Every method here is a no-op unless the charter limits
set `self_fix` (D-035: switched on deliberately). Nothing here pauses Forge: Forge fixes what it may fix itself
(D-037), and what only Ben can do becomes a blocker record with a ready fix kit (core.blockers).
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path

from core import blockers as blockers_mod
from core import channel
from core import drift as drift_mod
from core import lanes as lanes_mod

GATE_WATCH_S = 300
MERGE_RETRY_S = 1800
INTERNAL_KINDS = frozenset({"gate", "merge", "capability", "unpark"})  # R66: stored, never mailed as questions
PAUSE_TEXTS = ("a re-plan needs Ben", "drift keeper asked for a re-plan", "drift keeper failed 3 times")
MARKER_RE = re.compile(r"^(<{7} |={7}$|>{7} )", re.M)


def _bs():
    from core import bootstrap  # lazy: bootstrap imports this module
    return bootstrap


def retry_request(state_root: Path, lane: str, bid: str, not_needed: bool = False, wait_s: float = 0,
                  between_runs=None) -> tuple[int, str]:
    """`python -m core.bootstrap retry` (R66e): hand Forge Ben's retry through the lane's answer drop folder. Writes
    nothing in the lane's state; waits (up to wait_s) until the lane's conductor is between agent runs."""
    state_root = Path(state_root)
    if lane != lanes_mod.MAIN and lanes_mod.name_problem(lane):
        return 2, f"bad lane: {lanes_mod.name_problem(lane)}"
    state = lanes_mod.state_dir(state_root, lane)
    rec = blockers_mod.Blockers(state, lane).get(str(bid))
    if rec is None:
        return 2, f"no blocker {bid} in lane {lane}"
    if rec.get("status") != "ready_for_ben":
        return 2, f"blocker {bid} is {rec.get('status')}: nothing to retry"
    if rec.get("category") == "unpark":
        return 2, f"blocker {bid} needs the unpark email from its fix kit, not a retry"
    if between_runs is None:
        from core.status_page import conductor_between_runs as between_runs
    service = lanes_mod.service_dir(state_root, lane)
    deadline = time.time() + max(0.0, float(wait_s))
    while not between_runs(service):
        if time.time() >= deadline:
            return 2, "Forge is in the middle of an agent run; try again in a few minutes"
        time.sleep(2)
    channel.drop_answer(lanes_mod.channel_dir(state_root, lane) / "in", rec["id"], rec["code"],
                        "not needed" if not_needed else "retry", "fix kit")
    return 0, f"Forge will retry on its next step: {rec.get('summary')}. You'll get an email when it's fixed."


class SelfFixMixin:
    """Mixed into core.bootstrap.Conductor."""

    # ---------------------------------------------------------------- basics
    @property
    def _self_fix(self) -> bool:
        return bool(self.limits.get("self_fix"))

    def _sf_limit(self) -> int:
        try:
            return max(0, int(self.limits.get("self_fix_attempts", 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def blockers(self) -> blockers_mod.Blockers:
        return blockers_mod.Blockers(self.state, self.lane, self.clock)

    def _sf_state_root(self) -> Path:
        return self.shared.parent if self.shared is not None else self.state.parent

    def _sf_age(self, iso) -> float:
        try:
            return (self.clock() - datetime.fromisoformat(str(iso))).total_seconds()
        except (TypeError, ValueError):
            return float("inf")

    # ---------------------------------------------------------------- outbox (R66g)
    def _sf_mail(self, subject: str, body: str, *, bid: str | None = None, event: str | None = None,
                 digest: bool = False) -> None:
        box = self._read("outbox.json", [])
        box = box if isinstance(box, list) else []
        box.append({"subject": str(subject)[:300], "body": str(body)[:20000], "bid": bid, "event": event,
                    "digest": bool(digest), "at": self.clock().isoformat()})
        self._write("outbox.json", box[-blockers_mod.OUTBOX_KEEP:])

    def _sf_flush(self) -> None:
        if self._kill_set():
            return
        box = self._read("outbox.json", [])
        if not isinstance(box, list) or not box:
            return
        keep, blocked = [], False
        for x in box:
            if not isinstance(x, dict):
                continue
            if blocked or (x.get("digest") and self._channel_on):
                keep.append(x)
                continue
            if self._send(str(x.get("subject", "")), str(x.get("body", ""))):
                continue
            keep.append(x)
            blocked = True  # over budget, quiet hours or SMTP trouble: the rest waits too, in order
        if keep != box:
            self._write("outbox.json", keep)

    def _sf_drop_mail(self, bid: str, event: str | None = None) -> bool:
        box = self._read("outbox.json", [])
        box = box if isinstance(box, list) else []
        keep = [x for x in box if not (isinstance(x, dict) and x.get("bid") == bid
                                       and (event is None or x.get("event") == event))]
        if len(keep) != len(box):
            self._write("outbox.json", keep)
            return True
        return False

    def _sf_digest_lines(self) -> list[str]:
        if not self._self_fix:
            return []
        box = self._read("outbox.json", [])
        held = {x.get("bid") for x in (box if isinstance(box, list) else []) if isinstance(x, dict) and x.get("digest")}
        recs = self.blockers.active() + [r for r in self.blockers.all().values()
                                         if r.get("status") == "fixed" and r.get("id") in held]
        notices = [x for x in (box if isinstance(box, list) else [])
                   if isinstance(x, dict) and x.get("digest") and not x.get("bid")]
        lines = blockers_mod.digest_lines(recs)
        for x in notices:  # standalone notices (gate passed, merged into main) go in whole, capped
            lines += [str(x.get("subject", "")), str(x.get("body", ""))[:3000], ""]
        return lines

    def _sf_digest_sent(self) -> None:
        if not self._self_fix:
            return
        box = self._read("outbox.json", [])
        if isinstance(box, list):
            keep = [x for x in box if isinstance(x, dict) and not x.get("digest")]
            if len(keep) != len(box):
                self._write("outbox.json", keep)

    # ---------------------------------------------------------------- records
    def _sf_open(self, kind: str, key, summary: str, category: str | None = None) -> dict:
        b = self.blockers
        new = b.find(kind, key) is None
        rec = b.open(kind, key, summary, category)
        if new:
            subject, body = blockers_mod.opened_mail(rec, self.page_url)
            self._sf_mail(subject, body, bid=rec["id"], event="opened", digest=True)
        return rec

    def _sf_ready(self, rec: dict, kit: dict) -> dict | None:
        cur = self.blockers.get(rec["id"]) or rec
        old = cur.get("kit") or {}
        if cur.get("status") == "ready_for_ben" and all(old.get(k) == kit.get(k)
                                                         for k in ("category", "where", "paste", "script")):
            return cur  # the same fix again (only its wording may differ): no rewrite, no second email
        try:
            new = self.blockers.ready_for_ben(rec["id"], kit)
        except ValueError as e:
            self._log(f"fix kit refused for {rec['id']}: {e}"[:500])
            return None
        self._sf_drop_mail(rec["id"], "opened")  # the "fixing it myself" email is outdated now
        subject, body = blockers_mod.opened_mail(new, self.page_url)
        self._sf_mail(subject, body, bid=new["id"], event="opened", digest=False)  # Needs you: at once
        return new

    def _sf_fixed(self, rec: dict | None) -> None:
        if not rec or rec.get("status") == "fixed":
            return
        new = self.blockers.fixed(rec["id"])
        if self._sf_drop_mail(rec["id"], "opened"):
            return  # fixed before Ben heard of it: no email at all
        subject, body = blockers_mod.fixed_mail(new)
        self._sf_mail(subject, body, bid=new["id"], event="fixed", digest=True)

    def _sf_fix_kind(self, kind: str, key=None) -> None:
        if not self._self_fix:
            return
        for r in self.blockers.active():
            if r.get("kind") == kind and (key is None or r.get("key") == str(key)):
                self._sf_fixed(r)

    def _sf_retry_kit(self, rec: dict, why: str, expect: str, not_needed: bool = False) -> dict:
        return blockers_mod.retry_kit(self.repo, self.lane, rec["id"], why, expect, not_needed)

    # ---------------------------------------------------------------- the tick (each step)
    def _blockers_tick(self) -> None:
        if not self._self_fix or self._kill_set():
            return
        for part in (self._sf_migrate, self._sf_verify, self._sf_merge_retry, self._sf_gate_watch, self._sf_flush):
            try:
                part()
            except Exception as e:  # noqa: BLE001 - a blocker problem never stops the conductor
                self._log(f"blockers {part.__name__} error: {e!r}"[:500])

    def _sf_verify(self) -> None:
        tasks = {t.get("id"): t for t in self._queue().get("tasks", []) if isinstance(t, dict)}
        for r in self.blockers.active():
            if r.get("kind") in ("blocked", "merge") and (tasks.get(r.get("key")) or {}).get("status") == "done":
                self._sf_fixed(r)
        bs = _bs()
        for rec in bs.Journal(self.state).all():  # T1B3e: a blocked merge record is a blocker Forge retries
            if rec.get("status") == "blocked" and (rec.get("blocked") or {}).get("qid"):
                self._sf_open("merge", rec["tid"], f"merging task {rec['tid']} into the layer is blocked: "
                                                  f"{(rec.get('blocked') or {}).get('reason')}")
        root = self._sf_state_root()
        for r in self.blockers.active():  # R66i: a Do it run finished; re-check what it fixed
            st = blockers_mod.run_status(root, str(r.get("id")))
            if not st or not st.get("finished_at") or st.get("finished_at") == r.get("run_seen"):
                continue
            self.blockers.update(r["id"], run_seen=st["finished_at"])
            if r.get("kind") == "capability":
                self._sf_force_check(str(r.get("key")))

    def _sf_force_check(self, name: str) -> None:
        force = self._read("readiness_force.json", [])
        force = [n for n in force if isinstance(n, str)] if isinstance(force, list) else []
        if name and name not in force:
            self._write("readiness_force.json", force + [name])

    # ---------------------------------------------------------------- migration (R66h)
    def _sf_migrate(self) -> None:
        if self._read("r66.json", {}).get("done"):
            return
        bs = _bs()
        qs = self._read("questions.json", {})
        now = self.clock().isoformat()
        blocked_tasks, replans = [], []
        for qid, q in qs.items():
            if not isinstance(q, dict) or q.get("status") != "open":
                continue
            kind = q.get("kind")
            if kind in INTERNAL_KINDS and not q.get("auto"):
                q.update(auto=True, hold=True, delivered=True)
                if kind == "gate":
                    q["merge_set"] = False
            elif kind == "replan":
                q.update(status="answered", answer="R66: re-plans no longer wait for you", closed_at=now)
                replans.append(str(q.get("subject", "")))
            elif kind == "blocked":
                q.update(status="answered", answer="R66: Forge reopens blocked tasks itself", closed_at=now)
                if q.get("task"):
                    blocked_tasks.append((q["task"], str(q.get("subject", ""))))
        self._write("questions.json", bs._prune_questions(qs))
        paused = self.state / "PAUSED"
        try:
            text = paused.read_text(encoding="utf-8").strip()
        except OSError:
            text = None
        if text is not None and text in PAUSE_TEXTS:
            paused.unlink(missing_ok=True)
            self._log("R66: removed a re-plan pause; re-plans no longer wait for Ben")
        if replans and getattr(self, "manager", None) is not None:
            d = drift_mod.load(self.state) or drift_mod.adopt([], [], False, self._activity().total())
            if not d.get("replan"):
                drift_mod.new_replan(d, [f"migrated from a re-plan question: {s}" for s in replans], "R66 migration")
                drift_mod.save(self.state, d)
        tasks = {t.get("id"): t for t in self._queue().get("tasks", [])}
        for tid, subject in blocked_tasks:
            if (tasks.get(tid) or {}).get("status") == "blocked":
                self._sf_block(tid, f"migrated from an open question: {subject}")
        self._write("r66.json", {"done": True, "at": now})

    # ---------------------------------------------------------------- blocked tasks (R66d)
    def _sf_reopen(self, tid: str, note: str | None, reopens: int) -> None:
        qd = self._queue()
        for t in qd["tasks"]:
            if t["id"] == tid:
                if note:
                    t["trouble_notes"] = list(t.get("trouble_notes") or []) + [note]
                t["status"] = "tests_ok" if t.get("tests_commit") else "todo"
                t["fail_signatures"], t["fails_since"], t["troubleshot"], t["troubleshoots"] = [], 0, False, 0
                t["test_rejects"], t["plan_rejects"], t["focus_s"] = 0, 0, 0
                t["auto_reopens"] = reopens
        self._save_queue(qd)

    def _sf_block(self, tid: str, why: str) -> None:
        t = self._task(tid)
        title = str(t.get("title", ""))
        n = int(t.get("auto_reopens") or 0)
        if "ledger parked the contract" in why:
            return self._sf_parked(tid, title, why)
        if n < self._sf_limit():
            self._sf_reopen(tid, f"AUTO-REOPEN {n + 1} (R66): blocked because: {why}. Follow the latest "
                                 "TROUBLESHOOTER NOTES; if this route is a dead end, take the alternative.", n + 1)
            self._sf_open("blocked", tid, f"task {tid} ({title}) got stuck; Forge reopened it with the "
                                          "troubleshooter's notes")
            return
        self._update(tid, status="blocked")
        rec = self._sf_open("blocked", tid, f"task {tid} ({title}) is still stuck after {n} automatic retries")
        self._sf_ready(rec, self._sf_retry_kit(
            rec, f"Forge reopened task {tid} {n} times with its troubleshooter's notes and it is still blocked "
                 f"({str(why)[:300]}). Running this line gives it a fresh set of retries; other tasks carry on "
                 "meanwhile. If you'd rather drop the task, do nothing.",
            f"Task {tid} goes back to work, and you get an email when it's fixed."))

    def _sf_parked(self, tid: str, title: str, why: str) -> None:
        self._update(tid, status="blocked")
        rec = self._sf_open("blocked", tid, f"task {tid} ({title}) is parked by the ledger", category="unpark")
        if rec.get("status") == "ready_for_ben":
            return
        qs = self._read("questions.json", {})
        qid = next((k for k, v in qs.items() if v.get("kind") == "unpark" and v.get("task") == tid
                    and v.get("status") == "open"), None)
        if qid is None:
            qid = self._ask("unpark", f"unpark {tid}", f"Unpark task {tid} ({title}) in the ledger.", task=tid)
        code = self._read("questions.json", {})[qid]["code"]
        line = blockers_mod.gmail_compose_line(self.owner, f"[Forge Q-{qid} {code}] unpark {tid}", "unpark")
        self._sf_ready(rec, {
            "why": f"The ledger parked task {tid} after it used its attempt budget. Only you may unpark a ledger "
                   "contract (charter). The line opens an email to yourself, already written: sending it is your "
                   "approval.",
            "category": "unpark", "where": "Win + R", "paste": line,
            "expect": f"Forge unparks task {tid}, retries it, and emails you when it's fixed."})

    def _sf_unpark(self, q: dict, reply: str) -> bool:
        first = reply.split()[0].lower().strip(".!,") if reply.split() else ""
        if first not in ("unpark", "y", "yes"):
            return False
        tid = str(q.get("task", ""))
        if self._ledger().contracts().get(tid, {}).get("status") == "parked":
            self._apply(f"unpark-{tid}-{secrets.token_hex(4)}", "unpark", tid, "benjamin")
        self._sf_reopen(tid, "Ben unparked this task (R66d).", 0)
        r = self.blockers.find("blocked", tid)
        if r:
            self.blockers.back_to_fixing(r["id"])
        return True

    # ---------------------------------------------------------------- re-plans (R66c)
    def _sf_recut(self, d: dict) -> bool:
        rp = d.get("replan") or {}
        n = int(rp.get("recuts") or 0)
        try:
            limit = int(self.limits.get("replan_recuts", 2))
        except (TypeError, ValueError):
            limit = 2
        if n >= limit:
            return False
        n += 1
        mx = max(1, 4 - n)
        notes = "; ".join(str(x) for x in (rp.get("notes") or [])[-6:])[:3000]
        rp["recuts"], rp["attempts"], rp["max_tasks"] = n, 0, mx
        rp["reasons"] = (list(rp.get("reasons") or []) + [
            f"AUTO RE-CUT {n}: earlier proposals were rejected for: {notes}. Propose a SMALLER plan: at most {mx} "
            "tasks, each covering as few requirements as possible."])[-20:]
        d["replan"] = rp
        drift_mod.save(self.state, d)
        self._sf_open("replan", "replan", f"re-plan {rp.get('id')} was rejected twice; Forge is re-cutting it smaller")
        return True

    def _sf_escalate_replan(self, d: dict, why: str) -> None:
        rp = d.get("replan") or {}
        d["replan"] = None
        d["auto_replans"] = 0
        drift_mod.restart_window(d, self._activity().total())
        drift_mod.save(self.state, d)
        rec = self._sf_open("replan", "replan", f"Forge couldn't make an acceptable re-plan itself ({why})")
        reasons = "; ".join(str(x) for x in (rp.get("reasons") or [])[:5])[:600]
        self._sf_ready(rec, self._sf_retry_kit(
            rec, f"Forge needs a re-plan and couldn't make one that passes review: {why}. Trigger: "
                 f"{rp.get('trigger')}. {('Reasons: ' + reasons) if reasons else ''} Other tasks carry on. Running "
                 "this line queues a fresh re-plan.",
            "Forge queues a fresh re-plan; you get an email when one is accepted."))

    def _sf_drift_failed(self, error) -> None:
        d = drift_mod.load(self.state)
        if d and d.get("stall"):  # the stall that wanted this check must not ask for it every step
            d["stall"] = None
            drift_mod.restart_window(d, self._activity().total())
            drift_mod.save(self.state, d)
        rec = self._sf_open("drift", "drift", "the drift keeper keeps returning unusable output")
        self._sf_ready(rec, self._sf_retry_kit(
            rec, f"The drift keeper returned unusable output 3 times in a row (last error: {str(error)[:300]}). "
                 "Forge carries on building without it. Running this line asks for a drift check again.",
            "The next drift check runs; you get an email when it works."))

    def _sf_drift_no_manager(self, reasons: list[str], trigger) -> None:
        rec = self._sf_open("replan", "replan", "the drift keeper wants a re-plan and no Manager is configured")
        self._sf_ready(rec, self._sf_retry_kit(
            rec, ("Stall rule: " + str(trigger) + ". " if trigger else "") + "The drift keeper asked for a re-plan: "
                 + "; ".join(reasons)[:600] + ". Forge has no Manager to re-plan with, so it carries on with the "
                 "current queue. Running this line asks for a drift check again.",
            "The next drift check runs; you get an email when the re-plan is resolved."))

    # ---------------------------------------------------------------- gate (R66b)
    def _sf_gate(self, pr: str, url: str, report: str, layer: str) -> None:
        qid = self._ask("gate", f"{layer} passed its gate: merging it into main",
                        report + f"\n\nPull request: {url}", pr=pr, merge_set=False)
        self._sf_mail(f"[Forge] {layer} passed its gate: merging it into main myself",
                      report + f"\n\nPull request: {url}\n\nForge labels it human-approved (D-037) and turns on "
                      "auto-merge: GitHub merges it once CI's core check passes. Nothing for you to do; you get "
                      "one more email when it's in main.\n\n" + blockers_mod.STOP_LINE, digest=True)
        self._sf_gate_merge(qid, pr, layer, first=True)
        self._sf_flush()

    def _sf_gate_merge(self, qid: str, pr: str, layer: str, first: bool = False) -> bool:
        c1, o1 = self.gh(["pr", "edit", pr, "--add-label", "human-approved"])
        c2, o2 = self.gh(["pr", "merge", pr, "--merge", "--delete-branch", "--auto"]) if c1 == 0 else (c1, o1)
        if c1 == 0 and c2 == 0:
            qs = self._read("questions.json", {})
            if qid in qs:
                qs[qid]["merge_set"] = True
                self._write("questions.json", qs)
            return True
        self._log(f"gate auto-merge failed for PR {pr}: {o1} {o2}"[:500])
        rec = self._sf_open("gate", pr, f"setting {layer}'s pull request #{pr} to merge failed")
        if first:
            return False
        rec = self.blockers.attempt(rec["id"])
        if int(rec.get("attempts") or 0) >= self._sf_limit():
            line = f"cd {self.repo}; gh pr merge {pr} --merge --delete-branch --auto"
            self._sf_ready(rec, {
                "why": f"GitHub refused to merge pull request #{pr} for Forge ({str(o2 or o1)[:300]}).",
                "category": "exhausted", "where": "PowerShell", "paste": line, "script": line,
                "expect": f"Pull request #{pr} shows auto-merge on, and Forge emails you when it's in main."})
        return False

    def _sf_gate_watch(self) -> None:
        qs = self._read("questions.json", {})
        st = self._read("gate_watch.json", {})
        st = st if isinstance(st, dict) else {}
        layer = self._queue().get("layer")
        for qid, q in list(qs.items()):
            if not (isinstance(q, dict) and q.get("kind") == "gate" and q.get("auto") and q.get("status") == "open"):
                continue
            if self._sf_age(st.get(qid)) < GATE_WATCH_S:
                continue
            st[qid] = self.clock().isoformat()
            self._write("gate_watch.json", st)
            pr = str(q.get("pr"))
            gb = self.blockers.find("gate", pr)
            if not q.get("merge_set") and not (gb and gb.get("status") == "ready_for_ben"):
                if not self._sf_gate_merge(qid, pr, layer):
                    continue  # bounded: once the blocker is ready for Ben, only his retry tries again
            code, out = self.gh(["pr", "view", pr, "--json", "state"])
            try:
                state = str(json.loads(out or "{}").get("state", "")).upper() if code == 0 else ""
            except ValueError:
                state = ""
            if state == "MERGED":
                qs = self._read("questions.json", {})
                qs[qid].update(status="answered", answer="merged automatically (R66)",
                               closed_at=self.clock().isoformat(),
                               closed_seq=self._read("q_seq.json", {}).get("n", 0))
                self._write("questions.json", _bs()._prune_questions(qs))
                self._sf_fix_kind("gate", pr)
                self._sf_mail(f"[Forge] {layer} is merged into main", f"Pull request #{pr} is merged into main. "
                              "Nothing for you to do.\n\n" + blockers_mod.STOP_LINE, digest=True)
            elif state == "OPEN" and q.get("closed_seen"):  # Ben reopened it: auto-merge again
                qs = self._read("questions.json", {})
                qs[qid].update(closed_seen=False, merge_set=False)
                self._write("questions.json", qs)
                if gb:
                    self.blockers.back_to_fixing(gb["id"])
                self._sf_gate_merge(qid, pr, layer, first=True)
            elif state == "CLOSED":
                qs = self._read("questions.json", {})
                if qs[qid].get("merge_set") or not qs[qid].get("closed_seen"):
                    qs[qid].update(merge_set=False, closed_seen=True)
                    self._write("questions.json", qs)
                rec = self._sf_open("gate", pr, f"{layer}'s pull request #{pr} was closed without merging")
                line = f"cd {self.repo}; gh pr reopen {pr}"
                self._sf_ready(rec, {
                    "why": f"Pull request #{pr} ({layer} into main) was closed without being merged. If that was "
                           "on purpose, do nothing. Otherwise reopen it and Forge merges it.",
                    "category": "exhausted", "where": "PowerShell", "paste": line, "script": line,
                    "expect": f"Pull request #{pr} is open again; Forge turns auto-merge on and emails you when it's "
                              "in main."})

    # ---------------------------------------------------------------- merges (R66f)
    def _sf_merge_retry(self) -> None:
        bs = _bs()
        journal = bs.Journal(self.state)
        for r in self.blockers.active():
            if r.get("kind") != "merge" or r.get("status") != "fixing":
                continue
            rec = journal.load(str(r.get("key")))
            if rec is None or rec.get("status") != "blocked":
                continue
            if self._sf_age(r.get("last_try") or r.get("created_at")) < MERGE_RETRY_S:
                continue
            if int(r.get("attempts") or 0) >= self._sf_limit():
                self._sf_ready(r, self._sf_retry_kit(
                    r, f"Forge retried merging task {r.get('key')} {r.get('attempts')} times and it is still blocked "
                       f"({(rec.get('blocked') or {}).get('reason')}). Other tasks carry on. Running this line gives "
                       "it a fresh set of retries.",
                    f"Task {r.get('key')} merges, and you get an email when it's fixed."))
                continue
            n = int(r.get("attempts") or 0) + 1
            self._sf_unblock_merge(journal, rec, f"R66 auto retry {n}")
            self.blockers.update(r["id"], attempts=n, last_try=self.clock().isoformat())

    def _sf_unblock_merge(self, journal, rec: dict, note: str) -> None:
        qid = (rec.get("blocked") or {}).get("qid")
        if not qid:
            return
        journal.unblock(qid, note)
        qs = self._read("questions.json", {})
        if qid in qs and qs[qid].get("status") == "open":
            qs[qid].update(status="answered", answer=note, closed_at=self.clock().isoformat(),
                           closed_seq=self._read("q_seq.json", {}).get("n", 0))
            self._write("questions.json", _bs()._prune_questions(qs))

    # ---------------------------------------------------------------- capabilities (R66f)
    def _sf_cap_job(self, name: str, d: dict) -> None:
        rec = self._sf_open("capability", name, f"{name} is not usable ({d.get('condition')}); the troubleshooter "
                                                "is fixing it")
        self.blockers.attempt(rec["id"])

    def _sf_cap_ready(self, name: str, entry, d: dict) -> None:
        cond = str(d.get("condition") or "error")
        fix = str(d.get("fix") or "")
        rec = self._sf_open("capability", name, f"{name} is not usable ({cond})")
        detail = entry.get("detail") if isinstance(entry, dict) else None
        detail = "" if not detail or blockers_mod.has_secret(detail) else f" Detail: {str(detail)[:300]}."
        then = f" Then: {d['then']}." if d.get("then") and not blockers_mod.has_secret(d.get("then")) else ""
        expect = f"The status page shows {name} as OK; Forge re-checks it itself and emails you that it's fixed."
        if fix.startswith("PowerShell: ") or fix.startswith("Win + R: "):
            where, paste = ("PowerShell", fix[len("PowerShell: "):]) if fix.startswith("PowerShell: ") else \
                ("Win + R", fix[len("Win + R: "):])
            cat = "credentials" if cond in ("no_password", "auth_rejected", "logged_out") or \
                re.search(r"\blogin\b|\bauth\b|keyring set", paste, re.I) or cond.startswith("auth") else \
                "admin" if "winget install" in paste.lower() else "exhausted"
            why = {"credentials": f"{name} needs a password or key that only you can create or enter.",
                   "admin": f"{name} needs an install that asks for your administrator approval (UAC).",
                   "exhausted": f"Forge's troubleshooter couldn't make {name} work within its rounds."}[cat]
            kit = {"why": f"{why} Forge's readiness check says {name} is not usable ({cond}).{detail}{then}",
                   "category": cat, "where": where, "paste": paste, "expect": expect}
            if cat == "exhausted" and where == "PowerShell":
                kit["script"] = paste
        else:
            kit = self._sf_retry_kit(rec, f"Forge has no automatic check for {name} ({cond}). If the work doesn't "
                                          "really need it, this line tells Forge so and the tasks go on without it.",
                                     f"Forge stops waiting for {name}; you get an email when it's settled.",
                                     not_needed=True)
        self._sf_ready(rec, kit)

    def _sf_caps_usable(self, usable) -> None:
        if not self._self_fix:
            return
        for r in self.blockers.active():
            if r.get("kind") == "capability" and r.get("key") in usable:
                self._sf_fixed(r)

    # ---------------------------------------------------------------- Ben's retry (R66e)
    def _sf_take_retry(self, a: dict) -> bool:
        """True when the dropped answer was for a blocker (handled or refused); False for an ordinary question."""
        if not self._self_fix:
            return False
        rec = self.blockers.get(str(a.get("qid")))
        if rec is None:
            return False
        if rec.get("status") != "ready_for_ben" or not secrets.compare_digest(str(rec.get("code", "")),
                                                                              str(a.get("code", ""))):
            self._log(f"ignored a retry for blocker {rec.get('id')} (not waiting for Ben, or a wrong code)")
            return True
        if rec.get("category") == "unpark":
            self._log(f"ignored a retry for blocker {rec.get('id')}: unparking needs Ben's email")
            return True
        not_needed = _bs().clean_reply(str(a.get("answer", ""))).strip().lower().startswith("not needed")
        kind, key = rec.get("kind"), str(rec.get("key"))
        if kind == "blocked":
            self._sf_reopen(key, "Ben asked for a retry (R66e): a fresh set of attempts.", 0)
        elif kind in ("replan", "drift"):
            if kind == "replan" and getattr(self, "manager", None) is not None:
                d = drift_mod.load(self.state) or drift_mod.adopt([], [], False, self._activity().total())
                if not d.get("replan"):
                    drift_mod.new_replan(d, [f"Ben asked for a retry of: {rec.get('summary')}"], "retry by Ben")
                    drift_mod.save(self.state, d)
            else:
                q = self._queue()
                q["drift_due"], q["drift_failures"] = True, 0
                self._save_queue(q)
        elif kind == "merge":
            journal = _bs().Journal(self.state)
            jr = journal.load(key)
            if jr is not None and jr.get("status") == "blocked":
                self._sf_unblock_merge(journal, jr, "Ben asked for a retry (R66e)")
        elif kind == "sync":
            st = self._read("main_sync.json", {})
            for k in ("resolve", "conflict", "fetched_at"):
                st.pop(k, None)
            self._write("main_sync.json", st)
        elif kind == "gate":
            qs = self._read("questions.json", {})
            for qid, q in qs.items():
                if q.get("kind") == "gate" and q.get("auto") and q.get("status") == "open" and str(q.get("pr")) == key:
                    q["merge_set"] = False
            self._write("questions.json", qs)
            self._write("gate_watch.json", {})  # the watch retries at once
        elif kind == "capability":
            if not_needed:
                qs = self._read("questions.json", {})
                dropped = False
                for qid, q in qs.items():
                    if q.get("kind") == "capability" and q.get("status") == "open" and q.get("capability") == key \
                            and q.get("condition") == "no_check":  # only a need Forge can't check may be dropped
                        if self._answer_capability(qs, q, "not needed", "not needed"):
                            q.update(status="answered", answer="not needed (R66e)",
                                     closed_at=self.clock().isoformat(),
                                     closed_seq=self._read("q_seq.json", {}).get("n", 0))
                            dropped = True
                self._write("questions.json", _bs()._prune_questions(qs))
                if dropped:
                    self._sf_fixed(self.blockers.get(rec["id"]))
                else:
                    self._log(f"refused 'not needed' for {key}: only a capability with no check can be dropped")
                return True
            routing = self._read("cap_routing.json", {})
            if isinstance(routing, dict) and key in routing:
                del routing[key]
                self._write("cap_routing.json", routing)
            self._sf_force_check(key)
        self.blockers.back_to_fixing(rec["id"])
        self._log(f"R66: Ben's retry for blocker {rec['id']} ({kind} {key})")
        return True

    # ---------------------------------------------------------------- sync conflicts (R66f)
    def _sf_sync_conflict(self, layer: str, wt: Path, main: str, head: str, files: list[str], st: dict) -> str:
        bs = _bs()
        first = self.blockers.find("sync", "sync") is None
        rec = self._sf_open("sync", "sync", f"{layer} can't take the latest main (merge conflict in "
                                            f"{', '.join(files)[:150]})")
        res = st.get("resolve") if isinstance(st.get("resolve"), dict) else {}
        if res.get("main") != main or res.get("layer") != head:
            res = {"main": main, "layer": head, "tries": 0}
        try:
            limit = int(self.limits.get("sync_resolve_tries", 2))
        except (TypeError, ValueError):
            limit = 2
        st["conflict"] = {"main": main, "layer": head, "qid": None, "files": files}
        if first:  # the blocker is opened now; the troubleshooter tries at the next sync (MAIN_SYNC_EVERY_S)
            st["resolve"] = res
            self._write("main_sync.json", st)
            return "conflict"
        if int(res.get("tries", 0)) >= limit:
            st["resolve"] = res
            self._write("main_sync.json", st)
            self._sf_sync_exhausted(rec, layer, files, limit)
            return "conflict"
        res["tries"] = int(res.get("tries", 0)) + 1
        st["resolve"] = res
        self._write("main_sync.json", st)  # the try counts before anything runs (a crash can't loop)
        self.blockers.attempt(rec["id"])
        try:
            problem = self._sf_resolve_merge(layer, wt, main, head)
        except (bs.Capped, bs.NotReady):  # a cap or stop is not a failed try
            st = self._read("main_sync.json", {})
            (st.get("resolve") or {})["tries"] = max(0, int((st.get("resolve") or {}).get("tries", 1)) - 1)
            self._write("main_sync.json", st)
            return "conflict"
        except bs.Tampered:
            return "tampered"
        if problem:
            self._log(f"main sync: troubleshooter resolution rejected: {problem}"[:500])
            if int(res["tries"]) >= limit:
                self._sf_sync_exhausted(rec, layer, files, limit)
            return "conflict"
        st = self._read("main_sync.json", {})
        for k in ("resolve", "conflict"):
            st.pop(k, None)
        self._write("main_sync.json", st)
        self._sf_fix_kind("sync")
        if self.push:
            self._push()
        return "synced"

    def _sf_sync_exhausted(self, rec: dict, layer: str, files: list[str], limit: int) -> None:
        self._sf_ready(rec, self._sf_retry_kit(
            rec, f"Merging the latest main into {layer} conflicts in {', '.join(files)[:300]}, and Forge's "
                 f"troubleshooter couldn't resolve it in {limit} tries. {layer} keeps building on its old base. "
                 "Running this line gives Forge a fresh set of tries (for example after main moved).",
            f"{layer} takes the latest main, and you get an email when it's fixed."))

    def _sf_resolve_merge(self, layer: str, wt: Path, main: str, head: str) -> str | None:
        """One troubleshooter try at the conflicted sync merge, in a scratch worktree. None when the merge was
        resolved, judged, reviewed, registered and moved onto the layer; else the reason it was rejected."""
        bs = _bs()
        git = bs._git

        def run(cwd, *args):
            return subprocess.run(["git", "-c", "user.name=Forge", "-c", "user.email=forge@localhost", *args],
                                  cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, **bs.NOWIN)

        with self.trees.throwaway(head) as tw:
            p = run(tw, "merge", "--no-ff", "--no-edit", "-m", f"Sync {layer} with main", main)
            files = [f for f in git(tw, "diff", "--name-only", "--diff-filter=U", check=False).split() if f]
            if p.returncode != 0 and not files:
                return f"merge failed without conflicts: {(p.stdout + p.stderr).strip()[:300]}"
            index_before = {ln.split("\t", 1)[1]: ln for ln in git(tw, "ls-files", "-s").splitlines()
                            if "\t" in ln and ln.split("\t", 1)[1] not in files}
            if files:
                prompt = (bs.role_text(self.repo, "troubleshooter") + "\n\nMERGE CONFLICT JOB (R66f). Forge is merging "
                          f"main ({main[:12]}) into the layer {layer} ({head[:12]}) in this folder, and git stopped "
                          "on conflicts in these files:\n" + "\n".join(f"- {f}" for f in files) +
                          "\n\nResolve the conflicts: edit ONLY these files, keep the intent of both sides, and remove "
                          "every conflict marker. Do not stage, commit or touch any other file; Forge's plain code "
                          "checks and commits the result, then the judges and the reviewer check it.\n"
                          "Answer with JSON: {\"kind\": \"fix\" | \"dead_end\", \"notes\": \"...\", \"alternative\": \"...\"}")
                r = self._call("troubleshooter", prompt, bs.S_TROUBLE, cwd=tw, scratch=True)
                if not r.ok:
                    return f"troubleshooter failed: {r.error}"
                if git(tw, "rev-parse", "HEAD") != head:
                    return "the troubleshooter committed"
                index_after = {ln.split("\t", 1)[1]: ln for ln in git(tw, "ls-files", "-s").splitlines()
                               if "\t" in ln and ln.split("\t", 1)[1] not in files}
                if index_after != index_before:
                    return "the troubleshooter changed staged files outside the conflict"
                extra = [f for f in git(tw, "diff", "--name-only", check=False).split() if f and f not in files]
                if extra:
                    return "the troubleshooter changed files outside the conflict: " + ", ".join(extra[:10])
                untracked = git(tw, "ls-files", "--others", "--exclude-standard", check=False).split()
                if untracked:
                    return "the troubleshooter added files: " + ", ".join(untracked[:10])
                for f in files:
                    try:
                        text = (tw / f).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        text = ""
                    if MARKER_RE.search(text):
                        return f"conflict markers left in {f}"
                if run(tw, "add", "--", *files).returncode != 0:
                    return "git add failed"
                if git(tw, "diff", "--name-only", "--diff-filter=U", check=False).strip():
                    return "files are still unmerged"
                c = run(tw, "commit", "--no-edit")
                if c.returncode != 0:
                    return f"commit failed: {(c.stdout + c.stderr).strip()[:300]}"
            new = git(tw, "rev-parse", "HEAD")
            if git(tw, "rev-list", "--parents", "-n", "1", new).split()[1:] != [head, main]:
                return "the result is not a merge of the layer and main"
            tasks = self._queue().get("tasks", [])
            exclude = sorted({f for t in tasks if t.get("status") != "done" for f in (t.get("test_files") or [])})
            for cmd in bs.task_judge_cmds(self.judge_cmds, head, new, exclude=exclude):
                code, out = self._run_cmd(cmd, cwd=tw)
                if code != 0:
                    return f"{cmd} failed (exit {code}): " + "\n".join(out.splitlines()[-10:])
            diff = git(tw, "diff", head, new, "--", *files, check=False)[:30000] if files else ""
            rv = self._call("reviewer", bs.role_text(self.repo, "reviewer") + "\n\nReview this MERGE CONFLICT "
                            f"RESOLUTION (R66f): main ({main[:12]}) merged into {layer} ({head[:12]}). The diff below "
                            "is the change to the layer in the conflicted files. Pass it only if both sides' intent "
                            "is kept and nothing unrelated changed.\n\n" + diff +
                            "\nAnswer with JSON: {\"verdict\": \"pass\" | \"fail\", \"reasons\": [...]}", bs.S_REVIEW)
            if not rv.ok or (rv.data or {}).get("verdict") != "pass":
                return "review failed: " + "; ".join(str(x) for x in ((rv.data or {}).get("reasons") or [rv.error]))
            if git(wt, "rev-parse", "HEAD") != head:
                return "the layer moved meanwhile"
            approved = bs.ApprovedMerges(self.state)
            for m in [x for x in git(tw, "rev-list", "--merges", main, f"^{head}").split() if x]:
                if not approved.has(m):
                    approved.add(m, {"kind": "main", "via": new})
            approved.add(new, {"kind": "main_sync", "base": head, "other": main, "parents": [head, main],
                               "resolved_by": "troubleshooter"})
            ff = run(wt, "merge", "--ff-only", new)
            if ff.returncode != 0:
                return f"fast-forward failed: {(ff.stdout + ff.stderr).strip()[:300]}"
        self._log(f"main sync: troubleshooter resolved origin/main {main[:12]} into {layer} ({head[:12]} -> {new[:12]})")
        return None
