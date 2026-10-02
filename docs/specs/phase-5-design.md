# Phase 5 design: Scout and Optimizer

> Source of truth for Phase 5 (`docs/ROADMAP.md`: "Finds new tools and free models, trials them in a sandbox, rolls back regressions. Gate: poisoned plugin caught; regression rolled back; stale skill retired"). It builds on Layer 1 (`docs/specs/layer-1-design.md`, `docs/specs/bootstrap-conductor.md` R1-R67) and Phase 2 (`docs/specs/phase-2-design.md`) and must not contradict them or `docs/DECISIONS.md`. Plan tasks: `docs/specs/phase-5-queue.json`. New decisions this design needs are listed at the end as **proposed** entries; they are not in `DECISIONS.md` yet. Where it touches the Phase 3 (Account Factory, hands) and Phase 4 (Learner, skills) specs, which are written separately, it names the interface it assumes; the plans must reconcile with `phase-3-design.md` and `phase-4-design.md` once those merge.
>
> The numbered sections below use the coverage-map format (`core/coverage.py`): each top-level list item in a `## N.` section is requirement `N.k`.

## Why (PURPOSE trace)

PURPOSE: "Forge stays current: it finds better tools and fixes its own bottlenecks, and proves each change helped." Three of its failure rows apply:

- **False claims.** A tool's own description ("safe", "faster") and an agent's own belief that a change helped are claims. Phase 5 accepts neither: plain code vets, a sandbox with tripwires trials, and measured metrics decide.
- **Forgetting progress.** Without a record, a tool tried and rejected is tried again, and a change that quietly made things worse stays. Every candidate, trial, adoption and rollback is a ledger event.
- **Easy-way-out stops.** A missing tool is never a reason to stop (D-033). The Scout is how Forge equips itself, but only through the D-032 rules: free and vetted installs itself, new accounts stop at the human-only step, anything that costs money goes to Ben.

Marketplaces are a malware route (a fake email server copied every email to an attacker while working normally; a fake server shipped a password stealer). So the safety design below is the main work of the phase, not the discovery.

## 1. What already exists (do not rebuild)

- **The ledger** (`core/ledger.py`): hash-chained, append-only; actions `create, claim, submit, pass, fail, reopen, park, unpark, usage, test_run, run_report, release, withdraw, approve_spec` plus Phase 2's `audit` and `challenge`. Identities in `roles.json`: `ci, forge-manager, forge-executor, forge-auditor, benjamin, forge-core`.
- **Usage metering** (`core/usage.py` `Meter`): tokens per engine per UTC day, limit holds, the daily caps in `charter/limits.json` (D-020). The Max-window share Phase 5 needs is derived here.
- **The capability map** (`core/readiness.py`): `state/capabilities.json`, `run_checks`, TTLs, `requirements`, `diagnose`. A newly adopted tool becomes a capability with a check, so a broken tool is caught like any other.
- **Throwaway worktrees** (`core/worktrees.py`), **the runner** (`core/runner.py`, no-window, below-normal priority, process-tree kill on timeout) and **the digest, instant kinds and Ben's queue** (`core/channel.py`, D-023: every question carries a default).
- **The status page** (`core/status_page.py`, D-022) and the **drills** (`drills/run_drills.py`).
- **The charter** (`charter/authority.md`, `charter/limits.json`): D-011 (no startup budgets), D-032 (what Forge does when it lacks something). The "do not install" list lives in `PLUGINS_HANDOFF.md` on Ben's PC (D-017), outside the repo.
- **Installed on the PC** (`docs/STATUS.md`): Docker, n8n, Ollama, Node, Python, FFmpeg. Docker is the sandbox substrate; Ollama is the free local model.
- **Phase 2** (branches `phase-2a`, `phase-2b`, in progress): the read-only Auditor (`team.auditor`), per-role scores (`core/scores.py`) and `core/gate_drills.py`. Phase 5 reuses the Auditor and the scores; it does not edit `core/gate_drills.py` (its drills live in `core/gate_drills_p5.py`).
- **Not there yet:** no record of tools tried, no bottleneck metrics, no way to undo an adopted change, no skill staleness check, no free-model routing, and no `scout` or `optimizer` agent files in `agents/`.

## 2. Roles and identities

| Role | Engine | May write | Output shape |
|---|---|---|---|
| Scout | Claude, read-only, research tools | `state/scout/` only (candidate files) | `S_SCOUT`: list of candidates (`name`, `kind`, `source_url`, `publisher`, `bottleneck`, `why`, `declared_access`, `cost`) |
| Vetter | plain code | `state/changes/<id>/vet.json` | verdict `pass` or `reject`, reasons, flags |
| Trial runner | plain code + Docker | `state/changes/<id>/trial/` | tripwire report |
| Access reviewer | Codex, read-only (the Phase 2 Auditor role, new mode) | nothing | `S_ACCESS`: `verdict` (`approve` or `reject`), `needed` and `unneeded` capabilities, `findings` |
| Optimizer | Claude, read-only | `state/optimizer/proposal.json` only | `S_OPTIMIZE`: one proposed change with the metric it targets |
| Applier, Rollbacker, Retirer | plain code | tunables overlay, tool install dir, skill status | none |

- **Role instructions** live in protected files `agents/scout.md`, `agents/optimizer.md` and `agents/access_reviewer.md`, read through `core/roles.role_text`. **Registering the Scout, Optimizer and access reviewer:** each new role goes into `roles.EXTRA_ROLES` and `EXTRA_DEFAULTS`, onto `Team` as a plain class attribute defaulting to None (`scout = None`, and so on), and into `SMOKE_ROLES` (skipped when the member is None); `ROLE_NAMES`, `DEFAULTS` and the six `Team` fields are never changed (D-072; interface as written in `docs/specs/phase-3-design.md`, role `registrar`).
- **Ledger identities** `forge-scout` (role `scout`) and `forge-optimizer` (role `optimizer`) are added to `roles.json` (protected). Vetter, trial runner, applier, rollbacker and retirer events are applied by `forge-core`. The Optimizer and the Scout never apply anything; they only propose.
- **Engines are fixed by role (D-020).** The access reviewer is Codex and therefore independent of the Claude Scout whose candidate it reviews. When Codex is capped or held the review waits; it never falls back to Claude.

## 3. The change ledger

- **Every candidate is a ledger contract of a new kind `change`**, id `CHG-<yyyymmdd>-<n>` (must satisfy the R1 id rule), created by `forge-scout` or `forge-optimizer`. Its `kind_of_change` is one of `tool` (a plugin, MCP server, CLI or library), `model_route` (a free or local model for a job class), `tunable` (a numeric limit, section 7) or `skill_retire` (section 9).
- **Statuses.** `candidate`, `vetted`, `trialing`, `trial_passed`, `adopted` (on probation), `confirmed` (probation survived), and the terminal `rejected`, `rolled_back`, `retired`. Only the transitions in the table below are valid; the ledger refuses others.

| From | To | Applied by | Evidence required in the event |
|---|---|---|---|
| (create) | `candidate` | scout, optimizer | candidate record, bottleneck it fixes |
| `candidate` | `vetted` | core | `vet.json` with verdict `pass` |
| `candidate` | `rejected` | core | vet reasons |
| `vetted` | `trialing` | core | trial run id, sandbox spec hash |
| `trialing` | `trial_passed` | core | tripwire report with zero hits, plus the access review `approve` for a `tool` |
| `trialing` | `rejected` | core | tripwire hits or access review `reject` |
| `trial_passed` | `adopted` | core | the adoption gate of section 6 |
| `adopted` | `confirmed` | core | probation metrics, no regression |
| `adopted` | `rolled_back` | core | regression evidence (section 8) |

- **New ledger action `change`** (roles `scout`, `optimizer`, `core`; any contract status; follows the table above). Payload `{"kind": "candidate" | "vetted" | "trial" | "adopted" | "confirmed" | "rejected" | "rolled_back" | "retired" | "restored", "change_id", "evidence": {...}, "before": {...}, "after": {...}}`, capped like notes (R19). `before` and `after` hold exactly what is needed to undo or redo the change.
- **Every adoptable change implements three functions**, in `core/changes.py`: `apply(change)`, `undo(change)` from the stored `before`, and `measure(change, window)`. A change type without a tested `undo` cannot be adopted.
- **Files.** `state/changes/<id>/` holds `candidate.json`, `vet.json`, `trial/` and `review.json`. `state/changes.jsonl` is a convenience log; the ledger is the source of truth (D-001). `state/blocklist.json` lists rejected sources and names with the reason and the event hash; a blocked candidate is dropped by the Scout before it costs a run.
- **Idempotent and crash-safe.** `apply` and `undo` write a journal entry first (like the merge journal, `core/finalize.py`), so a crash between "install" and the ledger event is repaired on the next step, in either direction.

## 4. The Scout

- **Allowlisted sources only.** `charter/scout_sources.json` (protected, Ben approves changes) lists each source with its cadence: daily release notes for every tool in use (Claude Code, Codex, n8n, Higgsfield, Windsor.ai); weekly the Claude Marketplace, Glama's MCP registry, n8n templates, GitHub topics (for example `auto-clip`) and free-model lists. Reddit, TikTok and X are not sources. A source not on the list is never fetched; the Scout may only propose a new source, as a question to Ben (**needs Ben**).
- **A candidate exists to fix a named bottleneck.** `S_SCOUT` rejects a candidate with no `bottleneck` taken from `state/metrics.json` (section 7) or from a capability that is `broken` or `missing` in the capability map. This is the buy-before-build rule (PURPOSE) in code: no browsing for novelty.
- **Rate and dedupe.** At most `limits["scout_candidates_per_week"]` (default 10) new candidates; a candidate whose name or source URL is on the blocklist, or already `adopted`, is dropped; one `rejected` less than 90 days ago is dropped unless its version changed.
- **Release-note watch.** A new release of a tool Forge already uses creates a `tool` candidate of type `upgrade`. Upgrades go through the same vetting and trial as installs, and they keep the previous version for rollback.
- **The Scout never installs.** It has no shell outside its research tools and no write access outside `state/scout/`. After a Scout run, plain code checks that nothing else changed (the R29 read-only check), and R9 tamper rules apply to `state/`.

## 5. Vetting and the sandbox trial

- **Static vet (plain code, `core/vet.py`).** A candidate passes only if all of these hold, and every failure is a reason in `vet.json`:
  - the publisher is on the known-publisher list in `charter/scout_sources.json` or the repository shows an established history (age, count of releases) above thresholds in `limits`;
  - the name and source are not on `charter/do_not_install.json` (protected; seeded from the do-not-install list in `PLUGINS_HANDOFF.md`) or the blocklist;
  - a pinned version and a hash are recorded; a floating `latest` is rejected;
  - the license permits use and the terms of service do not forbid automated use (D-015, PURPOSE "Honest and legal");
  - the package declares its access (network hosts, read paths, write paths, environment variables, install scripts), and a scan of the code finds none beyond the declaration: network calls to undeclared hosts, reads of credential locations (`.ssh`, browser profiles, `forge-*` credential names, `.env`), install or post-install scripts that run code, and obfuscation (base64 or hex blobs decoded and executed, dynamic `exec` or `eval`, dynamically built URLs or paths);
  - it needs no account, key or payment, or the need is stated and the candidate is marked `needs_ben` (section 6).
- **The sandbox (`core/sandbox.py`).** Docker, no host mounts except a fresh empty work directory, no Windows credentials, an empty environment, a read-only root file system, CPU and memory limits, `agent_timeout_s` as the hard limit, and an internal network whose only reachable host is a **sink** container that records every connection and body. Docker's own filesystem layer is diffed after the run. Nothing in the sandbox is ever given a real credential. The substrate is behind an interface (`Sandbox.run(spec) -> report`); Phase 3's isolated space may become a second backend, and this phase does not depend on it.
- **The harness exercises the tool.** It installs the pinned package inside the sandbox, starts it, lists its tools or commands and calls each with generated, harmless arguments, so behaviour that triggers on first use is seen. A tool the harness cannot exercise is `trial_passed`-ineligible.
- **Tripwires** (each hit is a rejection): a connection to anything but the declared hosts, or any connection at all when none are declared; any **canary** (a random per-run secret planted in fake `.env`, `.ssh/id_*`, browser-profile and `credentials.json` files in the sandbox home) appearing in the sink, in an output file or in a log; a write outside the declared write paths; an extra process left running; resource use over the limits; and a sandbox escape attempt (mount, ptrace, privileged call) logged by Docker.
- **A trial can reject but cannot prove safety.** A time-triggered or environment-triggered payload may not fire. The answer is layered, not one gate: static vet, tripwire trial, independent access review, and a probation with minimal privilege and automatic rollback (section 6). No credential reaches a tool before all four pass.
- **Access review (independent).** For a `tool`, the Codex access reviewer sees `candidate.json`, the declared access, the vet and trial reports and the code at the pinned hash, read-only, and answers `S_ACCESS`. It judges purpose against access: "does a clip cutter need email?". Plain code is the veto: the reviewer can only add rejections, never override a vet or trial rejection. A reviewer run that changed `HEAD` or any file is invalid and counted (R29 style).
- **Free models are trialed too.** A `model_route` candidate runs the fixed evaluation set of its job class (section 10) in the sandbox with synthetic public data only, and scores it by plain-code checks.
- **Bounds.** At most `limits["trial_runs_per_candidate"]` (default 2) trials per candidate; a candidate that errors both times is `rejected` with reason `trial_unusable`. Trials run at below-normal priority, never use Ben's screen or input (D-019), and pause while Ben is active as other heavy work does.

## 6. Adoption and probation

- **The adoption gate, in order:**
  1. `trial_passed`, and for a `tool` the access review `approve`;
  2. the D-032 classification by plain code from the candidate's declared needs: **free and no account** (auto-eligible), **needs an account** (stops at the human-only step: a queue item for Ben, **needs Ben**), **costs money or a subscription** (a hard stop: **needs Ben**, with the cost and the venture or bottleneck it serves, default "not adopted");
  3. no new broad access: a tool declaring email, Drive, browser-profile or whole-disk access is never auto-adopted, whatever the review says (**needs Ben**, default "not adopted");
  4. `limits["optimizer_auto_adopt"]` is true (default **false**, D-035): while false every adoption is a proposal in Ben's queue and the digest, applied only on his yes.
- **Credentials are granted only after adoption, one named capability at a time, and only by Ben's yes** (**needs Ben**, default "none"). The stated purpose is recorded in the ledger. Secrets are fetched by plain-code tools from Windows Credential Manager (D-029); the agent never sees a value.
- **Install location.** Adopted tools install under `C:\Users\benja\Forge-tools\<name>\<version>` (outside OneDrive, D-005; outside the repo), with the pinned hash re-verified on every start. A new capability entry and readiness check are added to the capability map in the same step, so a broken tool starts a Troubleshooter job like any other.
- **Probation.** Every adopted change has `limits["probation_days"]` (default 7). During probation the tool runs with the minimum privilege it declared, its first uses are logged in full, and `measure` runs daily. The change becomes `confirmed` only when probation ends with no regression (section 8).
- **Upgrades keep the old version** until the new one is `confirmed`; rollback is a switch back, not a reinstall.

## 7. Metrics and the Optimizer

- **`core/metrics.py` `collect(ledger, state, now) -> dict`** derives, from ledger events and the usage meter only, written to `state/metrics.json` with a daily row appended to `state/metrics.jsonl`:
  - share of the daily Claude and Codex caps used (from `Meter`, D-020);
  - cost (tokens) and wall-clock time per merged contract;
  - the share of tokens spent on attempts that failed;
  - audit pass rate and false-claim and easy-out rates (from Phase 2's `core/scores.py`; reported `unavailable`, never 0, while Phase 2 is absent);
  - blockers by type and by capability;
  - canary failures (Phase 4 runbooks and Phase 6 ventures add theirs; `unavailable` until they exist);
  - for ventures, the venture's own metrics (Phase 6 supplies the rows).
- **Alarm levels** per metric live in `charter/limits.json` as `metric_alarms`. Crossing one runs the Optimizer early, at most once per metric per 24 hours; the alarm is a digest line, not an instant email.
- **The weekly loop.** A scheduled timer (default Sunday 03:00 local, inside quiet hours) runs one Optimizer cycle through five steps: **measure** (collect), **pick** (plain code ranks bottlenecks by wasted tokens and time; the Optimizer may choose among the top 3 only), **propose** (one change), **trial**, **decide**. At most one change per cycle and **at most one `adopted` change per metric family at a time**, so any regression can be attributed.
- **What the Optimizer may change.**
  - A **tunable** listed in `charter/tunables.json` (protected): name, type, bounds, step, the metric it affects, and a mode `auto` or `needs_ben`. The effective limits are the charter values plus a runtime overlay `state/tunables.json`; the overlay can only name registered tunables and only values inside the bounds.
  - A **model route** (section 10) and a **tool** (sections 4 to 6).
  - It may not touch: spend or contact limits (`*_token_cap`, `mail_*`), `mutation_min`, any protected file, any role instruction, any skill (the Learner's, Phase 4). A proposed protected-file or role-text change is written as a pull request description in Ben's queue and is never applied by the Optimizer (D-010: conductor agents never hold the label credential; **needs Ben**, default "not applied").
  - The D-026 focus numbers (attempt count, 20 minutes, 3 merges, 2 hours) are registered as `needs_ben`: D-026 says tuning them needs Ben's approval, so the Optimizer proposes with its evidence and the default is "no change".
- **Evidence the change helped.** Plain code decides, never the Optimizer. The trial is a paired replay of up to `limits["optimizer_replay_tasks"]` recorded tasks (a Phase 4 replay when it exists; before that a fixed set of planned fixture tasks under `drills/fixtures/phase5/`) under baseline and candidate settings, with a token budget `limits["optimizer_trial_token_cap"]` that counts against the daily caps like any run. A change is adopted only if its target metric improves by at least `limits["optimizer_min_gain"]` (default 10 percent) and no guard metric (audit pass rate, false-claim rate, blocker rate, tokens per merged contract) worsens by more than `rollback_tolerance`. A trial that cannot reach a decision in budget is `rejected` with reason `inconclusive`.
- **The Optimizer is scored like the other roles:** a change it proposed that is later rolled back is counted against it in `core/scores.py` (extension in P5C), and the count appears in its own prompt as `YOUR RECORD`. The Learner (Phase 4) may later tune the Optimizer's thresholds with evidence; Phase 5 does not.

## 8. Rollback of regressions

- **Rollback is plain code and automatic.** Each daily probation `measure`, the rollbacker compares the target metric and every guard metric over the probation window with the same-length baseline window before adoption. It rolls back when any guard metric worsens by more than `limits["rollback_tolerance"]` (default 10 percent relative) with at least `limits["rollback_min_samples"]` (default 5 contracts or events) in the probation window, or when the target metric ends probation worse than baseline. Too few samples never roll back (no flapping) and never confirm: probation extends once by `probation_days`, then the change is rolled back as `inconclusive`.
- **Rollback is immediate and complete.** `undo(change)` restores `before` (overlay value, previous tool version or route), the ledger gets `rolled_back` with the metric rows, the rolled-back change is blocklisted for 90 days, the Optimizer or Scout that proposed it is scored, and a digest line names what was undone and why. A failed `undo` is an instant question to Ben (kind `safety`, a stop-class event under D-023) and the change's tool is quarantined: its capability marked broken and its credentials revoked by plain code.
- **A rollback never stops building.** It is recorded and reported, not an alarm (D-037).
- **Manual rollback** is always available: `python -m core.changes rollback <id>`, from the status page or by emailing `ROLLBACK <id>`. It is the same code path and needs no evidence.
- **Confirmed is not forever.** `measure` keeps running weekly after confirmation; a later regression files a new `tunable` or `tool` candidate to undo it, through the normal path.

## 9. Stale skill retirement

- **Interface assumed from Phase 4** (the Learner's skill store; reconcile with `phase-4-design.md`): a `SkillIndex` with `list() -> [{id, path, created, last_used, requires, replay: {last_ok, last_at, consecutive_failures}, runbook_of, status}]`, `retire(id)` and `restore(id)`. Phase 5 defines the protocol and a file adapter; Phase 4's store implements it, or the adapter reads Phase 4's index file. Until Phase 4 merges, everything below runs against a fake index.
- **Stale means any of:** unused for `limits["skill_stale_days"]` (default 60) and not replay-verified in that window; `replay.consecutive_failures` of at least 2 with no open repair contract; or it `requires` a tool or capability whose change is `rolled_back` or `rejected`, or a capability that has been `broken` longer than `limits["skill_orphan_days"]` (default 14).
- **Never retire what a live venture runs on.** A skill that is some venture's active runbook (`runbook_of`) is never retired; plain code opens a repair task instead (the Learner's self-repair contract, Phase 4).
- **Retire, never delete.** `retire(id)` flips the status, moves the files to a `retired/` folder in the store, removes the skill from lookup, and writes a `change` event `retired` with the staleness evidence. `restore(id)` reverses it (`python -m core.changes restore <id>`; Ben or a replay that starts passing again). Retirement is automatic, by plain code, and needs no approval because it is fully reversible.
- **The weekly Optimizer cycle runs the staleness pass** after its metrics step, and the digest lists what was retired, so a wrongly retired skill is noticed within a week.

## 10. The free-model overflow lane

- **Trigger.** When the Claude daily share (`Meter`) passes `limits["overflow_trigger_share"]` (default 0.8), eligible jobs route to a free model instead of waiting for the reset. Below the trigger nothing changes.
- **Eligible jobs only.** A job class is eligible only if it is on an allowlist in `charter/overflow_jobs.json` (protected): captions, summaries, tagging and first-pass transcript scans, and only when the job's `sensitivity` is `public`. A job with private data, secrets, customer or Ben's information, or any money or account effect is never routed out.
- **Never downgraded.** The Manager, Test writer, Builder, Reviewer, Auditor, Challenger, Troubleshooter, Drift keeper and access reviewer never use the lane. Their engines are fixed by role (D-020), and a weaker model there brings back the false "done" problem. They wait for the window to reset.
- **Routes, in order.** (1) Local Ollama (no account, nothing leaves the PC), then (2) free tiers of OpenRouter, Groq and Cerebras through their official APIs with their own free keys. Google's free Gemini tier is excluded for private data (it trains on prompts outside the EU and UK). Plugging the Claude subscription login into any third-party tool is forbidden by Anthropic's terms and is never done.
- **Keys and accounts are Ben's.** Creating an account at a free-model provider, any one-time top-up (such as OpenRouter's $10) and any paid tier are **needs Ben**; the account stops at the human-only step (Phase 3's Account Factory, when it exists), and the key goes into Credential Manager by a command Ben runs. Until a key exists only the Ollama route is live.
- **Each route is a `model_route` change** with a fixed evaluation set per job class under `drills/fixtures/phase5/evals/` (labeled inputs with plain-code checks: JSON schema validity, required fields, key facts present, label accuracy at or above `limits["route_min_accuracy"]`). It is trialed, adopted and rolled back through sections 3 to 8. A route that fails its daily canary (a 5-item eval) is disabled by plain code and the job waits.
- **Limits.** Per-provider request budgets from the provider's published free limits are enforced by plain code (`core/overflow.py`); a provider returning errors or 429 is skipped for the rest of the day. The lane never spends money, and sends no private data, by construction.

## 11. Surfaces

- **CLI.** `python -m core.scout run [--source <name>]`, `python -m core.changes list|show <id>|rollback <id>|restore <id>`, `python -m core.optimizer cycle [--dry-run]|status`. Every command prints one line and exits non-zero on failure.
- **Ben's queue kinds** (each with its default, D-023): `adopt_tool` (default not adopted), `credential` (default none), `source` (default not added), `tunable` (default no change), `route_account` (default Ollama only), `change_pr` (default not applied). None is an instant email except a failed `undo`.
- **Status page** (`core/status_page.py`): an "Improvements" section with the current probation (change, days left, target and guard metrics against baseline), the last rollbacks and retirements, the candidate count by status, and the overflow lane state (share used, route in use).
- **Daily digest** (`core/channel.py`): one section listing candidates vetted, trials passed or rejected (with the tripwire named), adoptions, confirmations, rollbacks, retirements and the metric alarms.

## 12. Gate drills

- **Mechanical drills** (fake agents, fake clock, local sink, no network, in `drills/run_drills.py`, next free numbers at plan time): an illegal status transition is refused by the ledger; a crashed `apply` is repaired from the journal; a candidate with no bottleneck is dropped; a blocklisted candidate is dropped; a `needs_ben` change never applies without the queue answer; the overlay refuses an unregistered or out-of-bounds tunable; a regression with too few samples is not rolled back; a stale skill with an open live venture is not retired; the overflow lane refuses a `private` job and every independent role.
- **Live gate drill (a): poisoned plugin caught.** `python -m core.gate_drills_p5 poisoned-plugin`. Fixtures `drills/fixtures/phase5/plugins/` hold three small local packages, none of which names its own defect: `benign_notes` (a control that does what it declares), `poisoned_obvious` (an install script and a plain request that reads the planted canary files and posts them to an undeclared host) and `poisoned_obfuscated` (the same behaviour built at run time from encoded fragments, triggered by its first tool call, so the static scan can miss it). The drill runs each through the **real** vet, the **real** Docker sandbox and sink, and the **real** access reviewer. It passes only if: `poisoned_obvious` is rejected by the static vet and, with the vet bypassed, by the trial; `poisoned_obfuscated` is rejected by the trial's tripwire (canary seen in the sink) even with the static vet bypassed; both are blocklisted and neither ever reaches `adopted`; `benign_notes` reaches `trial_passed` with zero tripwire hits (and with the auto-adopt flag false, ends as an `adopt_tool` question); and no real credential, host path or network address appears in any sandbox artifact.
- **Live gate drill (b): regression rolled back.** `python -m core.gate_drills_p5 regression-rollback`. Fixture: a replayable metrics history (`drills/fixtures/phase5/metrics/`) and a registered `auto` tunable. The drill runs the **real** Optimizer cycle, applier and rollbacker with a fake clock: it adopts a change that helps, then injects probation metrics in which a guard metric (audit pass rate) drops by more than the tolerance with enough samples. It passes only if the next daily `measure` rolls the change back, the overlay value equals the stored `before`, the ledger holds `rolled_back` with the metric rows, the change is blocklisted, the Optimizer's score shows it, and the digest names it. Controls: a change whose probation metrics hold is `confirmed`; a drop with too few samples is not rolled back and the probation extends once.
- **Live gate drill (c): stale skill retired.** `python -m core.gate_drills_p5 stale-skill`, run against **Phase 4's real skill store** (so it cannot pass before Phase 4 merges). Fixture: three skills built through the store: one unused past the window with a failing replay, one recently used, one stale that is the active runbook of a fixture venture. It passes only if the first is retired (status, moved files, absent from lookup, ledger `retired` event with evidence), the second is untouched, the third is not retired and a repair task is opened, and `restore` brings the first back intact.
- **Evidence.** Each live drill writes `state/gates/phase-5-<drill>.json` (verdicts, change ids, ledger event hashes, token use) and prints a one-line result; exit code 0 only on a pass.
- **Phase 5 is done** when the mechanical drills and the core suite pass, all three live drills pass on Ben's PC (D-035), one Optimizer cycle has run in `--dry-run` with its proposal and evidence shown to Ben in the digest, and the result files are attached to the layer pull request. Turning on `optimizer_auto_adopt` after that watched cycle is **needs Ben**, and it is not part of the gate.

## 13. Build order and dependencies

- **P5A Change ledger, tunables, Scout** (`docs/superpowers/plans/2026-10-02-phase-5a-changes-scout.md`): the ledger contract kind `change` and action `change`, `core/changes.py` (apply, undo, journal), roles and identities, `charter/scout_sources.json`, `charter/do_not_install.json`, `charter/tunables.json` and the overlay loader, the blocklist, `S_SCOUT`, `agents/scout.md`, candidate intake. No dependency on another phase.
- **P5B Vetter, sandbox, access review** (`...phase-5b-vet-sandbox.md`, `depends_on` P5A): `core/vet.py`, `core/sandbox.py` (Docker, sink, canaries, tripwires), the harness, `S_ACCESS`, `agents/access_reviewer.md`, the poisoned-plugin fixtures and the mechanical drills for them. The access review needs the Phase 2 Auditor; until it is merged the review step is a fake in tests and a tool cannot reach `trial_passed`.
- **P5C Metrics, Optimizer, adoption and rollback** (`...phase-5c-optimizer-rollback.md`, `depends_on` P5A): `core/metrics.py`, the adoption gate, probation, `core/optimizer.py`, the rollbacker, the weekly timer, scores extension, the regression-rollback fixtures and drill logic.
- **P5D Stale skill retirement** (`...phase-5d-skill-retire.md`, `depends_on` P5A): the `SkillIndex` protocol, the file adapter, the retirer and the staleness pass, against a fake index. Binding to Phase 4's real store is the last task of this plan and is done only when `core/skills.py` (or Phase 4's named module) exists on the layer.
- **P5E Overflow lane, surfaces, gate drills** (`...phase-5e-overflow-surfaces-gate.md`, `depends_on` P5B, P5C, P5D): `core/overflow.py`, the evaluation sets, digest, status page and CLI, `core/gate_drills_p5.py` with the three drills.
- **What can be built now, in parallel:** P5A first; then P5B, P5C and P5D in separate lanes (they touch different modules; `core/bootstrap.py` wiring goes last in each and may need a rebase). P5E after them. Everything except the live access review (Phase 2 Auditor), the real skill-store binding and the live stale-skill drill (Phase 4), and the free API keys (Ben, section 10) needs nothing from an unmerged phase.
- **What waits:** the Phase 2 Auditor for adoption of any tool; Phase 4's skill store for the third gate drill; Phase 3's Account Factory is preferred, not required, for free-model accounts; Phase 6 supplies venture metrics later.
- **Starting the lane:** `python -m core.bootstrap init --lane p5 --layer phase-5 --tasks docs/specs/phase-5-queue.json --spec docs/specs/phase-5-design.md`. `init` refuses to replace a queue that still has tasks unless `--force` is given.

## Risks

- **A trial cannot prove a tool safe.** A payload that waits days, or checks for a real username, defeats the sandbox. Mitigation: layered gates, minimum-privilege probation, no credentials until Ben's yes, pinned hashes re-verified each start, and quarantine plus credential revocation on rollback failure.
- **Sandbox escape or a leak through Docker.** Mitigation: no host mounts, empty environment, no real secrets anywhere in the sandbox, internal network with a sink, canaries; the drill checks that no host path or real credential appears in any artifact.
- **Metric noise causes flapping or false confirmation.** Mitigation: minimum samples, one change per metric family, paired replay, extension then `inconclusive` rollback.
- **Goodhart.** The Optimizer could improve cost by lowering quality. Mitigation: guard metrics from independent sources (Auditor pass rate, false claims) and a ban on tuning spend, contact, `mutation_min` or protected files.
- **Optimizer trials burn the daily cap.** Mitigation: a per-trial token cap that counts against the caps; trials pause when Claude is held.
- **Scout's output is untrusted text.** Marketplace descriptions can carry prompt injection. Mitigation: the Scout is read-only with no shell, its output is schema-validated, and no candidate text reaches an agent with write access without passing vet.
- **Collisions with the Learner (Phase 4).** The boundary: the Learner owns skills and runbooks and tunes the Optimizer's thresholds; the Optimizer owns tools, routes and registered tunables. If the Phase 4 spec draws it differently, the Phase 4 spec wins and P5D is adjusted.

## Needs Ben

| Item | When | Default if no answer |
|---|---|---|
| Any new source for the Scout | the Scout proposes one | not added |
| Adopting a tool that needs an account, key, subscription or payment (D-032, D-011) | trial passed | not adopted |
| Adopting a tool that declares email, Drive, browser-profile or whole-disk access | trial passed | not adopted |
| Granting any credential to an adopted tool | after adoption | none granted |
| Any change to a `needs_ben` tunable, including the D-026 focus numbers | the Optimizer proposes | no change |
| Any change that needs a protected file or a role instruction | the Optimizer proposes | not applied |
| Free-model provider accounts, keys, any top-up or paid tier | first overflow route beyond Ollama | Ollama only |
| Turning on `optimizer_auto_adopt` after the watched dry-run cycle (D-035) | after the gate | stays off |
| Edits to `charter/scout_sources.json`, `do_not_install.json`, `tunables.json`, `overflow_jobs.json` (protected) | runtime additions | not changed |
| A failed `undo` (safety stop) | rollback fails | tool quarantined, credentials revoked |

## Proposed decisions (not yet in DECISIONS.md)

**D-060 (proposed) Nothing is installed or trusted on a Scout's word.** A candidate must fix a measured bottleneck, pass a plain-code vet and a sandbox trial with tripwires and planted canaries, and get an independent read-only access review before it is adopted. A rejection from plain code cannot be overridden by an agent. Adopted tools start on probation with minimum privilege, and credentials are granted one capability at a time only by Ben (D-032, D-029).

**D-061 (proposed) The Optimizer changes one thing at a time, inside bounds Ben set, and plain code rolls back anything that makes the guard metrics worse.** Registered tunables only; spend, contact, `mutation_min`, protected files and role instructions are out of reach; the D-026 numbers stay Ben's. A change is confirmed only after a probation with enough samples; too few samples never confirm. Auto-adoption stays off until one watched dry-run cycle has been shown to Ben (D-035).

**D-062 (proposed) Stale skills are retired on evidence, never deleted, and never while a live venture depends on them.** Retirement is automatic because it is fully reversible; a restore brings the skill back intact.

**D-063 (proposed) Free models are a low-stakes overflow lane, not a downgrade.** Only allowlisted public-data job classes use it, local Ollama first; independent roles and anything involving private data, money or accounts never do; provider accounts and keys are Ben's.
