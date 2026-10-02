# Phase 2A: the Auditor (implementation plan)

Source of truth: `docs/specs/phase-2-design.md`, sections 2 and 3 (proposed D-039). This plan covers only P2A.
The Challenger (P2B) and scores and gate drills (P2C) are planned separately. Scores are **not** computed here;
P2A only leaves the ledger evidence P2C will count (`audit` events of kinds `report`, `invalid`, `confirmed`,
`unconfirmed`).

## Ground rules for every task

- Test first: each task's tests are new files under `tests/core/`, written by the test writer before the build.
  `test_cmd` is always `python -m unittest <that file>` (R1).
- Every existing test in `tests/core/` and every drill in `drills/run_drills.py` must keep passing. In particular:
  `Team` keeps its six dataclass fields (tests build it with six positional or keyword members, and
  `test_live_run` asserts `vars(team)` has exactly six roles), `core.roles.ROLE_NAMES` and `core.roles.DEFAULTS`
  stay exactly as they are (`test_roles` asserts them), and a team **without** an auditor behaves exactly as today
  (no audit is ever queued, smoke runs six roles, the gate opens as before).
- The auditor is Codex, read-only, a fresh session per run (D-020). It never falls back to Claude: while Codex is
  capped, held or lacks usable evidence, the audit waits and costs nothing (R37, R54 style).
- Ledger events for audits are applied by identity `forge-auditor` (role `auditor`, already in `ROLES`).
- Line endings: write files as LF.

## Resolved design points (supervisor, 2026-10-02) and where each is owned

1. Partial findings survive: severity is a plain string in `S_AUDIT`; `validate_findings` enforces the enum. The
   end-to-end path (agent answer in, ledger report out) is tested in T2Ac. The report carries `dropped`.
   Owners: T2Ab (schema, `validate_findings`), T2Ac (end to end), T2Aa (ledger accepts `dropped`).
2. `line` and `contract_ref` are nullable and may be omitted. Owner: T2Ab.
3. Waiting is persisted in the `audit_due` entry, one distinct `waiting_on` value per condition. Owner: T2Ac.
4. The auditor prompt carries the plan file (when one exists) and the judges' evidence including mutation score
   and survivors. Owner: T2Ac.
5. Invalid read-only runs leave ledger `audit` events of kind `invalid`; retried once, then dropped. Owner: T2Ac
   (ledger shape in T2Aa).
6. Waiting bookkeeping runs in `step()` before the `_capped()` early return. Owner: T2Ac.
7. An audit launches only after a finished finalization (ledger pass exists and the merge journal record is
   `finished`). Owner: T2Ac.

## Task order

T2Aa (ledger) and T2Ab (schema, role, team, smoke) are independent. T2Ac needs both. T2Ad needs T2Ac. T2Ae needs
T2Ad. Each task lists `depends_on` (R55).

---

## T2Aa: the ledger action `audit`

- files_in_scope: `core/ledger.py`
- test_files: `tests/core/test_audit_ledger.py`
- test_cmd: `python -m unittest tests/core/test_audit_ledger.py`
- covers: 3.6

### Section

Add a new ledger action `audit` to `core/ledger.py` (Phase 2 design 3.6, proposed D-039). Nothing else in Forge
changes in this task; later tasks (T2Ac, T2Ad) apply these events.

Interface: add to `ACTIONS` the entry `"audit": ({"auditor"}, {"done"}, None)`: only role `auditor` may apply it,
only to a contract whose status is `done`, and it never changes the status (no transition). Add a module constant
`AUDIT_SEVERITIES = ("blocker", "major", "minor")`, `AUDIT_FINDINGS_MAX = 30` and `AUDIT_TEXT_MAX = 2000` (the R19
caps). In `Ledger.apply`, inside the generic branch (after the from-status check), validate the payload of an
`audit` proposal and raise `Rejected` with a clear message on any problem:

- `payload["kind"]` must be one of `report`, `invalid`, `confirmed`, `unconfirmed`; anything else is rejected.
- `report`: `run_id` a non-empty str; `commit` a 40-character lowercase hex sha; `verdict` `"clean"` or
  `"findings"`; `findings` a list of at most 30 objects, each with `id` (non-empty str), `severity` in
  `AUDIT_SEVERITIES`, `file`, `summary`, `evidence` (non-empty str each); optional `line` (int, not bool, or null)
  and `contract_ref` (str or null); `verdict` must be `"findings"` exactly when `findings` is non-empty and
  `"clean"` exactly when it is empty; `dropped` an int >= 0 (not a bool), required.
- `invalid`: `run_id` a non-empty str and `reason` a non-empty str.
- `confirmed` and `unconfirmed`: `of` a non-empty str (the audited task id) and `finding` a non-empty str (the
  finding id); optional `task` (str, the fix task id).
- Every string anywhere in an `audit` payload is at most 2000 characters; a longer one is rejected (the
  conductor caps before applying).

An applied `audit` event leaves the contract unchanged (status `done`, attempts, tokens, commit), records
`result_status: "done"`, is part of the hash chain, and replays: `derive()`, `rebuild()` and `reconcile()` keep
working on a log that contains audit events, and `completion(cid)` still returns the `pass` event. Audit events
never write `note: "false_claim"` and never change `false_claims()`. Duplicate proposal ids stay idempotent
(`{"status": "duplicate"}`). Add one bullet to the module docstring: an audit never undoes `done`; the fix is new
work.

Acceptance criteria the tests must check (build a ledger in a temporary folder with a `roles.json` that maps
`forge-auditor` to `auditor`, and bring one contract to `done` through create, claim, submit, test_run,
run_report, pass, as the existing ledger tests do):

1. A valid `report` (clean, empty findings, dropped 0) and a valid `report` with one finding and `dropped: 2` are
   applied by `forge-auditor`; the contract is still `done` with the same attempts and commit; the event's
   `payload` holds `dropped`.
2. `invalid`, `confirmed` and `unconfirmed` payloads with the fields above are applied.
3. Rejected: role `executor`, `core` or `manager` applying `audit`; an `audit` on a contract that is `open`,
   `claimed`, `submitted` or `failed`; an unknown contract; an unknown `kind`; a `report` whose finding has
   severity `"critical"`; a short or upper-case `commit`; `verdict` `"clean"` with findings; `verdict` `"findings"`
   with no findings; missing or negative or boolean `dropped`; 31 findings; a 2001-character summary; an
   `invalid` with an empty reason. After every rejection `snapshot()` is unchanged.
4. After audit events, `verify_chain()` is true, `reconcile()` returns False (cache matches), `rebuild()`
   reproduces the same contracts, `completion(cid)` is the pass event and `false_claims(cid)` is 0.
5. Re-applying the same proposal id returns status `duplicate` and adds no event.

---

## T2Ab: S_AUDIT, findings validation, the auditor role, team member and smoke

- files_in_scope: `core/audit.py` (new), `core/roles.py`, `agents/auditor.md` (new), `core/bootstrap.py`
- test_files: `tests/core/test_audit_role.py`
- test_cmd: `python -m unittest tests/core/test_audit_role.py`
- covers: 2.1

### Section

Create the Auditor's schema, the plain-code findings validator, its role text and its team member (Phase 2 design
2 and 3.5). No audit is scheduled yet (that is T2Ac).

1. New module `core/audit.py` (plain code, no AI, standard library only). Define
   `SEVERITIES = ("blocker", "major", "minor")`, `FINDINGS_MAX = 30`, `TEXT_MAX = 2000` and the agent schema
   `S_AUDIT = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["clean", "findings"]},
   "findings": {"type": "array", "items": FINDING}}, "required": ["verdict", "findings"]}` where
   `FINDING = {"type": "object", "properties": {"id": {"type": "string"}, "severity": {"type": "string"},
   "file": {"type": "string"}, "line": {"type": ["integer", "null"]}, "summary": {"type": "string"},
   "evidence": {"type": "string"}, "contract_ref": {"type": ["string", "null"]}},
   "required": ["id", "severity", "file", "summary", "evidence"]}`. Severity is a plain string with **no enum**,
   so one finding with an unknown severity never fails the whole answer; `line` and `contract_ref` are
   explicitly nullable and optional. `core.agents.strict_schema(S_AUDIT)` (what Codex receives) makes every
   finding property required, so Codex sends null for them.
2. `validate_findings(findings, exists) -> tuple[list[dict], int]` in `core/audit.py`: `findings` is the agent's
   list, `exists(path) -> bool` says whether a repo-relative file exists at the audited commit. Returns
   `(kept, dropped)`. A finding is dropped (counted once in `dropped`) when it is not a dict; its `severity` is
   not in `SEVERITIES`; its `file` is not a non-empty str, is absolute, contains `..`, or `exists()` is false
   (the path is normalised first: backslashes to `/`, a leading `./` removed); its `summary` or `evidence` is not
   a str or is empty after strip. Kept findings are new dicts with exactly `id` (str, at most 40 chars; an empty
   or missing id becomes `f"f{position}"`, 1-based), `severity`, `file` (normalised), `line` (int, not bool,
   else None), `summary`, `evidence`, `contract_ref` (str or None), every str cut to `TEXT_MAX`. At most
   `FINDINGS_MAX` are kept; any valid finding beyond that is counted in `dropped`. A non-list `findings` returns
   `([], 0)`. Also `verdict_for(kept) -> str`: `"findings"` if `kept` else `"clean"`.
3. Role text: in `core/roles.py` add `EXTRA_ROLES = ("auditor",)` and `EXTRA_DEFAULTS = {"auditor": ...}` (a short
   default: "You are the AUDITOR (read-only). You change nothing. ... Answer with JSON matching the schema given
   in the prompt."). `role_text(repo, "auditor")` reads `agents/auditor.md` exactly like the other roles and falls
   back to `EXTRA_DEFAULTS["auditor"]`. Leave `ROLE_NAMES` and `DEFAULTS` unchanged. Create `agents/auditor.md`:
   the auditor is read-only and changes nothing; it audits one merged task against its ledger contract, its
   section, its plan and the design; it reports only real defects with concrete evidence (file, line, why the
   contract or design is not met); severity is `blocker`, `major` or `minor`; it must not report style
   preferences; `verdict` is `clean` when it finds nothing; answer with JSON matching the schema.
4. Team member: in `core/bootstrap.py` give `Team` a plain class attribute `auditor = None` with **no type
   annotation**, so it is not a dataclass field (the six-field constructor and `vars(team)` stay as they are).
   `real_team(limits)` sets `team.auditor = CodexAgent(t, sandbox="read-only")` on the team it builds. Import
   `S_AUDIT` from `core.audit` into `core/bootstrap.py`.
5. Smoke (R23/R29): add `"auditor": (S_AUDIT, False, '{"verdict": "clean", "findings": []}')` to `SMOKE_ROLES`
   (a read-only role). In `smoke()`, skip (no call, no problem) any role whose member
   `getattr(team, role, None)` is None, so a team without an auditor smokes its six roles exactly as today.

Acceptance criteria the tests must check:

1. `schema_ok(answer, S_AUDIT)` is True for a findings answer whose finding omits `line` and `contract_ref`;
   `schema_ok(answer, strict_schema(S_AUDIT))` is True for the same finding with `line: None` and
   `contract_ref: None` present (the omitted case is NOT tested against the strict schema); a finding with
   severity `"critical"` passes `schema_ok(..., S_AUDIT)` and the severity property has no `enum`; an answer
   without `verdict` fails `schema_ok(..., S_AUDIT)`.
2. `validate_findings` with `exists` true only for `core/x.py`: keeps a valid major finding; drops (and counts)
   one with severity `"critical"`, one for `missing.py`, one with an empty summary, one with blank evidence, one
   with `../x.py`, one non-dict; keeps the valid ones in the same list; `dropped` equals the number dropped; a
   list of only invalid findings gives `([], n)` and `verdict_for([])` is `"clean"`; 35 valid findings keep 30
   and count 5; `core\\x.py` is kept as `core/x.py`; a 3000-char summary is cut to 2000; a missing line becomes
   None.
3. `role_text(repo, "auditor")` returns the text of `agents/auditor.md` when present and `EXTRA_DEFAULTS["auditor"]`
   when the file is missing; `role_text(repo, "nope")` still raises ValueError; `ROLE_NAMES` is unchanged.
4. `real_team({})` has `auditor` that is a `CodexAgent` with `sandbox == "read-only"` and provider `codex`;
   `Team(...)` built from six members has `auditor is None` and `"auditor" not in vars(team)`.
5. `SMOKE_ROLES["auditor"]` is `(S_AUDIT, False, ...)`. `smoke()` with a fake team that has a fake auditor
   answering `{"verdict": "clean", "findings": []}` returns `[]` and calls the auditor once in its own fresh repo;
   an auditor that writes a file gives a problem naming `auditor`; an auditor that answers `{}` gives a problem
   naming `auditor`; a six-member team without an auditor makes exactly six calls and returns `[]`.

---

## T2Ac: the audit trigger, scheduling and the audit run

- files_in_scope: `core/bootstrap.py`, `core/audit.py`
- test_files: `tests/core/test_audit_run.py`
- test_cmd: `python -m unittest tests/core/test_audit_run.py`
- depends_on: T2Aa, T2Ab
- covers: 3.1, 3.2, 3.3, 3.4, 3.5

### Section

Builds on T2Aa (ledger action `audit`) and T2Ab (`core/audit.py`: `S_AUDIT`, `validate_findings`, `verdict_for`;
`Team.auditor`; `role_text(repo, "auditor")`). Implements Phase 2 design 3.1 to 3.5 in `core/bootstrap.py`, with
file helpers in `core/audit.py`. Fix tasks are T2Ad: here a report is recorded and the entry finished.

**The audit_due file.** `state/audit_due.json` is a JSON list of entries
`{"tid", "cid", "base", "task_commit", "final_sha", "depth", "waiting_on": None, "invalid_runs": 0}`, oldest
first, always written with `self._write("audit_due.json", entries, durable=True)`. Add `Conductor._audit_due()`
(read; anything not a list counts as `[]`) and `_save_audit_due(entries)`.

**Trigger (3.1, point 7).** In `_hooks().mark_drift(tid)`, only when `getattr(self.team, "auditor", None)` is not
None, first append an entry for `tid` (unless one for `tid` already exists, or the ledger already holds an
`audit` event of kind `report` for that `cid` and `final_sha`), then do the existing drift write. Values come from
`Journal(self.state).load(tid)`: `cid`, `task_commit = rec["task_sha"]`, `final_sha = rec["final_sha"]`; `base` is
the parent of the task's `tests_commit` (`git rev-parse <tests_commit>^` in the layer worktree), falling back to
`rec["base"]` when there is no `tests_commit` or it cannot be resolved; `depth` is the task's `audit_depth`
(default 0). This entry is durable trigger intent only. An entry is **launchable** only when both hold:
`self._ledger().completion(cid)` is not None AND the journal record for `tid` has `status == "finished"`. A
blocked or still-active finalization keeps its entry and launches nothing. Without an auditor nothing is written.

**Waiting bookkeeping before the early return (3.2, points 3 and 6).** Add `_audit_waits()` and call it in
`step()` immediately before `if self._capped(): return "capped"`. For the oldest launchable entry it sets
`waiting_on` to `"codex_held"` when `self.meter.held(provider)` is not None, else `"codex_capped"` when
`self.meter.over(provider, self.limits)`, else `"codex_not_ready"` when `self.ready_for("auditor")` is non-empty,
else None (provider is the auditor's `provider`, normally `codex`). It writes only when the value changed, never
launches anything, and never touches `invalid_runs` or any task counter. It does nothing without an auditor.

**Scheduling (3.2).** Add `_audit_step(q, cap_map) -> str | None` and call it in `step()` after the drift check and
the active-finalization block, before the pending re-plan and new task stages. With no launchable entry it
returns None. If `ready_for("auditor", cap_map=cap_map)` is non-empty it records `waiting_on` `"codex_not_ready"`,
sets `self._waiting = True` and returns None (other work goes on). Otherwise it runs one audit of the oldest
launchable entry and `step()` returns `"worked"`. In `step()`, `Capped` returns `self._held()`, `NotReady` records
`"codex_not_ready"` and returns `"not_ready"`, `Tampered` returns `"killed"`, `RuntimeError`/`OSError`/`Rejected`
are logged and return `"error"`. A capped, held, stopped or unready audit leaves the entry in place with
`invalid_runs` unchanged and writes no ledger event. If the ledger already has a `report` for this `cid` and
`final_sha`, nothing is launched: the entry is finished (removed) from that event, so a crash never audits twice.

**What the auditor sees (3.3).** The prompt is `role_text(self.repo, "auditor")` plus: `LEDGER CONTRACT:` the
contract's `title`, `spec_ref`, `acceptance`, `files_in_scope`; `TASK SECTION:` the task's `section`;
`PLAN FILE (<path>):` the text of the task's `plan_file` read at `final_sha`, only when the task has a `plan_file`
and it exists there; `DESIGN (<path>):` `self._spec_rel()` read at `final_sha` (cap 40000 chars);
`MERGED DIFF <base>..<task_commit>:` `git diff base..task_commit` (tests included, cap 60000 chars); and
`JUDGES' EVIDENCE:` from the ledger `pass` event payload: the CI `run_id` and whether that test run passed, and the
`mutation` evidence: `score`, `total`, `killed`, `reason` and every survivor (`id`, `file`, `line`, `original`,
`replacement`), or the text "survivors: none". The prompt never contains the Reviewer's verdict or reasons.
`_plan_stage` additionally records `plan_file` on each build task it creates.

**Where it runs (3.4).** `with self.trees.throwaway(final_sha) as tw:` snapshot `HEAD` and a file list with
content hashes (excluding `.git`), call `self._call("auditor", prompt, S_AUDIT, cwd=tw)`, then check `HEAD` is
still `final_sha`, the snapshot is unchanged and `self.last_agent_commit` is None. Any change makes the run
invalid. An unusable answer (`not r.ok`, `verdict` not `clean`/`findings`, `findings` not a list) is invalid too.
For an invalid run the conductor applies ledger `audit` `{"kind": "invalid", "run_id", "reason"}` (reason capped
2000) by `forge-auditor`, logs it to `state/audits.jsonl`, increments `invalid_runs`; at 2 the entry is removed
without a report. The audit `run_id` is generated by the conductor (`f"audit-{tid}-{uuid8}"`). R9 tamper rules
still apply through `_call`.

**Validation and report (3.5, point 1).** The conductor never runs `schema_ok` on the whole answer. It calls
`validate_findings(data["findings"], exists)` where `exists` checks the file is in the worktree at `final_sha`,
and applies ledger `audit` `{"kind": "report", "run_id", "commit": final_sha, "verdict": verdict_for(kept),
"findings": kept, "dropped": dropped}` with proposal id `f"{cid}-audit-{final_sha[:12]}"`, appends a line to
`state/audits.jsonl`, then removes the entry (`_finish_audit(entry, payload)`, which T2Ad extends).

Acceptance criteria (fake agents, a fake Codex auditor set as `team.auditor`, a real git repo, as in
`test_merge_pipeline.py`):

1. Trigger: after a task finalizes, `audit_due.json` has one entry with `tid`, `cid`, `base` (parent of
   `tests_commit`), `task_commit`, `final_sha`; a team without an auditor writes no file; a second `mark_drift`
   adds no duplicate.
2. Point 7: with the journal record active or blocked (ledger refused the pass) the entry stays and the auditor
   is never called; after the finalization completes, the next `step()` audits exactly once and a further
   `step()` does not audit again.
3. Points 3/6, one test per condition, each driving unmodified `step()`: codex held (`meter.hold`) gives
   `waiting_on == "codex_held"`; codex over its daily cap gives `"codex_capped"` (step returns `"capped"`); codex
   lacking usable evidence (failing codex probe) gives `"codex_not_ready"`. In each: the auditor is not called,
   `invalid_runs` stays 0, no ledger audit event; once the condition clears, `step()` audits and returns
   `"worked"`.
4. Point 4: the prompt holds the contract title and acceptance, the section, the plan file text (when the task
   has a `plan_file`), the design text, the diff including the test file, the mutation score and a survivor id,
   and not the Reviewer's reasons text.
5. Point 5: an auditor that writes a file (or moves HEAD) yields a ledger `invalid` event and is retried on the
   next step; a second invalid run adds another `invalid` event and removes the entry with no `report`.
6. Point 1 end to end: an answer with one valid major finding, one finding with severity `"critical"` and one for a
   missing file yields a ledger `report` with exactly the valid finding and `dropped: 2`; an answer whose findings
   are all invalid yields verdict `clean`, `findings: []`, `dropped` = their count; `line`/`contract_ref` null are
   accepted. The entry is removed afterwards.

---

## T2Ad: fix tasks, confirmation and `dismissed`

- files_in_scope: `core/bootstrap.py`, `core/audit.py`
- test_files: `tests/core/test_audit_fix_tasks.py`
- test_cmd: `python -m unittest tests/core/test_audit_fix_tasks.py`
- depends_on: T2Ac
- covers: 3.7, 3.8, 3.9, 3.10

### Section

Builds on T2Ac (`_audit_step`, `_finish_audit(entry, payload)` called after a `report` is in the ledger, entries
with `depth`) and T2Aa (`audit` kinds `confirmed`, `unconfirmed`). Implements Phase 2 design 3.7 to 3.10.

**Fix tasks (3.7).** Extend `_finish_audit` so that, before the entry is removed, every kept finding of severity
`blocker` or `major` (in report order, numbered n = 1, 2, ... among those findings) becomes a new build task
appended at the end of the queue, unless a task with that id already exists (idempotent: a crash after the
report re-runs `_finish_audit` from the ledger's report event and creates only what is missing). Helpers in
`core/audit.py`: `fix_id(tid, n)` gives `f"{tid[:40 - len(suffix)]}{suffix}"` with `suffix = f"-F{n}"` so it
always matches the R1 id rule `^[A-Za-z0-9_-]{1,40}$`; `fix_test_file(tid, n)` gives
`f"tests/core/test_audit_{slug}_f{n}.py"` where `slug` is `tid` lower-cased with every character outside
`[a-z0-9_]` replaced by `_`; `fix_task(orig, finding, n) -> dict` builds: `id`; `title`
`f"Fix (audit of {tid}): {summary}"` (cut to 200 chars); `section` containing the finding (summary, evidence,
file, line, contract_ref), the original task's `section`, and the sentence "The test writer must write a test that
reproduces this finding; it must fail on the merged code."; `files_in_scope` the original's plus the finding's
`file` when it does not start with `tests/`; `test_files` `[fix_test_file(tid, n)]`; `test_cmd`
`f"python -m unittest {that file}"`; `needs` the original's; `audit_of` `{"tid", "cid", "finding_id"}`;
`audit_depth` the audited entry's `depth` + 1. The task goes through `Conductor._new_task` (kind `build`,
status `todo`) and must pass `validate_task`.

**Depth limit (3.10).** Fix tasks are audited like any merge (T2Ac's trigger carries their `audit_depth`). When
the audited entry's `depth` is 2 or more, blocker and major findings create no task: each goes to Ben as a
question `self._ask("audit", subject, body, hold=True, digest=True, default="Forge leaves it as is.", task=tid)`
whose body holds the finding.

**Minor findings (3.9).** Each kept `minor` finding is added to the original task's `notes` (each note capped at
2000 chars, the list kept to its last 30: R19), and all minor findings of one report go into one digest item:
`self._ask("audit", f"Minor audit findings on {tid}", body, hold=True, digest=True,
default="Forge leaves it as is.", task=tid)`. Minor findings create no task.

**Confirmed or unconfirmed (3.8).** In `_tests_stage`, for a task with `audit_of`: when its tests are accepted
(the normal R4 weak-test check and the empty-implementation check pass and the tests are committed), apply
ledger `audit` `{"kind": "confirmed", "of": audit_of.tid, "finding": audit_of.finding_id, "task": fix id}` on
contract `audit_of.cid` by `forge-auditor` (proposal id `f"{fix id}-confirmed"`). When its tests are rejected for
the second time, instead of `_block` (no question to Ben) the task's status becomes `dismissed` (a new final
status) and ledger `audit` `{"kind": "unconfirmed", ...same fields}` is applied (proposal id
`f"{fix id}-unconfirmed"`). Tasks without `audit_of` behave exactly as today. A `dismissed` task is never picked
again by `_pick_tasks`.

Acceptance criteria (fake agents, a fake auditor, a real git repo):

1. A report with one blocker, one major and one minor finding on task `T9` appends exactly two tasks, `T9-F1` and
   `T9-F2`, at the end of the queue, with status `todo`, the title format above, `test_files`
   `["tests/core/test_audit_t9_f1.py"]`, `test_cmd` `python -m unittest tests/core/test_audit_t9_f1.py`,
   `files_in_scope` the original's plus the finding's non-test file, `audit_of` and `audit_depth` 1, a section
   that contains the finding summary, the original section and the reproduce rule; both pass `validate_task`.
2. A 40-character tid gives ids of at most 40 characters that match the R1 rule; a tid with `-` gives a test file
   name with `_`.
3. The minor finding is in the original task's notes and in one held `audit` question with default
   "Forge leaves it as is."; it creates no task.
4. Re-running `_finish_audit` from the same report creates no duplicate task.
5. A fix task whose test writer's tests fail on the merged code is `tests_ok` and the ledger has a `confirmed`
   event on the original contract with `of`, `finding` and `task`.
6. A fix task whose tests are rejected twice (they pass on the merged code) is `dismissed`, the ledger has an
   `unconfirmed` event, and no question of kind `blocked` was asked; `_pick_tasks` never yields it.
7. An audit of an entry with `depth` 2 that reports a major finding creates no task and asks one `audit`
   question with default "Forge leaves it as is."; an entry with `depth` 1 still creates fix tasks with
   `audit_depth` 2.

---

## T2Ae: the gate waits for audits; the drift keeper reads `spec_file`

- files_in_scope: `core/bootstrap.py`
- test_files: `tests/core/test_audit_gate.py`
- test_cmd: `python -m unittest tests/core/test_audit_gate.py`
- depends_on: T2Ad
- covers: 3.11, 3.12

### Section

Builds on T2Ac (`state/audit_due.json`, `Conductor._audit_due()`) and T2Ad (fix tasks with `audit_of`, final status
`dismissed`). Implements Phase 2 design 3.11 and 3.12 in `core/bootstrap.py`.

**Gate (3.11).** In `step()`, the layer gate condition changes from "every task is `done`" to: the queue has tasks,
every task's status is `done` or `dismissed`, `self._audit_due()` is empty, and the existing conditions still hold
(no `drift_due`, drift not busy, no open `gate` question, no active or blocked merge-journal record). So the gate
never opens while any audit is due (pending, waiting or being retried) or while any fix task is not `done` or
`dismissed`. `dismissed` counts as finished for the gate only: it never counts for coverage (coverage keeps using
tasks that are `done` in the queue and passed in the ledger), and `_pick_tasks` keeps treating only `done` as a
satisfied dependency. The gate's report lists each task with its status, so dismissed fix tasks are visible. A team
without an auditor never has audit entries, so its gate opens exactly as today.

**Drift keeper and coverage read the same design (3.12).** `_drift_check` must read the design through
`self._spec_rel()` (the lane queue's `spec_file`, else `limits["spec_file"]`, else
`docs/specs/layer-1-design.md`), exactly the file `_spec()` and the coverage map use, never a hard-coded
`layer-1-design.md`. If the current code already does this, keep it and prove it with the tests.

Acceptance criteria (fake agents; set queue and state files directly where simpler):

1. All tasks `done`, `audit_due.json` holding one entry: `step()` never returns `"gate"` and `gh` is never asked to
   create a pull request; after the entry is removed, `step()` returns `"gate"`.
2. All tasks `done` except one fix task (with `audit_of`) in `todo`, `tests_ok` or `blocked`: no gate.
3. All tasks `done` except one fix task in `dismissed`, `audit_due` empty: `step()` returns `"gate"`.
4. A `dismissed` task's `covers` are not counted as covered by the coverage map (`_coverage_block` or
   `cov_mod.compute` with `cov_mod.verified_done`).
5. A dependency on a `dismissed` task is not treated as satisfied by `_pick_tasks`.
6. With `limits["spec_file"]` naming a design file in the layer worktree that contains a unique marker, and no
   queue `spec_file`, the drift keeper's prompt contains that marker and not the text of
   `docs/specs/layer-1-design.md`; with a queue `spec_file` set, that file is used instead.
7. A team without an auditor and all tasks `done` still reaches `"gate"` as before.

---

## Not in this plan

Scores, the `false_claims` alarm, the digest and status-page sections (P2C); the Challenger (P2B); D-039 is
recorded in `docs/DECISIONS.md` by the supervisor when the layer merges.

## Reviewer notes

- T2Ac: Test that the auditor's actual cwd is a throwaway worktree at final_sha, including when the layer tip has advanced. Verify cleanup after both valid and invalid runs.
- T2Ac: Recover invalid_runs from durable invalid ledger events after a crash between event append and audit_due update, so restarting cannot exceed the one-retry limit.
- T2Ad: Handle generated ID collisions explicitly. For a 40-character parent, truncating an existing -F1 suffix can make its child's ID identical to its own. Different parent IDs and normalized test filenames can also collide. Reuse an existing task only when its audit_of matches the same finding; otherwise allocate a distinct bounded ID and test filename.
- T2Ad: Make confirmation, dismissal, minor notes and digest questions restart-safe as well as fix-task creation. Exercise crashes between ledger events and queue writes; replay must neither lose confirmation evidence nor duplicate notes or questions.
- T2Ae: Update the gate report's introductory claim that every task passed tests and review: dismissed fix tasks did neither. Describe completed builds and dismissed findings accurately while retaining each task's status.
