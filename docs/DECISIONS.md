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
