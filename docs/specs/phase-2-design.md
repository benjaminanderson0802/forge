# Phase 2 design: Auditor and Challenger

> Source of truth for Phase 2 (`docs/ROADMAP.md`: "Codex audits every submission read-only; blocker claims must survive a Challenger. Gate: planted bug caught; fake 'impossible' blocker overturned"). It builds on Layer 1 (`docs/specs/layer-1-design.md`, `docs/specs/bootstrap-conductor.md` R1–R57, E1–E5) and must not contradict them or `docs/DECISIONS.md`. Plan tasks: `docs/specs/phase-2-queue.json`. New decisions this design needs are listed at the end as **proposed** entries; they are not in `DECISIONS.md` yet.
>
> The numbered sections below use the coverage-map format (`core/coverage.py`): each top-level list item in a `## N.` section is requirement `N.k`.

## Why (PURPOSE trace)

PURPOSE names six failures. Layer 1 answered most of them at the moment of a merge. Phase 2 answers two of them after the fact and adversarially:

- **False claims**, "done when it wasn't": a merge passed its tests and one review, but nothing checks it again with fresh eyes against the contract and the spec. The **Auditor** does.
- **Easy-way-out stops**, "can't" or vague worries: Layer 1 checks that a blocker claim *looks* like real work. Nothing tries to *prove it wrong*. The **Challenger** does. PURPOSE's own answer is "blockers must survive a Challenger".

Both feed one **score per agent role** (D-031: "Rejected easy-outs are logged against the agent, like false claims"), so the Learner (Phase 4) gets clean data.

## 1. What Layer 1 already has (do not rebuild)

- **The build Reviewer** (`agents/reviewer.md`, Codex `read-only`, `S_REVIEW`). It runs once per build attempt, after the judges pass, on the diff since the tests commit. A `pass` is applied as the ledger action `pass` by identity `forge-auditor` (role `auditor`); a `fail` is applied as `fail`. Its reasons feed the next builder attempt (`REVIEW FEEDBACK:`). A Codex outage never costs an attempt: the core applies `withdraw` (R54).
- **False claims in the ledger.** `fail` after a run report whose `claim` was `done` writes an event with `note: "false_claim"`; `Ledger.false_claims(cid)` counts them. Nothing reads that count yet: there is no alarm and no score.
- **Blocker-claim validation** (D-031, T1B2e, `Conductor._check_blocker`). A builder answer `{"status": "blocked"}` is checked in order: (1) evidence: at least 2 distinct `tried` routes, a non-empty `error`, a valid `capability` name and a `meanwhile`; (2) the capability map: a claim that the map shows as `ok` and fresh is rejected; (3) the Reviewer judges the claim ("check the claim, not the code"). An accepted claim adds the capability to the task's `needs` (readiness gating holds the builder) and hands the task to the Troubleshooter at once.
- **The easy-out log.** Every rejected claim is written by `_record_easy_out` to `state/easy_outs.jsonl` (`task`, `agent: builder`, `kind: easy_out`, `reason`, `capability`, `summary`, `at`) and to the ledger as a `run_report` whose payload has `easy_out: {reason, capability}`. It is not counted or scored anywhere.
- **Dead ends.** A Troubleshooter answer of `kind: "dead_end"` is appended straight to `state/dead_ends.jsonl` and shown to every later Builder and Troubleshooter under `KNOWN DEAD ENDS:`. Nobody checks it. A false dead end therefore steers all later work away from a route that works.
- **Blocked tasks.** After 3 Troubleshooter rounds each followed by 2 failed attempts (R5), a task becomes `blocked` and a question (kind `blocked`) goes to Ben. This is Forge's final "can't be done" verdict, and nobody challenges it.
- **The ledger.** Hash-chained, append-only events; actions `create, claim, submit, pass, fail, reopen, park, unpark, usage, test_run, run_report, release, withdraw, approve_spec`. A `pass` needs a CI run for the exact commit, a clean run report, and may carry merge evidence (`task_commit`, `final_sha`, `merges`).
- **Plumbing Phase 2 reuses:** `Conductor._call` (metering, caps, holds, stops, tamper guard R9/R14/R42), per-task worktrees (`core/worktrees.py`), the merge journal (`core/finalize.py`), readiness gating and `waiting_on` (R54/R55), plan tasks with `depends_on` (R55), the digest and instant kinds (`core/channel.py`, E4), the status page (E5), drills in `drills/run_drills.py` (1–21 today).

## 2. Roles Phase 2 adds

| Role | Engine | May write | Output shape |
|---|---|---|---|
| Auditor | Codex, read-only, fresh session | nothing | `S_AUDIT`: `verdict` (`clean` or `findings`), `findings` list |
| Challenger | Codex, workspace-write, fresh session | its own scratch worktree; edits are discarded after plain code checks them | `S_CHALLENGE`: `verdict` (`overturned` or `stands`), `route`, `tried`, `error`, `proof` |
| Scorer | plain code | `state/scores.json` | per-role counts and rates |

- Role instructions live in protected files `agents/auditor.md` and `agents/challenger.md`, read through `core/roles.role_text` like the other roles.
- Engines are fixed by role (D-020). Every claim the Challenger attacks comes from a Claude role (Builder, Troubleshooter), so the Challenger is Codex: a different engine from the claimant. When Codex is capped or held, the Challenger and Auditor wait; they never fall back to Claude.
- The Auditor and Challenger are new `Team` members (`team.auditor`, `team.challenger`). `real_team` builds them from `CodexAgent` with sandbox `read-only` and `workspace-write` respectively. Both are added to `SMOKE_ROLES` (R23/R29): the auditor as a read-only role, the challenger as a writer role.
- Ledger identities: audit events are applied by `forge-auditor` (role `auditor`, already in `ROLES`); challenge outcomes are verified by plain code and applied by `forge-core` (role `core`).

## 3. The Auditor

- **Trigger.** When a build task's finalization completes (the ledger `pass` exists and the merge journal record is done), the conductor appends `{tid, cid, base, task_commit, final_sha}` to `state/audit_due.json`, where `base` is the layer tip the task's tests commit was made on. This is a durable write, done in the same place `drift_due` is set, so a crash cannot lose an audit.
- **Scheduling.** In `step()`, after the drift check and before a new task stage, the oldest `audit_due` entry runs one audit and `step()` returns `"worked"`. An audit waits (and records `waiting_on`) while `codex` lacks usable evidence or is capped or held; that wait is not a failure and costs nothing (R37/R54 style).
- **What it sees.** `agents/auditor.md`; the ledger contract (`title`, `spec_ref`, `acceptance`, `files_in_scope`); the task's `section`; its plan file if one exists; the design file (`limits["spec_file"]`, defaulting to the layer design); the merged diff `base..task_commit`, tests included; and the judges' evidence (test result, mutation score and survivors). It does **not** see the Reviewer's verdict or reasons, so it judges independently.
- **Where it runs.** A throwaway worktree at `final_sha`, read-only sandbox. After the run, plain code checks that `HEAD` and the file list are unchanged (the R29 read-only check). A change makes the audit invalid: logged, counted against the auditor, and retried once; R9 tamper rules still apply to `state/`.
- **Output** `S_AUDIT`: `{"verdict": "clean" | "findings", "findings": [{"id", "severity": "blocker" | "major" | "minor", "file", "line", "summary", "evidence", "contract_ref"}]}`. Plain code drops a finding whose `file` does not exist at `final_sha`, whose severity is not in the enum, or whose `summary` or `evidence` is empty; each dropped finding is counted as auditor noise. A `findings` verdict with no valid findings counts as `clean`.
- **Ledger.** New ledger action `audit` (role `auditor`, allowed from `done`, no transition), payload `{"kind": "report", "run_id", "commit": final_sha, "verdict", "findings"}` (capped like notes, R19). A finding never undoes `done`: the ledger is append-only, and the fix is new work.
- **Fix tasks.** Every valid `blocker` or `major` finding becomes a new build task at the end of the queue:
  - `id`: `<tid>-F<n>` (must satisfy the R1 id rule; truncate `tid` if needed);
  - `title`: `Fix (audit of <tid>): <summary>`;
  - `section`: the finding (summary, evidence, file, line, contract_ref), the original task's `section`, and the rule "the test writer must write a test that reproduces this finding; it must fail on the merged code";
  - `files_in_scope`: the original task's `files_in_scope`, plus the finding's `file` when it is not a test file;
  - `test_files`: `["tests/core/test_audit_<tid>_f<n>.py"]` (lower case), `test_cmd` `python -m unittest` on that file;
  - `audit_of`: `{tid, cid, finding_id}`.
- **A finding must be reproduced to count.** The fix task's test-writer stage runs the normal weak-test check (R4 and the empty-implementation check). If its tests are accepted, the finding is **confirmed**: ledger `audit` event `{"kind": "confirmed", "of": <tid>, "finding": <id>}`. If the tests are rejected twice, the finding is **unconfirmed**: ledger `audit` event `{"kind": "unconfirmed", ...}`, the fix task becomes `dismissed` (a new final status), and no question goes to Ben.
- **Minor findings** go to the original task's notes and the next digest; they create no task.
- **Fix tasks are audited too**, like any merge. A finding on a fix task of a fix task (depth 2) creates no further task: it goes to Ben's digest as a question of kind `audit` with the default "Forge leaves it as is".
- **Gate.** The layer gate never opens while `audit_due` is non-empty or any fix task is not `done` or `dismissed`. `dismissed` counts as finished everywhere `done` does for gate purposes, and never for coverage.
- **Drift keeper and coverage read the same design file.** `_drift_check` reads `limits["spec_file"]` (as `_spec` already does) instead of the hard-coded `layer-1-design.md`.

## 4. The Challenger

- **What it attacks.** Every "impossible" verdict an agent gives:
  1. a builder blocker claim that passed Layer 1's checks (evidence, capability map, Reviewer);
  2. a Troubleshooter `dead_end`, before it may enter `dead_ends.jsonl`;
  3. a task's final `blocked` verdict (R5), before the `blocked` question is sent to Ben.
- **Its brief.** "Your job is to find a working route. You win only if plain code verifies your route." It sees the task `section`, the claim (summary, tried, error, capability, meanwhile, or the dead-end notes, or the task's failure history), the capability map entry, `KNOWN DEAD ENDS:`, and the attempt's diff. It runs in a fresh scratch worktree at the task's tests commit.
- **Output** `S_CHALLENGE`: `{"verdict": "overturned" | "stands", "route": str, "tried": [str], "error": str, "proof": "patch" | "capability"}`.
- **An overturn must be proven by plain code.**
  - `proof: "patch"`: the scratch worktree's changes must lie within `files_in_scope` and must not touch `test_files`. Then `test_cmd` runs there without a shell (R1). It is verified if the run passes (exit 0 and `Ran N` with N ≥ 1), or if it shows strictly fewer failures plus errors than the same command at the tests commit (progress disproves "impossible").
  - `proof: "capability"`: plain code runs that capability's readiness check fresh, ignoring any TTL. It is verified if the check is `ok`.
  - Anything else, or a proof that fails verification, is an **unverified overturn**, counted against the challenger.
- **A claim stands only when the Challenger fails with evidence.** A `stands` answer counts only with at least 2 distinct `tried` routes that are not copies of the claimant's routes, and a non-empty `error`. A `stands` without that evidence counts as an unusable run.
- **Bounds.** At most `challenge_runs` (default 2) Challenger runs per claim. If none gives a verified overturn or an evidenced `stands`, the claim is **unconfirmed**.
- **Outcomes for a builder blocker:**
  - **overturned**: the attempt fails as an easy-out: `_record_easy_out` with reason `blocker overturned by challenger: <route>`, and the route is stored for the next builder prompt under a new heading `CHALLENGER ROUTE:`. No `needs` entry is added and there is no handoff.
  - **stands**: the existing accepted-blocker path (`needs`, immediate Troubleshooter handoff), with the challenger's evidence added to the task notes.
  - **unconfirmed**: the attempt fails with reason `blocker unconfirmed: challenger gave no evidence`. No `needs` entry, not an easy-out; the normal focus rule applies.
- **Outcomes for a dead end:** it is held in the task's `dead_end_pending` until challenged. **overturned**: it is never written to `dead_ends.jsonl`, it is scored against the troubleshooter, and the route goes to `CHALLENGER ROUTE:`. **stands**: it is appended to `dead_ends.jsonl` with `"challenged": <run_id>`. **unconfirmed**: it stays only in the task's own trouble notes.
- **Outcomes for a blocked verdict:** **overturned**: the task returns to `tests_ok` with the route in its notes and its focus counters reset, at most `challenge_rescues` (default 1) times per task. **stands or unconfirmed**: the task is blocked as today, and the challenger's evidence is included in the `blocked` question to Ben.
- **Never a stall.** A capped, held, stopped or unready Challenger undoes nothing permanent: a builder attempt is withdrawn (R54, not counted), a pending dead end or blocked verdict stays pending (like `troubleshoot_pending`, R38) and runs first when the Challenger can run.
- **Records.** Every challenge writes `state/challenges.jsonl` and a ledger event: new action `challenge` (role `core`, any contract status, no transition), payload `{"target": "blocker" | "dead_end" | "blocked", "claimant": <role>, "verdict": "overturned" | "stands" | "unconfirmed", "proof", "route", "run_ids"}`. The scratch worktree is removed; nothing the Challenger wrote reaches any branch. The Builder must build the route itself, tests first, so no agent grades its own work.

## 5. Scores per agent role

- **Source of truth is the ledger** (D-001). `core/scores.py` derives scores from ledger events only; `easy_outs.jsonl` and `challenges.jsonl` are conveniences.
- **What is counted, per role** (the "agent" is the role, and each role has one engine):
  - **builder:** false claims (`false_claim` notes); easy-outs (`run_report` with `easy_out`, including challenger overturns); escaped defects (`confirmed` audit findings on its tasks).
  - **troubleshooter:** overturned dead ends; overturned blocked verdicts.
  - **reviewer:** missed defects (`confirmed` findings on tasks it passed).
  - **auditor:** unconfirmed findings; dropped (invalid) findings; invalid read-only runs.
  - **challenger:** unverified overturns; `stands` without evidence.
  - **for each role:** the number of runs, so each count also has a rate.
- **Output.** `scores(ledger, state) -> dict` written to `state/scores.json` after every audit, challenge or verdict; a "Scores" section in the daily digest and on the status page.
- **Agents see their record.** Builder and Troubleshooter prompts get one line, `YOUR RECORD (last 7 days): false claims <n>, easy-outs <n>, overturned <n>`.
- **Alarm (D-023 "repeated false claims").** When one role's false claims plus easy-outs in the last 24 hours reach `limits["false_claim_alarm"]` (default 3), an instant question of a new kind `false_claims` is asked (added to `channel.INSTANT_KINDS`), at most once per role per 24 hours, with the default "Forge keeps building; the role's record stays in its prompts". It does not pause Forge (D-037).
- **No automatic tuning.** Scores are recorded and shown only. Acting on them (prompt changes, thresholds) is the Learner's job (Phase 4) with evidence.

## 6. Gate drills

- **Mechanical drills** (fake agents, no network, in `drills/run_drills.py`, next free numbers, 22 onward today):
  - an Auditor finding becomes a ledger `audit` event and a fix task, and the gate stays closed until the fix task is done or dismissed;
  - an unreproducible finding is dismissed and scored against the auditor;
  - a fake blocker with a patch proof is overturned, logged as an easy-out, and adds no `needs`;
  - a Challenger "overturn" whose patch fails `test_cmd`, or touches a test file, is not accepted;
  - a dead end that is overturned never reaches `dead_ends.jsonl`.
- **Live gate drill (a): planted bug caught.** `python -m core.gate_drills planted-bug`. Fixture: `drills/fixtures/phase2/planted_bug/` holds a tiny spec, a contract, passing tests and a merged change with one planted defect the tests do not cover (for example an inclusive upper bound implemented as exclusive). Nothing in the fixture names the defect. The drill builds a throwaway git repo and ledger from the fixture, records the change as merged, and runs the **real** Auditor through the same code path the conductor uses. It passes only if: a `blocker` or `major` finding names the planted file within 3 lines of the planted line; the ledger holds the `audit` event; and a fix task was queued. It also runs the same audit on a clean control copy (bug removed), which must give no `blocker` or `major` finding at that location.
- **Live gate drill (b): fake "impossible" blocker overturned.** `python -m core.gate_drills fake-blocker`. Fixture: `drills/fixtures/phase2/fake_blocker/` holds a task whose tests need only the standard library (for example parsing dates with `datetime`), and a canned builder claim that it is impossible without an uninstalled package, with two plausible "tried" routes and an error. The drill runs the claim through the real Layer 1 checks and the **real** Challenger. It passes only if the verdict is `overturned` with a plain-code-verified `patch` proof, the ledger holds the `challenge` event, and the builder's score shows the easy-out. Control: a genuinely impossible claim from the same fixture (its test needs a file whose content is fixed by a hash nobody can invert) must end `stands` with evidence, never overturned.
- **Evidence.** Each live drill writes `state/gates/phase-2-<drill>.json` (verdicts, run ids, ledger event hashes, token use) and prints a one-line result; exit code 0 only on a pass. The live drills cost Codex tokens and count against the caps like any run.
- **Phase 2 is done** when the mechanical drills and the core suite pass, both live drills pass on Ben's PC (D-035), and the result files are attached to the layer pull request.

## 7. Build order

- **P2A Auditor** (`docs/superpowers/plans/2026-10-01-phase-2a-auditor.md`): the ledger `audit` action, `S_AUDIT` and `agents/auditor.md`, the `audit_due` trigger and scheduling, findings validation, fix tasks, confirmed/unconfirmed/`dismissed`, gate condition, drift keeper reading `spec_file`.
- **P2B Challenger** (`docs/superpowers/plans/2026-10-01-phase-2b-challenger.md`, `depends_on` P2A): the ledger `challenge` action, `S_CHALLENGE` and `agents/challenger.md`, proof verification, the three targets, bounds and pending/withdraw handling.
- **P2C Scores and gate drills** (`docs/superpowers/plans/2026-10-01-phase-2c-scores-gate.md`, `depends_on` P2A and P2B): `core/scores.py`, prompt record line, digest/status page section, the `false_claims` alarm, mechanical drills, fixtures and `core/gate_drills.py`.
- **Starting the lane:** load the queue with `python -m core.bootstrap init --layer phase-2 --tasks docs/specs/phase-2-queue.json`, with `spec_file` pointing at this file for that lane.

## Proposed decisions (not yet in DECISIONS.md)

**D-039 (proposed) The Auditor re-checks every merge, and its findings must be reproduced to count.** Codex, read-only and in a fresh session, audits each merged task against its ledger contract and the design, without seeing the Reviewer's reasons. Blocker and major findings become fix tasks; a finding is confirmed only when the fix task's test writer produces a test that fails on the merged code, and an unreproducible finding is dismissed and counted against the Auditor. A finding never undoes `done`; the layer gate waits for every audit and fix task.

**D-040 (proposed) Every "impossible" verdict faces a Challenger, and only plain code decides who won.** Accepted builder blockers, Troubleshooter dead ends and final `blocked` verdicts are each attacked by a Codex Challenger in a scratch worktree. An overturn counts only when plain code verifies it (the task's own tests pass or measurably progress within scope, or the capability checks out). A claim stands only when the Challenger fails with evidence (2 different routes and a real error); otherwise it is unconfirmed and gets no blocker privileges. The Challenger's code is discarded: the Builder builds the route, tests first.

**D-041 (proposed) Agents are scored per role from the ledger, and repeated false claims alarm Ben without stopping the build.** Scores count false claims, easy-outs, overturned verdicts, escaped and missed defects, and the Auditor's and Challenger's own errors. Three false claims or easy-outs by one role in 24 hours is the D-023 "repeated false claims" safety alert: an instant email, at most once per role per day. Under D-037 it does not pause building. Scores are not used to change prompts or limits until the Learner (Phase 4).

**D-042 (proposed) Phase 2's gate is proven twice: by mechanical drills and by live drills with real Codex.** The mechanical drills prove the plumbing; the live drills (planted bug, fake blocker, each with a control) prove the agents actually catch and overturn (D-035). Fixtures never name the planted defect, and the live drills run in throwaway repos outside the Forge checkout.
