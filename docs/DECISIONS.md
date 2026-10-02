# Forge: decisions log

> Protected file. Ben approves every entry (human-approved PR). Append-only: to change a decision, add a new entry that supersedes the old one; never rewrite history. Agents must follow the latest entry on each topic. If a situation isn't covered, put a question in Ben's queue with a proposed default. Don't decide it silently.

Format: **D-NNN (date) Decision.** Why. *Supersedes / superseded by.*

## Architecture

**D-001 (2026-09-28) Forge is a thin layer over existing tools (Claude Max, Codex, GitHub), with one ledger as the only record of progress.** A task is done only on independent evidence. Earlier attempts failed because the agent that did the work also judged it and kept its own memory. Source: `docs/source/forge-research-and-plan.md`.

**D-002 (2026-09-28) Build the trusted core first and prove it with sabotage drills before anything relies on it.** The system can't be trusted to build itself until the rules it runs under are proven to hold.

**D-003 (2026-09-28) Ben's current Windows PC is the agent computer for now.** A cloud machine may come later.

**D-004 (2026-09-29) The trusted core is Python; the PowerShell rebuild is retired.** GitHub's checks run the Python core unchanged. The PowerShell version's best ideas were ported as drills 6–10: frozen spec, restoring protected files, scope check, false-claim log, crash recovery.

**D-005 (2026-09-29) Forge lives at `C:\Users\benja\Forge`, outside OneDrive.** OneDrive sync locks files mid-write, which broke the first install and would corrupt a ledger.

**D-006 (2026-09-29) Projects Forge builds are their own local git repos, with spec, tests, roles and a git-ignored ledger.** In Layer 1, evidence runs locally at the exact commit in a throwaway worktree. A separate GitHub identity for agents (GitHub App) waits until the first layer that pushes to GitHub.

**D-007 (2026-09-29) Always-on means an invisible background service, not a timer.** It starts with Windows, restarts itself if it crashes, runs cycles back to back while there's work, and sleeps until woken when there isn't. It runs at low priority, shows no windows, and never uses Ben's screen, mouse or keyboard while he's active. Replaces the 30-minute Task Scheduler idea in the first Layer 1 plan draft.

**D-008 (2026-09-29) Foundation before Layer 1 code.** The repo is the memory (`CLAUDE.md` plus `docs/`). Anti-drift guards come first: stall detector, spec coverage map, weak-test check, and Ben's queue. Then a grilling session on Forge's own spec, then building. "Build thickest at the beginning."

## GitHub and approvals

**D-009 (2026-09-29) The repo is public for now, rather than paying for GitHub Pro.** `main` is protected, including against admins: the `core` check must pass. No secrets are ever committed.

**D-010 (2026-09-29) Protected-file changes need Ben's approval, given as the `human-approved` label from his account.** "y" from Ben in chat, or running `Approve Forge change`, counts as that approval. Forge's own agents must never hold credentials that can apply that label.

## Money

**D-011 (2026-09-29) No startup budgets: each venture funds itself from its own collected profit.** Any spend its profit doesn't cover, and every new subscription, service or API fee, goes to Ben at that moment. Forge's Claude use is capped at 50% of the Max plan per day. Recorded in `charter/authority.md`.

**D-012 (2026-09-28) Pricing strategy across ventures: minimize profit and maximize accepted offers.** Price so switching from competitors makes no sense. A base of past customers matters more than margin.

## Ventures

**D-013 (2026-09-28/29) Approved ventures.**
- Auto ventures: Whop clipping, 9x12 co-op postcards, handcrafted goods from a local carpenter.
- Ventures 1–8: truck dispatch, 9x12 postcards, property tax appeals, restaurant delivery refunds, government supply orders, tariff refunds, motel pricing, utility bill audits.

Details and timeline: `docs/source/`. Tariff refunds go first because refund windows close in early 2027.

**D-014 (2026-09-28) Ventures must be fully autonomous and self-improving.** Human involvement is limited to calls with hesitant but worthwhile clients. Target markets that don't expect AI; skip saturated "AI side hustles".

**D-015 (2026-09-28) Rejected approaches, each replaced by a compliant design.**
- Reselling on Vinted, Depop or eBay against their rules
- Marketplace bots
- Fake personas
- CAPTCHA bypass
- AI cold calls (TCPA)
- Filing DoorDash disputes as a third party
- Acting as an unlicensed customs broker (Forge matches clients with licensed brokers instead)
- Pooling competitors' pricing data (antitrust)

## Communication

**D-016 (2026-09-29) Forge reaches Ben three ways: a daily email digest, instant emails when something needs him, and a live status page.**

## Tools

**D-017 (2026-09-29) Agents use the plugins in `PLUGINS_HANDOFF.md` and access GitHub as described in `GITHUB_ACCESS.md`.** Never install anything on its "do not install" list. Vet any new tool for security and terms of service before installing.

## From the Foundation grilling (2026-09-29)

**D-018 (2026-09-29) The PC stays on 24/7, set to never sleep while plugged in (the screen may turn off).** Forge moves to a cloud machine later, once ventures pay for it.

**D-019 (2026-09-29) Forge never gets in Ben's way.**
- Never uses his screen, mouse or keyboard.
- Browsers are hidden; anything that truly needs a screen runs in its own virtual machine.
- No visible windows.
- Low priority, and lighter while Ben is active.
- Slower is an accepted trade-off.

**D-020 (2026-09-29) Usage limits apply anytime.**
- Claude: up to 50% of the Max plan per day. Forge pauses if it hits a limit window.
- Codex: up to 50% of the ChatGPT plan.
- If either is capped, the team slows down rather than swapping models between roles that must stay independent.

**D-021 (2026-09-29) Forge emails from Ben's own Gmail (benjaminanderson0802@gmail.com) with an app password.** It sends to him and reads his replies. The working copy lives in Windows Credential Manager (`forge-gmail`), with the record copy in Bitwarden. Connected and verified 2026-09-29.

**D-022 (2026-09-29) The live status page is served on the PC only.**

**D-023 (2026-09-29) Contact rules.**
- **Instant email** only when:
  - Ben alone can unblock all progress
  - a spend or subscription needs approval
  - a safety stop fires (kill switch, tampering, repeated false claims)
  - a customer call needs him
- **Everything else** goes in a daily 8am digest.
- **Quiet hours** 23:00–07:00, except emergencies.
- **Ben answers** by email reply, on the status page, or in chat. All three work.
- **Every question** comes with the default Forge will use if he doesn't answer.

**D-024 (2026-09-29) Emergency stop, three equal ways:**
- a "Stop Forge" desktop shortcut
- an email saying STOP
- a status-page button

Restarting requires Ben to deliberately clear the stop.

**D-025 (2026-09-29) Forge is built by a team, never by one agent alone.**
- Codex writes each task's tests first.
- Claude Code builds; it can't edit tests or mark itself done.
- Mechanical judges with no AI decide: tests, drills, mutation testing.
- Codex reviews read-only against the plan and PURPOSE.
- A fresh-session drift keeper checks after each task.
- Every task is a contract in the real ledger.
- Agents hand work to each other through the ledger and git, run by plain-code conductor scripts. Ben never relays messages.
- As each Layer 1 piece works, it replaces the manual step it covers.

**D-026 (2026-09-29) Focus rule: the Builder builds, a separate Troubleshooter fixes.** Supersedes any time-based "stuck" rule.
- **Non-blocking problems:** the Builder files a side ticket and moves on.
- **Blocking problems:** 2 attempts or 20 minutes, then the Troubleshooter takes it and the Builder moves to unblocked work.
- **No progress:** 2 zero-progress attempts in a row (same error, no more tests passing) trigger an immediate handoff.
- **Troubleshooter:** its own lane, 3 approaches, research tools. Its dead ends go into a shared file everyone reads.
- **Coverage:** every task names the spec section it serves, and spec coverage must rise.
- **Re-plan:** no coverage gain in 3 merges, or no merge in 2 hours of active work, sends the drift keeper in to re-plan.
- **Tuning:** the numbers are first guesses; the Learner tunes them later, with Ben's approval.

**D-027 (2026-09-29) While Forge builds itself, the team works freely on a layer branch.** Ben approves once per layer, when the layer passes its gate, with the full review report. Nothing reaches `main` without that approval.

**D-028 (2026-09-29) Forge builds its own layers from Layer 2 on.** Every change to itself is a protected-file change, so it still needs Ben's approval.

**D-029 (2026-09-29) Secrets at runtime come from Windows Credential Manager; Bitwarden holds the record copy.** Agents never see secret values: plain-code tools fetch and use them on their behalf.

**D-030 (2026-09-29) A readiness check runs before every build session and every cycle.**
- It tests every connection: Claude Code, Codex, GitHub, Gmail, Docker/n8n, Ollama, Python libraries, the hidden browser.
- Results go to a capability map (status plus last-checked time) that every agent reads.
- A builder is never started into a broken setup: the Troubleshooter fixes it first, or the exact fix goes in Ben's queue.

**D-031 (2026-09-29) A blocker claim must show real work before anyone accepts it.**
- It must include: at least 2 routes tried, the actual error output, the capability needed, and what the agent works on meanwhile.
- Plain code rejects any claim that contradicts the capability map.
- The Reviewer rejects claims without real attempts.
- Rejected easy-outs are logged against the agent, like false claims.

**D-032 (2026-09-29) When a task needs something Forge doesn't have:**
- **Free tools:** installed automatically if they pass vetting (known publisher, not on the do-not-install list, no account needed), and logged.
- **New accounts:** prepared up to the human-only step, which goes in Ben's queue.
- **Anything that costs money:** Ben approves.

**D-033 (2026-09-29) Founding principle: a missing connection is never a reason to stop.** AI agents aren't malicious, but when stumped they commonly take the easy way out. Forge is designed on that assumption. Connections are set up and verified in advance; blocker claims must show work; problems route to whoever can fix them; agents are equipped for the task at hand.

## Build order

**D-034 (2026-09-29) Build the conductor first, and let it build the rest.** A small bootstrap conductor (`core/bootstrap.py`) runs the D-025 team by itself on Ben's PC. It's a hidden, self-restarting scheduled task, and it builds the rest of Layer 1 from `docs/specs/layer-1-queue.json`. Ben is reached only by email: questions carry a secret reply code, and "STOP" halts everything. Chat sessions become optional. This replaces "Cowork plays the conductor for 1A and 1B" in `docs/specs/layer-1-design.md` §7.

## After the first live run

**D-035 (2026-09-29, proposed) Nothing goes live on fake tests alone.** Before any part of Forge that sends, spends, posts or runs unattended is switched on:
1. It passes a live test against the real services it uses.
2. Its possible damage is capped by hard limits in the charter.
3. Its first real cycle is watched and the evidence is shown to Ben.

This comes from the email-flood incident (`docs/incidents/2026-09-29-email-flood.md`).

**D-036 (2026-09-29, proposed) The rabbit-hole limit applies to every agent, including the one talking to Ben.** A fix-and-review loop gets at most 3 rounds or about 45 minutes. Then Ben gets a short report (what is fixed, what is left, a recommendation) and chooses whether to continue.

## Building around the clock

**D-037 (2026-09-30) Ben's approval to build Forge is standing and complete. Approvals are never a build blocker.** Ben's words: "I approve the building and implementation of the project, entirely… approvals should never be a blocker… we need building 24/7."
- **Protected-file changes that build Forge** (`core/`, `drills/`, `tests/`, `charter/`, `.github/`, specs, the docs, including this file) are pre-approved. The builder in charge (the supervisor or a chat session) applies the `human-approved` label and merges when **all** of these hold:
  1. The tests were written first by a different agent (Codex).
  2. The full core suite and the drills pass.
  3. A read-only Codex review raises no unresolved findings.
  4. CI's "core" check passes.

  This replaces the per-change approval in D-010 for build work. Forge's own conductor agents still never hold credentials that can apply the label (D-010 stands for them).
- **Loops don't wait on Ben.** This supersedes D-036's "Ben chooses whether to continue". After 3 rounds or about 45 minutes on one problem, the builder writes a short report and proceeds on its own best recommendation (a different approach, or re-scope and move on). It doesn't stop.
- **What still goes to Ben** is not approval to build. It's the things only he can do or that act in the world outside the build:
  - spending money, and paid subscriptions;
  - creating accounts, and human-only sign-up steps;
  - messages to anyone other than Ben;
  - legal commitments.

  These go in his queue while building continues on everything else.
- **A builder is always on.** An hourly "Forge supervisor" scheduled task works from `docs/SUPERVISOR.md`, so building continues whether or not a chat is open.

**D-038 (2026-09-30) The mutation gate is a rate, not "every mutant".** The design said both that "each mutant must be caught" and that "the rate must be at least `mutation_min`". The 1B plan reviewer rightly flagged the contradiction. Some mutants are equivalent (they don't change behaviour) and can never be caught, so a 100% rule is unworkable. The gate is: the kill rate on changed lines is at least `mutation_min` (0.8 in `charter/limits.json`), and every surviving mutant is reported to the Builder and Reviewer as evidence. Decided by the builder under D-037.

**D-043 (2026-10-01) Blockers: Forge fixes them itself, and anything only Ben can do comes as a ready fix.** Ben's words: "if there's a blocker, I just want you to tell me, prepare the fix, do it on your own if you can, but if you can't then prepare it and make it as easy as possible for me to fix it." No path pauses Forge or waits for Ben's reply except the Ben-only work of D-037 (money, accounts and credentials, messages to other people, legal commitments, a ledger unpark, hardware, admin prompts), and even those never stop other work. The layer gate merges itself under D-037 once CI passes. Ben gets one email when a blocker opens ("fixing it myself" or "needs you, ~2 min" with one paste line and a prepared script) and one when Forge has checked it fixed; the status page shows every blocker with Copy and Do it buttons. Spec: R66 in `docs/specs/bootstrap-conductor.md`; switched on by `self_fix` in `charter/limits.json`.
