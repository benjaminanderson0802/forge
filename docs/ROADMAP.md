# Forge: roadmap

Each phase ends with a **gate**: tests or drills that must pass before the next phase starts. Nothing is "done" on anyone's say-so. Drill numbers are assigned when each phase's implementation plan is written.

| Phase | What it adds | Gate | Status |
|---|---|---|---|
| **0. Trusted core** | Ledger, protected files, GitHub's checks, runner, budget rules | Drills 1–10 pass on the PC and on GitHub; gate-test pull request blocked | **Done 2026-09-29** |
| **F. Foundation** | Repo memory, grilled specs for Forge itself, anti-drift design, ways to reach Ben, build handoff to the PC | Ben approves the grilled specs; every later phase's plan traces to them | **In progress** |
| 1. Orchestrator loop | Invisible always-on service; cycles plan, execute, verify and merge; anti-drift guards; Ben's queue, digest email and status page | Full cycle from the ledger alone; kill switch mid-cycle; daily cap; stall detector; coverage map; weak-test check; live build of a small project with no human help | Planned (`docs/superpowers/plans/2026-09-29-forge-layer-1.md`, to be revised after the grilling) |
| 2. Auditor + Challenger | Codex audits every submission read-only; blocker claims must survive a Challenger | Planted bug caught; fake "impossible" blocker overturned | Not started |
| 3. Account Factory + hands | Sign-ups prepared up to the human-only step; browser and computer use in their own space, never on Ben's screen while he's active | CAPTCHA-bypass attempt refused; spend over the cap stopped; no input sent while Ben is active | Not started |
| 4. Learner | Finished tasks become reusable skills, promoted only with evidence | Bad skill rejected; skill replays a past task | Not started |
| 5. Scout + Optimizer | Finds new tools and free models, trials them in a sandbox, rolls back regressions | Poisoned plugin caught; regression rolled back; stale skill retired | Not started |
| 6. Venture runner | Spec-to-launch template for every venture; weekly report; GitHub App identity for pushing | First venture reaches its first paying customer | Not started |

## Venture order

1. Tariff refunds: time-sensitive, needs licensed broker partners.
2. Whop clipping.
3. 9x12 postcards, then restaurant refunds as an add-on to it.
4. Truck dispatch, after an attorney reviews the agreement.
5. Government supply, after SAM.gov, CAGE and DIBBS registration.
6. Motel pricing, after the Cloudbeds partner application is approved.
7. Utility audits.
8. Property tax: backtest by 2027-01-31, file during Colorado's appeal window (May 1 – Jun 8, 2027).
9. Handcrafted goods: once a carpenter is found.

Full timeline with dates: `docs/source/forge-master-build-plan.md`.
