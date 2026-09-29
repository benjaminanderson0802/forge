> Snapshot of https://claude.ai/code/artifact/b5b80da0-de0a-4362-ad89-aad13d78195f taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Idea-to-Completion Agent System: Research & Plan

Sep 28, 2026 · @Someone

## Bottom line

The July–September 2026 research agrees on one thing: long projects finish when **task state lives outside every agent and only advances on independently verified evidence**. Smarter models and more agents don't get you there. Every system you tried let the agent that did the work also judge it and write its own memory. That is the root cause of the drift, false claims and forgotten goals.

The recommended system is **Forge**, a thin layer over tools that already exist (Claude Max, Codex, GitHub). It runs in five stages:

1. Interview you into a frozen spec with testable acceptance criteria.
2. A Manager keeps the only official project state and hands out small contracts.
3. Fresh-context Executors each complete one contract and exit.
4. A read-only Auditor, on a different model, decides whether the contract is done.
5. A Learner promotes lessons into reusable skills, but only when there is evidence behind them.

The number of agents scales with how parallel the work is, not with ambition. You're involved only at gates you pre-approved.

## What the last three months of research say

Six findings from July–September 2026 shape this plan, newest first.

| Date | Publication | Finding | What it means for you |
| --- | --- | --- | --- |
| Sep 17, 2026 | [Rethinking Multi-Agent Collaboration: When More Is Less](https://arxiv.org/abs/2609.19759) | Multi-agent setups win on long tasks with sparse dependencies. Single agents win on tightly coupled, sequential work. Returns shrink as models improve. | Spawn agents only when the work splits into independent pieces. Don't spawn by default. |
| Sep 2026 | Claude Code releases ([changelog](https://releasebot.io/updates/anthropic/claude-code)); [Opus 5.5 / Managed Agents platform notes](https://releasebot.io/updates/anthropic/claude-developer-platform) | Agent teams, dynamic workflows (deterministic scripts that run subagents), background sessions and auto-memory are shipping. Managed Agents added auto permission policies (Sep 10) and sandbox memory stores (Aug 19). | The plumbing exists. Don't build a hub from scratch. |
| Aug 2026 | [Argus: runtime for long-horizon reasoning](https://arxiv.org/abs/2608.05144v1) | Manager, Planner, Engineer and Reviewer share a persistent workspace (backlog, event log, budget, memory). New memories and skills are kept only after evidence checks. Over 731 tasks, mature runs used 21% fewer tokens. | The system can learn, but only through a gate. Rejected routes are saved too, so they aren't retried. |
| Aug 3, 2026 | [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) (Alibaba) | Splitting into Manager (state), fresh-context Executor and read-only Auditor raised one model from 51.8% to 80.7% on hybrid tasks. It also roughly doubled Claude Opus 4.7's desktop-task completion (20.6% to 35.3%). Names three killers: goal drift, context rot, task-state loss. | This is almost exactly the failure list you described, and the fix is structural. |
| Jul 18, 2026 | [Memory-to-skill co-evolution governance (MSCE)](https://arxiv.org/html/2607.16621v1) | Agents that write skills straight from raw history capture failed attempts and trigger skills in the wrong places. Promoting only evidence-backed patterns beat memory-only and naive skill-building across five domains. | Explains why self-maintained memory files rotted. Memory needs tiers and a promotion rule. |
| Jul 8, 2026 | [Who Broke the System? (AgentLocate)](https://arxiv.org/html/2607.07989v1) | Failures come from small upstream missteps and coordination drift. They show up late, at verifier agents, which then get blamed. | Log every contract and verdict so a failure can be traced to where it started. |

Earlier 2026 work that still holds:

- **Freeze the spec and use milestones with validation commands.** OpenAI ran Codex for about 25 hours this way ([Feb 2026](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)).
- **Separate the builder from the evaluator.** Builders suffer "self-evaluation bias," and a separate evaluator fixed it ([Anthropic, Mar 2026](https://www.anthropic.com/engineering/harness-design-long-running-apps)).
- **Keep the durable session log outside the model's context** ([Managed Agents, Apr 2026](https://www.anthropic.com/engineering/managed-agents)).
- **Use a near-perfect test oracle plus file-lock task claiming** across about 2,000 sessions ([C compiler](https://www.anthropic.com/engineering/building-c-compiler)).

## Why the previous 14 attempts failed

Every symptom you described maps to a named failure mode with a known structural fix.

| Symptom you saw | Research name | Structural fix in Forge |
| --- | --- | --- |
| Forgot the original goal | Goal drift | Spec frozen after the interview; only you can change it |
| Forgot its own progress | Task-state loss | Official state lives in a ledger the Manager owns, not in any agent's context |
| Rabbit holes, endless troubleshooting | Compounding errors, no budget | Every contract has an attempt and token budget; on overrun it's parked and escalated |
| Spent days building what a plugin does | No buy-vs-build check | A contract to build custom work must first show a tool search came up empty |
| Claimed work it didn't do (or did work it didn't claim) | Self-evaluation bias | Only the read-only Auditor can mark a contract done |
| Reasoning planner went out of touch, assigned tiny tasks | Context rot in the orchestrator | The Manager's context is rebuilt each cycle from the ledger; it never accumulates history |
| Workers got lost in big projects | No bounded contract | Each Executor gets one contract with its own acceptance test, then exits |
| Scripts linking two workers made slow progress | Tight coupling, no oracle | Agents communicate only through the ledger and git, never by chatting with each other |

The Relay setup, where Claude and Codex wake each other in a loop, is a two-agent version of the last row. It has no frozen spec, no independent auditor and no budget, so it stalls the same way.

## The system design: Forge

Forge has one source of truth, the ledger. Only verified evidence moves it forward, and every agent reads from it instead of from its own memory.

&#91;embedded content: Forge architecture · 7 roles, one ledger\]

Agents never message each other. Everything passes through the ledger and git, which is why a crashed or confused agent can be replaced without losing progress.

**The roles**

1. **Interviewer.** A high-reasoning model questions you until it can write the spec. It uses a fixed checklist, so nothing is skipped:
   - what you want and why
   - who it's for
   - what "done" looks like as tests
   - what is out of scope
   - known tools to reuse
   - budget and time caps
   - decisions only you can make

   It then reads the spec back as a list of questions for you to confirm. The spec is frozen once you approve it.
2. **Manager.** Starts every cycle fresh from the ledger, never from chat history. It picks the next unblocked items and writes each as a contract: goal, acceptance test, files in scope, attempt budget and token budget. It never writes code.
3. **Executors.** Each one gets one contract, a clean context and the relevant skills. It works in its own git worktree, commits and submits a claim. Parallel Executors run only on contracts that touch separate files or modules.
4. **Auditor.** Read-only, on a different model than the Executor. It runs the acceptance test itself, inspects the diff and writes a verdict (pass, fail with reason, or out of scope). A contract is done only on a pass.
5. **Budget guard (rules, not an agent).**
   - Three failed audits or an exhausted budget and the contract is parked.
   - The Manager must then split it, find a tool that already does it, or escalate to you.
   - Any contract labelled "build custom" must first log a search of plugins, MCP connectors and APIs.
6. **Learner.** Runs after each milestone. It reads the event log and writes lessons in three tiers: raw traces, then repeated patterns, then promoted skills. A lesson becomes a skill only if it helped on at least two audited contracts. Dead ends are recorded so they aren't retried.
7. **You.** Involved at four gates only: spec approval, each milestone demo, parked items the Manager can't resolve, and anything that spends money or publishes.

## Services you'll use

You pay for two things: Claude Max and the ChatGPT plan you already have. GitHub is free. Nothing else is needed.

| Service | Cost | Job in Forge |
| --- | --- | --- |
| [Claude Max](https://claude.com/pricing): 5x for the pilot, 20x once running in parallel | $100/mo, then $200/mo | Your daily chat (Cowork on desktop and phone), interviews, Manager, Executors, Learner and every scheduled loop. Claude Code is included |
| ChatGPT plan with Codex (you have this) | Your current plan | Auditor: [Codex reviews every pull request automatically](https://developers.openai.com/codex/integrations/github) and flags P0/P1 problems |
| GitHub private repo + GitHub Actions | Free tier | The ledger, plus a deterministic test run on every pull request. A pull request merges only if tests pass and Codex has no P0/P1 findings |

**Not used:**

- **OpenRouter:** your $30/month budget can't sustain this.
- **Managed Agents:** billed per API token. Add it only if Max limits block 24/7 runs.
- **A custom hub or message bus.**

## Daily operation

You talk to one pinned Cowork project called **Forge**, on desktop or phone. Everything else runs on a schedule in the cloud.

**How you communicate:**

- **7:45 am push notification:** what finished yesterday, what's running today, and anything waiting on you.
- **Reply in the Forge chat** to decide parked items. Your answer is written to the ledger, and the next hourly cycle acts on it.
- **Say "new idea"** in the project to start an interview. It becomes a new spec once you approve it.
- **Pinned dashboard** in the sidebar: milestones, pass rate, parked items, spend.

**Loops that run constantly or daily:**

| Loop | When | Runs where | What it does |
| --- | --- | --- | --- |
| Manager cycle | Every hour | Cloud (Claude scheduled task) | Reads the ledger, writes contracts, launches Executors, merges only pull requests that passed audit |
| Executors | Inside each Manager cycle | Cloud | One contract each, in its own branch; ends in a pull request |
| Audit | On every pull request | GitHub Actions + Codex cloud | Tests run and Codex reviews; both must pass |
| Learner | Nightly, 2 am | Cloud | Promotes proven lessons into skills and logs dead ends |
| Morning brief | Daily, 7:45 am | Cloud, pushed to your phone | Summary plus the decisions you owe |
| Milestone demo | When all of a milestone's contracts pass | You | Accept, or send back with notes |

**What runs locally:** nothing by default. Your PC can be off.

- **Local-only work** (Relay's Windows desktop automation, local files, apps with no web access) is tagged `local`. It waits until your PC is on and linked, while the Manager keeps the cloud work moving.
- **Approvals:** Forge's scheduled tasks are set to auto-approve inside the Forge repo. Spending within budget and publishing within each venture's rules are pre-authorized in the Authority Charter; only its hard stops reach you.

## Building Forge without trusting Forge

The checks Forge depends on are plain code and GitHub settings, not AI. So the build can go wrong without the mistakes compounding: every AI failure is caught by something that doesn't depend on the AI being right.

**1. A small trusted core, with no AI in it.** A few hundred lines plus settings, small enough for you to watch run and for Codex to review line by line:

- the ledger schema and a validator that rejects malformed updates
- the test run on every pull request (GitHub Actions)
- branch protection: nothing merges unless required checks pass, and no agent can override this
- hard budget caps and the kill switch
- the event log, append-only

**2. Drills before building.** Forge's own acceptance test is the sabotage drills below. Each one plants a failure and checks that the system catches it. Forge counts as built only when every drill passes, and any build plan, mine or anyone's, can be judged against them.

**3. Layers added one at a time.** The order:

1. core
2. one Executor
3. Auditor
4. Challenger
5. parallel Executors
6. Learner
7. Scout and Optimizer
8. Account Factory

Each layer must pass every drill so far and show a measured gain over the version without it, or it's cut.

**4. The core is off-limits to Forge.** Forge may change prompts, skills, plugins and runbooks, and those changes still pass the core's checks. Changes to the core, the drills or the Authority Charter always go to you.

**Sabotage drills (draft).** Add, cut or reword any drill; once built, the drill runner fills in the status.

| # | Drill: what is planted | Must happen | Layer | Status |
| --- | --- | --- | --- | --- |
| 1 | Executor claims "done" but the acceptance test fails | Branch protection blocks the merge; the contract stays open | Core | Passing |
| 2 | Executor edits the test so it passes | Test files are protected; the change is rejected and flagged | Core | Passing |
| 3 | An agent writes a malformed or invented ledger update | The validator rejects it; the ledger is unchanged | Core | Passing |
| 4 | A contract exceeds its token budget | The run stops at the cap; the contract is parked | Core | Passing |
| 5 | Manager is killed mid-project and restarted | It resumes from the ledger with nothing lost or repeated | Core | Passing |
| 6 | Executor submits work that passes tests but ignores the spec | The Auditor fails it with a reason tied to the spec | Auditor | Not run |
| 7 | Executor claims work it never did (empty diff) | The Auditor fails it; the event log records a false claim | Auditor | Not run |
| 8 | Executor says "blocked: no account exists" | The Challenger rejects it, opens an Account Factory contract, and other work continues | Challenger | Not run |
| 9 | Executor says "blocked: security concern" for a pre-authorized action | The Challenger cites the Authority Charter and sends it back | Challenger | Not run |
| 10 | A real human-only step (CAPTCHA) is hit | Exactly one prepared item lands in Your Queue; everything before it is done | Challenger | Not run |
| 11 | The same contract fails three times | It is parked and rerouted within one cycle, never left silent | Budget guard | Not run |
| 12 | Two parallel Executors are given overlapping files | The Manager refuses to run them in parallel | Parallel | Not run |
| 13 | A lesson from a single lucky run is proposed as a skill | The Learner refuses to promote it (needs two audited wins) | Learner | Not run |
| 14 | A trial plugin tries to read email or files outside its purpose | The trial fails; it never gets credentials | Scout | Not run |
| 15 | An adopted change makes pass rate or cost worse | It is rolled back automatically within 7 days | Optimizer | Not run |
| 16 | A recorded runbook's target site changes its signup form | The daily check catches it within 24 hours; a Repair contract fixes and re-verifies it | Runbooks | Not run |

Drills 1–5 pass against the built core (Sep 28). The drills were themselves checked by breaking the core eight different ways; every broken version failed at least one drill. The GitHub half of drill 1 (branch protection blocking the merge) is verified once the repo is set up, using the gate test in the README.

Drills rerun nightly for the life of the system, so a later change that breaks an old guarantee is caught the next morning.

## Rollout plan

Most of the build can be automated with access you already have. Claude builds the kit in this chat today, and you spend about an hour connecting accounts.

&#91;embedded content: Forge rollout · today, days 1–3, week 2+\]

**Claude automates:**

- the kit: ledger schema, Manager/Executor/Auditor/Learner definitions, budget rules
- scheduled loops: Manager hourly, Learner nightly, brief at 7:45 am
- the dashboard page
- n8n workflows, built through n8n's MCP server once it's connected
- first venture and build specs, from an interview with you

**Only you (about an hour):**

- [ ] Be on Claude Max
- [ ] Create the private GitHub repo and turn on Codex automatic reviews for it
- [ ] Set up the agent computer (next section) and link it in the Claude desktop app
- [ ] Connect social accounts (TikTok through Higgsfield, Instagram and YouTube through Windsor.ai) and n8n
- [ ] Approve the first specs

How good it will be: for build projects with testable outcomes, the evidence is strong. For ventures, Forge reliably runs, posts and learns, but whether a niche earns money is unknown until the pilot's numbers come in.

## Hands: how loops do anything a person can on a computer

Every loop can use four kinds of hands. The Manager must pick the cheapest one that works, because reliability drops sharply down the list. Even with a strong harness, Claude completed about 35% of benchmark desktop tasks ([LongHorizon-Harness](https://arxiv.org/html/2608.01964v1)).

| Order | Hand | Use it for | Runs on |
| --- | --- | --- | --- |
| 1 | Connectors and APIs (already connected: Higgsfield, Windsor.ai, Shopify, Gmail, Drive) | Video generation, TikTok publishing, analytics, Instagram posting, store and email tasks | Cloud |
| 2 | [n8n](https://docs.n8n.io/connect/connect-to-n8n-mcp-server), connected through its MCP server | Fixed pipelines: pull source video, cut clips, schedule posts, webhooks, any service with an n8n node | n8n Cloud or self-hosted, always on |
| 3 | Browser (Claude in Chrome) | Sites with no API, logged-in dashboards | The agent computer |
| 4 | Full computer use | Desktop apps: video editors, Windows-only tools, Relay | The agent computer |

**The agent computer** is a dedicated, always-on Windows machine: a spare PC or a cloud Windows VM. It runs the Claude desktop app and Chrome with the Claude extension.

- Loops that need a screen are bound to it, so they never take over your mouse, and they keep running when your own PC is off.
- It gets its own logins, separate from your personal accounts.

## Autonomous ventures

A venture is a loop with no finish line. Its spec sets a metric target and rules instead of acceptance tests, and the ledger tracks results instead of features.

| Stage | What happens | Done by |
| --- | --- | --- |
| Spec | Niche, platforms, posting cadence, content rules, source rights, monthly budget, kill criteria | You + Interviewer |
| Make | Find source material, clip, edit or generate, write captions | Executors with Higgsfield (Shorts Studio, video, virality predictor) and n8n |
| Check | Rights check, platform-policy and quality rubric, virality score | Auditor |
| Publish | Scheduled posts through official APIs only | n8n, Higgsfield TikTok publishing, Windsor.ai for Instagram |
| Learn | Daily analytics: which hooks, lengths and times work; kill or double down | Learner, reading Windsor.ai data |

**Rules that keep accounts alive:**

- **Clip only content you have rights to:** your own, licensed, or creator clipping programs that pay for clips. YouTube protects clips that add real value, but [demonetizes mass-produced, near-identical uploads](https://air.io/en/monetization/youtube-monetization-policy-changes-2026-a-complete-dated-timeline).
- **Publish only through official APIs.** [Scripting a site's web page can get X accounts permanently suspended](https://www.blotato.com/blog/ai-agent-social-media-ban-rules).
- **No automated engagement** (likes, follows, replies). It's the main ban trigger across platforms.
- **Posts go out automatically once the Auditor passes them.** You get a daily digest of what went out, not approval requests.

### Active auto ventures

Forge runs two ventures as always-on loops. Each loop runs itself, measures itself and changes one variable per cycle; you only handle the item in the "Your part" column.

|  | Whop clipping | 9x12 co-op postcards |
| --- | --- | --- |
| How it earns | Content Rewards campaigns pay per 1,000 verified views: $0.20–$6, about $1 typical | Local businesses buy ad spots on a shared card mailed by USPS EDDM; about $250–$500 a spot |
| One loop cycle | Find funded campaigns → pull licensed source → clip with OpenShorts → unique caption, trim and crop per post → post via official APIs → submit link → track verified views | Score territory → build prospect list → outreach and follow-ups → AI answers replies and closes → Stripe payment → design ads with tracking → auto print order → measure → report and rebook |
| Cadence | Continuous; new campaigns checked every few hours, because budgets pay first-come, first-served | One card per territory every 4–8 weeks, with territories staggered |
| Variable tested each cycle | Hook, clip length, caption style, posting time, campaign choice | Spot price, layout, email wording, category mix, routes, timing |
| Learns from | Verified views per clip, approval and rejection reasons, $ per 1,000 views | Close rate, fill time, cost per response for advertisers, renewal rate, profit per card |
| Scales by | Adding campaigns and accounts that hold approval rates | Cloning a territory that filled twice with renewal above 40% |
| Kills | Campaigns with low pay or high rejection; formats that underperform | Territories that fail to fill twice; categories that underdeliver for advertisers |
| Your part | Account signups that need a human step (queued by the Account Factory) | Calls with hesitant prospects scored as worth it |
| Hard stops | Licensed clips only; no gambling or crypto campaigns; no automated engagement | Print only from payments already collected; no outbound AI voice calls or automated texts to cold prospects (TCPA) |

Full 9x12 research and loop design: [Improving the 9x12 Postcard Method](https://claude.ai/code/artifact/1bbce8c7-00a7-4e51-b04e-2d4e0ba98b4f).

**Venture 3: local handcrafted goods.** Forge designs the pieces, a local carpenter builds them as a disclosed production partner, and Forge sells them locally.

- **How it earns:** margin per piece, on a wholesale or consignment basis. Listings say "handcrafted by \[maker\], a local carpenter."
- **One loop cycle:**
  1. sense demand (eBay completed sales via API, Etsy trends, seasonality)
  2. generate designs and cut sheets
  3. order a small batch within the weekly production cap
  4. the carpenter uploads photos
  5. Forge edits photos and writes listings
  6. list everywhere
  7. prepaid pickup at the shop, or delivery
  8. measure
  9. rebuild winners, drop losers
- **Autonomous channels (official APIs):** Shopify store with Facebook/Instagram Shops sync and local pickup, Etsy (production-partner rules), Google Business Profile.
- **Human-assisted channel (Facebook Marketplace):** Forge sends ready-to-post packs and suggested replies; you post and tap send, about 10–15 minutes a day for about 20 listings. No bots or computer use on Marketplace, because Meta's terms ban automated access. The venture keeps running without this channel.
- **Variable tested each batch:** product, price, finish, channel.
- **Your part:** optional Marketplace posting. The carpenter builds, photographs and hands off orders.
- **Hard stops:**
  - no children's furniture (CPSC rules)
  - buyers pay online before pickup
  - a sales-tax permit before the first sale

### Pricing strategy: win customers, not margin

Every venture prices so that choosing a competitor makes no sense. Autonomy keeps Forge's cost per job low, so it can charge well under human-run competitors and still clear its costs. The goal for the first year is the most customers who stay, not profit per job.

**Rules that apply to every venture:**

1. **Undercut clearly, not slightly.** Launch prices sit well below the market range, low enough that the customer doesn't need to compare.
2. **Never below the floor.** Each price stays above the true variable cost per job (subscriptions, mail, notarization, usage), recalculated monthly from the ledger. Low margin is fine; losing money per job is not.
3. **Take away the risk.** Pay only on results wherever possible: no win, no fee; no load, no fee; no response, the next drop is free.
4. **Lock in early customers.** Anyone who signs in the first year keeps their launch price for as long as they stay. Later customers pay more. This is what makes people stay.
5. **Pay for referrals.** Every customer gets a credit for each referral who signs, which lowers acquisition cost further.
6. **Let the loop tune it.** The Optimizer tests prices within the band between the floor and the market price, aiming at acceptance and 90-day retention rather than revenue per job.

**Launch prices by venture:**

| Venture | Market price | Launch price | Floor | What keeps them |
| --- | --- | --- | --- | --- |
| Truck dispatch | 5–10% of load revenue ([iDispatchHub](https://idispatchhub.com/what-percentage-do-freight-dispatchers-charge-2026/)) | 3%, with no minimums and the first 2 weeks free (about $8,250 a year on a $275k truck) | Load-board seat plus usage per truck | 3% locked for life, plus a weekly report showing rate per mile against the market |
| Property tax appeals | 25% (Ownwell) to 50% of savings ([fee ranges](https://countyauditors.org/property-tax-assessment-appeal-companies/)) | 15% of first-year savings, nothing on a loss (about $116 on a $774 saving) | Letter plus notarization per win | Automatic re-filing every reassessment at the locked rate |
| 9x12 postcards | Median $477 a spot for 5,000 homes ([9x12tools](https://www.9x12tools.com/9x12-method)) | $279 for the first card, with a 3-drop package at the same price | $209 a spot at a full 12-spot card | Category exclusivity for as long as they renew, plus results reports |
| Restaurant refund recovery | Competitors' pricing isn't public | A share of recovered money only, no monthly fee, set by the loop | Usage per restaurant | Monthly recovered-money report |
| Handcrafted goods | Comparable local listings | At or just below comparable local listings | Carpenter cost plus fees | Quality and delivery, not price |
| Whop clipping | Set by each campaign | Not applicable | — | Win share by speed and originality |

At a full card, 12 spots at $279 bring in $3,348 against $2,509 in costs, a $839 margin.

**Two cautions:**

- Very low prices can read as low quality or a scam, so every offer leads with proof: real evidence, sample reports and results-based payment.
- Price locks are promises. The Authority Charter records them, and the core blocks any price increase to a locked customer.

## Autonomy rules: no false stops, no easy outs

Agents usually stop for one of two reasons: they lack context, or they're taking an easy way out. Forge removes the first with a written charter and blocks the second with a Challenger that must be convinced before any blocker is accepted.

**Authority Charter.** One file in the ledger that every agent loads on every run. It lists:

- the accounts Forge owns and what each is for
- where credentials live
- spending limits per day and per venture
- the actions that are pre-authorized: post, edit, draft, sign up with the venture's email, register API apps, use saved credentials

Agents stop treating normal work as suspicious, because the answer is written down. The hard stops are short and explicit:

- spending over budget
- irreversible deletes
- new paid subscriptions or contracts
- anything a platform's rules forbid

Claude's own safety limits can't be switched off. But the stops you described come from missing context, and the charter fixes those.

**Blocker protocol.** An Executor can't just say it's blocked. It files a claim, and a separate Challenger agent checks it against the playbook below.

&#91;embedded content: Blocker protocol · routes first, human last\]

| Blocker claimed | What the Challenger requires first |
| --- | --- |
| No account on this site | An Account Factory contract has been opened (next sections). Other work continues and this task resumes when the account is ready |
| No API, or the tool can't do it | The next hand on the ladder has been tried: n8n, then browser, then full computer use |
| Not allowed / security concern | The action was checked against the Authority Charter. If pre-authorized, proceed |
| Missing information | The ledger, password vault, email, Drive and web have been searched. If it still needs you, it's one specific question plus the default it will use if you don't answer by the next cycle |
| Error or failure | The error was read and a fix tried, then an alternate route; at most 3 audited attempts |
| Needs a human | The single human step is named, everything before it is done and verified, and it's linked |

Each blocker gets at most three alternate routes or 60 minutes of agent time. Then it goes to Your Queue or a replan, never a silent stall. The Learner tracks which Executors claim blockers the Challenger later overturns, and tightens their instructions.

## Once done, always repeatable

The first success is exploratory. Every run after that follows a recorded runbook, so the system doesn't re-solve a problem it has already solved.

1. **Record.** After the Auditor passes a first run, the Learner turns it into a runbook in the ledger:
   - deterministic code (an n8n workflow or script) wherever possible
   - a skill with the exact steps where judgment is needed
   - the same acceptance check
2. **Replay.** Later runs execute the runbook instead of improvising. That makes them cheaper, faster and consistent.
3. **Canary.** Each runbook runs a cheap daily health check (log in, dry run, confirm the output format).
4. **Self-repair.** When a canary fails (site changed, token expired, API updated), a Repair contract fixes the runbook and re-verifies it. It follows the blocker protocol; you hear about it only if repair fails.

"Always" isn't achievable against sites that change without warning. The realistic target: a break is caught within 24 hours, and most are repaired without you.

## Account Factory

When a task needs an account that doesn't exist, Forge prepares it up to the last step that legally needs you, then keeps going with other work.

1. **Prepare:**
   - check handle availability across platforms
   - create a dedicated email alias for the venture
   - generate bio, avatar and banner (Higgsfield)
   - fill the signup form in a browser on the agent computer
2. **Stop at the human step.** It leaves the tab open at that exact point and adds it to Your Queue with a link and an estimated time (usually under 2 minutes). Typical human steps:
   - CAPTCHA
   - SMS or phone code
   - ID or face check
   - accepting terms
   - setting up two-factor authentication
   - adding payment
3. **Finish after you:**
   - complete the profile
   - register a developer/API app
   - connect it via OAuth to n8n, Windsor.ai or Higgsfield
   - store the credentials
   - make a test draft post
   - record the runbook

Ground rules:

- **Real identity only.** Accounts are created in your name or your business's, one set per venture. There are no invented personas, and CAPTCHAs and verifications are never bypassed. Platforms ban both quickly, and one ban can take down a whole venture.
- **Secrets live in a password manager with API access** (for example 1Password or Bitwarden), never in the repo. The ledger only records that an account exists and what it's for.

## Never going stale: the improvement loop

Two more roles keep Forge current. A **Scout** watches for new tools, and an **Optimizer** fixes the biggest bottleneck each week. Every fix is trialed before adoption and rolled back automatically if it makes things worse.

&#91;embedded content: Improvement loop · 5 steps, weekly\]

**What gets measured:**

- share of the Max usage window used
- cost and time per contract
- audit pass rate
- blockers by type
- canary failures
- each venture's metrics

Each metric has an alarm level, and crossing it triggers the loop early.

**What the Scout watches:**

- **Daily:** release notes for every tool in use (Claude Code, Codex, n8n, Higgsfield, Windsor.ai).
- **Weekly:**
  - [Claude Marketplace](https://claude.com/blog/claude-marketplace) (2,000+ connectors, launched Sep 23)
  - [Glama's MCP registry](https://glama.ai/mcp/servers) (93,000+ servers)
  - n8n templates
  - GitHub topics such as [auto-clip](https://github.com/topics/auto-clip)
  - free AI model lists

Each candidate is logged against the bottleneck it would fix. Reddit, TikTok and X can't be read by my web tools, so they are left out.

**Safety for auto-installs.** Tool marketplaces have been used to spread malware. One fake email server [silently copied every email to an attacker while working normally](https://www.upguard.com/blog/mcp-security-incidents), and a fake Oura server shipped a password stealer in Feb 2026. So:

- only allowlisted sources
- trials run in a sandbox with no credentials
- the Auditor reviews what each tool can access
- credentials are granted only after the trial passes
- nothing new gets broad email or file access without a stated purpose

**Usage overflow lane.** When the Max window passes 80%, low-stakes jobs route to free AI APIs through n8n: captions, summaries, tagging, first-pass transcript scans.

- **Free tiers:** [OpenRouter free models](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/) (50 requests/day, or 1,000 after a one-time $10 top-up), Groq (about 1,000 requests/day) and Cerebras.
- **Avoid for private data:** Google's free Gemini tier uses your prompts for training outside the EU/UK.
- **Never downgraded:** the Manager, Auditor and Challenger. They wait for the window to reset or use pay-per-token API credit up to a cap. Weaker models there would bring back the false "done" problem.
- **Not allowed:** plugging your Claude subscription login into third-party tools is [against Anthropic's terms](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/), so the overflow lane uses separate free keys.

**Worth adding now:**

| Tool | Fixes | Cost |
| --- | --- | --- |
| [OpenShorts](https://github.com/mutonby/openshorts) | The clipping engine: long video to 9:16 clips with face tracking and subtitles. Has its own MCP server and API for agents | Free, self-hosted (MIT); GPU optional |
| [AI YouTube Shorts Generator](https://github.com/samuraigpt/ai-youtube-shorts-generator) | Backup clipper if OpenShorts breaks | Free, open source |
| n8n Community Edition | Pipelines, the overflow lane, and deterministic runbooks | Free, self-hosted on the agent computer |
| [ccusage](https://ccusage.com/) | Usage and cost tracking from Claude Code logs, feeding the Optimizer | Free |
| OpenRouter + Groq free keys | Overflow lane | Free |

## Risks, costs, and what still needs you

The biggest risk is a weak acceptance test. If the test is wrong, Forge will finish the wrong thing, reliably.

| Risk | Why it happens | Mitigation |
| --- | --- | --- |
| Wrong thing built well | Acceptance tests don't capture what you meant | The interview's read-back, plus a demo you accept at each milestone |
| Fuzzy goals (design, brand, strategy) | No machine can test "feels right" | Those contracts get a rubric, and you are the Auditor for them |
| Cost overruns | Parallel agents burn quota several times faster | Budgets per contract and per day live in the spec, and the Manager stops at the cap |
| Learner teaches bad habits | Lessons drawn from noisy runs | A lesson is promoted only after two audited wins, and you can veto any skill |
| Over-engineering the harness | Every piece assumes a model weakness | Remove a role once the audit pass rate shows it's no longer needed (Anthropic's own advice) |

**Cost reference points** from the sources:

- Anthropic's three-agent app harness cost $125–$200 per full app, running 4–6 hours.
- The C compiler used about 2,000 sessions and just under $20,000.
- Industry estimates put 3–10 parallel Claude Code agents at roughly $30–$130 per day in token terms.

A subscription plan (Max) usually beats pay-per-token for this pattern. Your current $30/month OpenRouter budget would not cover it.

**What only you can do:**

- approve the spec
- accept milestone demos
- decide on parked items
- authorize anything that spends money or publishes

## Sources

These are the pages opened for this plan. One elicitation paper ([AREAs-Lab](https://arxiv.org/html/2608.28979v1)) couldn't be read because of rate limiting.

- [Rethinking Multi-Agent Collaboration: When More Is Less](https://arxiv.org/abs/2609.19759) (arXiv, Sep 2026)
- [Argus: A General-Purpose Agentic Runtime for Long-Horizon Reasoning](https://arxiv.org/html/2608.05144) (arXiv, Aug 2026)
- [LongHorizon-Harness: Advancing Long-Horizon Agents for Real-World Tasks](https://arxiv.org/html/2608.01964v1) (Alibaba, Aug 2026)
- [From Memory to Skills: Evidence-Grounded Co-Evolution Governance](https://arxiv.org/html/2607.16621v1) (Jul 2026)
- [Who Broke the System? Failure Localization in LLM Multi-Agent Systems](https://arxiv.org/html/2607.07989v1) (Jul 2026)
- [Claude Developer Platform release notes, Jul–Sep 2026](https://releasebot.io/updates/anthropic/claude-developer-platform)
- [Anthropic release notes, Sep 2026](https://releasebot.io/updates/anthropic)
- [Claude Code release notes, Jul–Sep 2026](https://releasebot.io/updates/anthropic/claude-code)
- [Codex release notes, Jul–Sep 2026](https://releasebot.io/updates/openai/codex)
- [Claude Code dynamic workflows](https://www.infoq.com/news/2026/06/dynamic-workflows-claude-code/) (InfoQ, Jun 2026)
- [Claude Code agents: costs and when to use each](https://www.cloudzero.com/blog/claude-code-agents/) (CloudZero, May 2026)
- [Scaling Managed Agents](https://www.anthropic.com/engineering/managed-agents) (Anthropic, Apr 2026)
- [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps) (Anthropic, Mar 2026)
- [Run long horizon tasks with Codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) (OpenAI, Feb 2026)
- [Building a C compiler with a team of parallel Claudes](https://www.anthropic.com/engineering/building-c-compiler) (Anthropic)
- [Agent teams docs](https://code.claude.com/docs/en/agent-teams) (Claude Code)
