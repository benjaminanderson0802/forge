> Snapshot of https://claude.ai/code/artifact/6695a7ba-015e-4919-a5e9-7f2728e77ff8 taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Forge Master Build Plan

Sep 28, 2026 · @Benjamin Anderson

## Where things stand

**Phase 0 complete (Sep 29, 2026).** Every program is installed; the core lives in `C:\Users\benja\Forge` with drills 1–10 passing on your PC and in GitHub's checks; n8n runs at `localhost:5678`; the repo `benjaminanderson0802/forge` is public with `main` protected (admins included). Gate test passed: a pull request that touched `tests/acceptance/` failed the protected-path check and could not merge.

Nothing essential was lost. Every plan lives on claude.ai, and the original Python core, with drills 1–5 passing, survived in the cloud session and was re-sent as `forge-core.zip`.

| Item | State | Next |
| --- | --- | --- |
| Forge plan and 8 venture docs | Intact on claude.ai (links at the bottom) | This doc ties them into one build order |
| Trusted core (Python: ledger, protected paths, CI checks, runner, drills 1–10, GitHub setup script) | Upgraded Sep 29; all 10 drills pass | Unzip into `Documents/Forge`, push to a private GitHub repo, run the gate test |
| PowerShell core rebuilt on your PC today | Works, but duplicates the Python core | Retired. Its best ideas were ported into the Python core (drills 6–10); the old OneDrive folder can be deleted |
| Software on your PC | Reinstalled Sep 29 | Done; virtualization (SVM) turned on in BIOS for Docker |
| The original chat | Gone | Not needed; decisions are in the docs |

**Next action:** start Layer 1 (the orchestrator loop). Sign-ins are done (Claude Code, Codex, Bitwarden), and the charter budgets are merged: ventures fund themselves from their own profit, and Forge uses at most 50% of the Max plan per day.

## Machine setup

One script, `install-forge-machine.ps1`, reinstalls everything. It skips anything already present and prints a summary, so it is safe to run twice.

| Program | Why Forge needs it |
| --- | --- |
| Git, GitHub CLI | The repo, branch protection, pull requests |
| Python 3.12, uv | Trusted core, drills, venture scripts |
| Node.js LTS | Claude Code, Codex CLI, n8n, MCP servers |
| PowerShell 7, Windows Terminal, VS Code | Shell and editor |
| Docker Desktop | Runs n8n, OpenShorts and sandboxed plugin trials |
| FFmpeg, yt-dlp | Clip cutting for the Whop clipping venture |
| Chrome | Claude in Chrome (browser automation) |
| Bitwarden app + CLI | Every venture password and API key; nothing secret lives in the repo |
| 7-Zip | Archives |
| npm: Claude Code, Codex, Bitwarden CLI, n8n | Agents, second-model auditor, secrets, workflows |
| pip: requests, pyyaml | Forge scripts |

**Run it:** open PowerShell in the folder with the script, then run `Set-ExecutionPolicy -Scope Process Bypass; .\install-forge-machine.ps1`. When it finishes, close and reopen the terminal, run it once more (so the npm packages install now that Node is on the path), then run `gh auth login`.

**Check:** `python drills/run_drills.py` inside `Documents/Forge` prints 10 passing drills.

## Services and accounts

Budgets are set in the Authority Charter; anything new that costs money is a hard stop that waits for you.

| Service | Purpose | Needed by | Cost |
| --- | --- | --- | --- |
| Claude Max | Manager, executors, Interviewer, computer use | Now (have it) | Existing plan |
| ChatGPT + Codex | Read-only Auditor on a different model | Layer 2 (have it) | Existing plan |
| GitHub | The ledger, CI, branch protection | Phase 0 (have it) | Free (private repo) |
| Bitwarden | Vault for every account Forge prepares | Phase 0 | Free tier; $10/yr for organizations |
| Docker Desktop | Runs n8n and sandbox trials | Layer 1 | Free for personal use |
| n8n (self-hosted) | Schedules, webhooks, venture loops | Layer 1 | Free self-hosted |
| Gmail (connected) | Venture inboxes, client email | Layer 3 | Free |
| Google Drive (connected) | Shared venture files, client uploads | Layer 3 | Free tier |
| OpenShorts, Higgsfield | Clip cutting, captions, video generation | Whop clipping | Free / existing credits |
| Windsor.ai (connected) | Reads social and ad stats into the ledger | Whop clipping, 9x12 | Existing plan |
| Lob or PostGrid | Print-and-mail API for 9x12 and letters | 9x12 | Pay per piece |
| USPS EDDM | Route-based mail drops | 9x12 | Postage only |
| Stripe | Invoices and payouts for all ventures | First paying venture | Per transaction |
| DAT or Truckstop API | Load boards | Dispatch | Paid subscription (hard stop) |
| FMCSA QCMobile API | Carrier authority checks | Dispatch | Free (key) |
| SAM.gov, CAGE, DIBBS | Federal registration and bidding | Government supply | Free |
| County assessor data | Comparable sales for appeals | Property tax | Free / public records |
| Cloudbeds partner API | Rate updates for motels | Motel pricing | Partner application |
| Green Button / UtilityAPI | Utility bill data with client consent | Utility audits | Per meter |
| Licensed customs broker partner | Files the refund claims | Tariff refunds | Revenue share |

The free-model and plugin sources the Scout watches stay in the main plan; Scout only adds a service after a sandbox trial and your approval of any new cost.

## Build plan: layers with gates

Each layer is built by Forge itself as contracts in the ledger, from Layer 1 on. A layer counts as done only when its gate passes in CI. A failed gate stops the next layer; nothing is skipped.

| # | Layer | What it adds | Gate (must pass) | Target |
| --- | --- | --- | --- | --- |
| 0 | Trusted core | Ledger, protected paths, CI checks, drills 1–5; plus frozen spec, protected-file restore, scope check, false-claim log and crash resume (drills 6–10, done) | Drills 1–5 pass locally and in CI; gate-test PR is blocked | Sep 28 – Oct 2 |
| 1 | Orchestrator loop | Plain-code runner: rebuilds the Manager from the ledger each cycle, launches fresh-context Executors under fixed identities, n8n schedules and kill switch | Drill 11 (a full Manager cycle runs from the ledger alone), drill 12 (kill switch stops a live cycle) | Oct 3 – 9 |
| 2 | Auditor + Challenger | Codex audits every submission read-only; Challenger tests every blocker claim before a contract can park | Drill 13 (planted bug slips past executor), drill 14 (fake "impossible" blocker) | Oct 10 – 16 |
| 3 | Account Factory + computer use | Prepares sign-ups up to the human-only step, stores credentials in Bitwarden, follows the Authority Charter | Drill 15 (attempt to bypass a CAPTCHA is refused), drill 16 (spend above the cap is stopped) | Oct 17 – 23 |
| 4 | Learner | Turns any finished task into a reusable skill, promoted only with evidence | Drill 17 (bad skill is rejected), drill 18 (skill replays a past task) | Oct 24 – 30 |
| 5 | Scout + Optimizer | Finds new tools and free models, trials them in a sandbox, auto-rolls back regressions | Drill 19 (poisoned plugin), drill 20 (regression is rolled back), drill 21 (stale skill is retired) | Oct 31 – Nov 6 |
| 6 | Venture runner | A spec-to-launch template every venture uses: Interviewer spec, loop, weekly report, human call queue | First venture reaches its first paying customer | From Nov 7 |

**Rule for every layer:** Forge may not edit core, drills, acceptance tests, the charter or CI. New drills for a layer are written first and merged by you with the human-approved label, then Forge builds against them.

## Venture timeline

&#91;embedded content: venture timeline · 11 ventures and milestones, Sep 2026 to Jun 2027\]

Each bar runs from the start of the build to the target for the first paying customer. Tariff refunds and Whop clipping start before the runner is live because their early work is outreach and account prep, which the Account Factory handles from Oct 23.

| Venture | Starts when | Blocked until |
| --- | --- | --- |
| Tariff refunds | Oct 19 | At least one licensed customs broker agrees to partner; refund windows start closing around Feb–Apr 2027, so this goes first |
| Whop clipping | Oct 26 | Whop and platform accounts ready |
| 9x12 postcards | Nov 2 | Spec interview settles 6x11 vs 9x12 for the first run; print API account |
| Government supply | Nov 2 | SAM.gov, CAGE code and DIBBS registration (weeks of processing) |
| Truck dispatch | Nov 9 | Attorney review of the dispatch agreement; load-board subscription approved |
| Property tax backtest | Nov 16 | Must prove results on past Colorado cases by Jan 31, 2027 |
| Restaurant refunds | Dec 7 | Add-on sold to 9x12 advertisers; restaurant submits its own disputes |
| Motel pricing | Dec 7 | Cloudbeds partner application approved |
| Utility audits | Jan 4, 2027 | Utility data access with client consent |
| Property tax filing | May 1, 2027 | Colorado appeal window, May 1 – Jun 8 |
| Handcrafted goods | Not scheduled | A local carpenter agrees to supply |

## Your action queue

These are the only steps Forge can't take for you. Everything else runs on its own.

**This week (Phase 0)**

- [ ] Run `install-forge-machine.ps1`, reopen the terminal, and run it once more
- [ ] `gh auth login`, then unzip `forge-core.zip` into `Documents/Forge`
- [ ] `python drills/run_drills.py`: confirm 5 passing drills
- [ ] `bash scripts/setup_github.sh <your-username>`, then run the gate test in the README
- [ ] Delete the PowerShell core once the Python drills pass
- [ ] Fill in budgets and spend caps in `charter/authority.md`, then merge it yourself with the human-approved label
- [ ] Create a fine-grained GitHub token for agents (contents and pull requests only) and store it in Bitwarden

**October (as layers land)**

- [ ] Merge each layer's new drills yourself before Forge builds it (about 10 minutes per layer)
- [ ] Do the Tariff refunds spec interview; take the calls with the broker partners Forge shortlists
- [ ] Finish the human-only step on each account the Account Factory prepares (ID checks, phone codes, CAPTCHAs, payment details)

**November onward**

- [ ] Spec interview for each venture as it comes up on the timeline (about 20 minutes each)
- [ ] SAM.gov entity registration (needs your identity) for Government supply
- [ ] Attorney review of the dispatch agreement before Truck dispatch goes live
- [ ] Approve any new paid subscription (load board, print API volume, utility data)
- [ ] Calls with hesitant-but-worthy clients that Forge puts in your queue
- [ ] Find a local carpenter when you want Handcrafted goods to start

## All Forge docs

| Doc | What it holds |
| --- | --- |
| [Idea-to-Completion Agent System: Research & Plan](https://claude.ai/code/artifact/b5b80da0-de0a-4362-ad89-aad13d78195f) | The Forge design, research basis, pricing strategy, auto ventures, drills |
| [Venture 1: Autonomous Truck Dispatch](https://claude.ai/code/artifact/7b1aa652-8f4d-47da-b7f2-71c460954cfa) | Dispatch, detention and IFTA add-ons |
| [Improving the 9x12 Postcard Method](https://claude.ai/code/artifact/1bbce8c7-00a7-4e51-b04e-2d4e0ba98b4f) | Venture 2: shared-ad postcards |
| [Venture 3: Autonomous Property Tax Appeals](https://claude.ai/code/artifact/e02ea6b8-6377-4956-b3f7-9d3fdd75d9d7) | Colorado first, May 1 – Jun 8, 2027 |
| [Venture 4: Restaurant Delivery Refund Recovery](https://claude.ai/code/artifact/af7a545f-0393-4440-98b7-02e0f0279202) | Add-on to 9x12; the restaurant submits |
| [Venture 5: Government Supply Orders](https://claude.ai/code/artifact/86ce6d3c-5a98-4bc8-83e3-2839696a52ae) | DIBBS bidding |
| [Venture 6: Tariff Refund Recovery for Small Importers](https://claude.ai/code/artifact/78f529b4-fd1d-4c16-bbec-ddc43887f306) | Broker partner files; you match clients to brokers |
| [Venture 7: Pricing Management for Independent Motels](https://claude.ai/code/artifact/99246beb-9f15-4e0f-8a65-70fef91c41a2) | Cloudbeds API, antitrust data separation |
| [Venture 8: Utility Bill Audits for Small Businesses](https://claude.ai/code/artifact/d893b5c9-ecc8-4686-90a8-d03b8c241f49) | Bill errors and rate-class savings |
