# Phase 6 design: Venture runner

> Source of truth for Phase 6 (`docs/ROADMAP.md`: "Spec-to-launch template for every venture; weekly report; GitHub App identity for pushing. Gate: first venture reaches its first paying customer"). It builds on Layer 1 (`docs/specs/layer-1-design.md`, `docs/specs/bootstrap-conductor.md` R1-R67) and Phase 2 (`docs/specs/phase-2-design.md`) and must not contradict them or `docs/DECISIONS.md`. Plan tasks: `docs/specs/phase-6-queue.json`. New decisions this design needs are listed at the end as **proposed** entries; they are not in `DECISIONS.md` yet. Where it touches Phase 3 (Account Factory, hands, spend caps), Phase 4 (Learner, runbooks) and Phase 5 (Optimizer) it names the interface it assumes; the plans must reconcile with those specs when they merge. Venture facts (what each venture sells, its blockers and dates) stay in `docs/source/venture-*.md`; this phase builds the machinery, not the businesses.
>
> The numbered sections below use the coverage-map format (`core/coverage.py`): each top-level list item in a `## N.` section is requirement `N.k`.

## Why (PURPOSE trace)

PURPOSE: "Some projects are autonomous ventures: businesses that run in a loop, earn money, and improve themselves", and "Ventures run on their own money: they spend only what they've earned." Ventures are where Forge touches the outside world: other people, money, public accounts. That is exactly where PURPOSE draws its lines:

- **Ben's money is protected.** No startup budgets (D-011). Spending, subscriptions, accounts, messages to other people, public posting and legal commitments are Ben's (D-037). Phase 6 therefore designs every venture action to be **requested by an agent and performed by plain code only after the approval its class requires**. Agents never perform external effects themselves.
- **Honest and legal.** Real identity, official APIs, no CAPTCHA bypass, none of the D-015 rejected approaches. Plain code rejects them; no agent is asked to judge them.
- **False claims.** "The venture earned money" and "we have a customer" are claims. Revenue is counted only when the payment provider says so, and the phase gate is checked against the provider, not against an agent's report.
- **Easy-way-out stops.** A venture blocked on something only Ben or a third party can clear (a licensed broker, an attorney, a registration) is marked waiting and the loop moves to the next venture, never stalls (D-033).

## 1. What already exists (do not rebuild)

- **The ledger** (`core/ledger.py`): hash-chained, append-only; `approve_spec` (Ben approves a spec), `usage`, Phase 2's `audit` and `challenge`, Phase 5's `change`. Identities in `roles.json`: `ci, forge-manager, forge-executor, forge-auditor, benjamin, forge-core` (and Phase 5's `forge-scout`, `forge-optimizer`). A `human` role exists and only Ben's identity holds it.
- **Ben's queue and channels** (`core/channel.py`, D-016, D-023): questions with reply codes and a default, instant kinds (a customer call needs him, a spend or subscription needs approval, a safety stop), the daily 8am digest, quiet hours, the status page (`core/status_page.py`, D-022), STOP (D-024).
- **The charter** (`charter/authority.md`, `charter/limits.json`): no startup budgets, hard stops (spend over budget, new subscriptions, irreversible deletes, anything a platform forbids), the rule that a venture may spend only from its own collected profit. Section 5 below makes that rule checkable.
- **The conductor** (`core/bootstrap.py`, `core/service.py`): runs while there is runnable work and sleeps until woken by a changed approval, a reply from Ben, a scheduled venture timer or the cap reset (D-007). Venture timers are the existing wake source; Phase 6 adds the timers.
- **Projects are their own local git repos with spec, tests, roles and a git-ignored ledger** (D-006); Forge already builds a project from its approved spec with no human code (Phase 1 gate, `projects/entry_ledger`). A venture's software (a clip pipeline, a postcard ordering flow, a dispatch tool) is built that way, from the venture's approved spec.
- **Ventures' source documents** (`docs/source/venture-1` to `venture-8`, `forge-research-and-plan.md` "Autonomous ventures", `forge-master-build-plan.md` "Venture timeline"): the loop stages (Spec, Make, Check, Publish, Learn), the rules that keep accounts alive, pricing (D-012), blockers and dates per venture. D-013 and D-015 list the approved and rejected ventures and approaches.
- **Phases 2 to 5** (in progress or planned): the Auditor, per-role scores, the Challenger (Phase 2); Account Factory, hands and spend-cap enforcement (Phase 3); runbooks and skills (Phase 4); metrics, the Optimizer and rollback (Phase 5).
- **Not there yet:** no venture spec format or validator, no action gate, no money ledger beyond token usage, no venture loop, no human call queue, no weekly report, no agent GitHub identity (D-006 deferred it "to the first layer that pushes to GitHub"), and no way to verify revenue.

## 2. Roles and identities

| Role | Engine | May write | Output shape |
|---|---|---|---|
| Interviewer | Claude, read-only plus Ben's channel | `ventures/<slug>/interview.json`, a draft `venture.json` | `S_INTERVIEW`: questions each with a proposed default; then a spec draft |
| Operator | Claude, acceptEdits in the venture's own worktree | files in the venture worktree; **action requests** (`requests/*.json`) | `S_OPERATE`: stage result plus zero or more action requests |
| Venture checker | Codex, read-only (the Phase 2 Auditor role, venture mode) | nothing | `S_VCHECK`: `verdict` (`pass`, `fail`, `needs_ben`), reasons, rule ids |
| Venture judge | plain code | `state/ventures/<slug>/` | rule checks, one-variable rule, kill criteria, caps |
| Executor | plain code | the only thing that performs an external effect | performs an approved action request through an official API adapter |
| Reporter | plain code, plus a Claude narrative that plain code validates | `ventures/<slug>/reports/` | weekly report |
| Token broker | plain code | none (returns a short-lived token to the caller process only) | GitHub App installation token |

- **Intent and effect are separate.** The Operator, the Interviewer and every other agent can only draft and request. The Executor (plain code, `core/venture_exec.py`) is the single place that posts, mails, orders, pays or pushes, and it refuses a request that lacks the approval of its class (section 4). No agent holds a credential for a venture account; the Executor fetches secrets from Windows Credential Manager (D-029).
- **Role instructions** live in protected files `agents/interviewer.md`, `agents/operator.md` and the venture mode of `agents/auditor.md`, read through `core/roles.role_text`. Ledger identities `forge-interviewer` (role `interviewer`) and `forge-operator` (role `operator`) are added to `roles.json` (protected); Executor, judge, reporter and broker events are applied by `forge-core`.
- **Engines are fixed by role (D-020).** The venture checker is Codex, independent of the Claude Operator whose output it checks. When Codex is capped or held, a Check waits; it never falls back to Claude, and nothing publishes without a passed Check.

## 3. The venture spec

- **A venture is a ledger contract of kind `venture`**, id `V-<slug>` (R1 id rule), plus a project repo at `projects/ventures/<slug>/` (its own git repo, D-006) holding `venture.json`, `runbooks/`, `requests/`, `outbox/`, `reports/`. A venture has no acceptance tests; its spec states a metric target and rules, and the ledger tracks results (research plan, "Autonomous ventures").
- **`venture.json` fields**, validated by plain code (`core/venture_spec.py`, `validate(spec) -> list[str]`, empty when valid):
  - `slug`, `name`, `source_doc` (a path under `docs/source/`), `order` (the position in the ROADMAP venture order);
  - `metric`: `{name, unit, target, window_days}` (for example paid advertisers per card, or verified views per week);
  - `stages`: the loop stages, each `{name, actor, class, canary}` where `class` is an action class from section 4 and `canary` names a cheap daily health check;
  - `cadence`: how often a cycle runs (per stage or per venture);
  - `channels`: each platform or provider used, with `official_api: true` and the account it needs;
  - `rules`: the rules that keep accounts alive (rights to source material, no automated engagement, official APIs only, content policy), each with an `id` the checker cites;
  - `budget`: `{startup_cents: 0, spend_rule: "venture_profit_only"}` (D-011); any other value fails validation;
  - `pricing`: the price band and rule from D-012;
  - `kill_criteria`: a list of `{metric, op, value, window_days}`;
  - `blockers`: each `{id, kind: "legal" | "account" | "partner" | "date" | "data", owner: "ben" | "third_party", clears_on: <what evidence>}`;
  - `human_calls`: when a call with a person is worth Ben's time (D-014);
  - `repos`: the GitHub repositories the venture may push to (section 10);
  - `self_refs`: emails, names and accounts that belong to Ben or Forge, so Forge's own payments are never counted as customers (section 11).
- **The validator rejects**, with the rule named: any stage or channel tagged with a D-015 rejected approach (`marketplace_bot`, `fake_persona`, `captcha_bypass`, `ai_cold_call`, `third_party_dispute_filing`, `unlicensed_customs_brokerage`, `competitor_price_pooling`, `automated_engagement`, `scripted_web_posting`, `resale_against_platform_rules`); a channel without `official_api: true`; a stage without a class or a canary; a `budget` other than profit-only; a `metric` without a target; and a spec with no `kill_criteria`.
- **The Interviewer** grills Ben to produce the spec, as PURPOSE step 1 and the research plan's Spec stage describe. It starts from the venture's `docs/source/venture-*.md`, pre-fills every field it can, and asks only what is missing, each question with its proposed default (D-023), by email reply, the status page or chat. Its draft must pass `validate` before Ben sees it. **Ben approves the spec** (ledger `approve_spec`, identity `benjamin`, **needs Ben**); after approval the spec is frozen like any frozen spec, and a change is a `spec_amend` request that needs Ben again.
- **A venture is `waiting` while any blocker is uncleared** (a licensed broker agrees to partner; an attorney reviews the agreement; SAM.gov, CAGE and DIBBS registration; a Cloudbeds partner application; a carpenter found). A blocker is cleared only by a ledger event from `benjamin` that names the evidence. The runner skips a waiting venture and works the next in order, so a blocked venture never stalls the others.

## 4. The action gate

- **Every external effect has a class**, declared by the stage and re-derived by plain code from the request's kind (`core/venture_gate.py`, `classify(request) -> auto | ask | forbid`). An unclassified or unknown request is `ask`. The classes:

| Class | Examples | Rule | Needs Ben |
|---|---|---|---|
| `draft`, `research` | writing copy, reading public data or an official API read | `auto` | no |
| `publish_public` | a post, a page, a public repo, a listing | `ask` | yes: standing approval per platform account and content template |
| `message_person` | an email, SMS, DM or call to anyone other than Ben, a reply to a reply | `ask` | yes: standing approval per message template and recipient class |
| `account` | sign-up, OAuth app, connecting a service | `ask`, always | yes: prepared up to the human-only step (Phase 3), then Ben |
| `spend` | any money out, subscription, API fee, print order, ad spend | `ask`, every time | yes: a named line item with a cap, or per item |
| `collect_payment` | a payment link, an invoice, a payout setup | `ask` | yes: standing approval per provider and price band |
| `legal` | a contract, terms, a registration, a partner agreement, a filing | `ask`, always | yes |
| `irreversible` | deletes that cannot be undone | `ask`, always (charter hard stop) | yes |
| forbidden | any D-015 approach, anything a platform's terms forbid | `forbid` | never offered |

- **A standing approval is a ledger event from Ben** (`venture` action, kind `approval_granted`, role `human` only; the ledger refuses it from any agent identity). It names the venture, the class, the exact template or line item, the recipient or account class, the daily and weekly caps, and an expiry (default 30 days). It is revoked by `approval_revoked` at any time, by reply, status page or by email `REVOKE <id>`. An agent cannot create, widen or renew one.
- **A request is performed only if plain code finds** an approval that matches it exactly: same class, same template with only its declared slots filled, a recipient inside the approved class, counts under the caps, an unexpired approval, a passed Check (section 7), and no STOP in force. A request outside any approval becomes a queue item for Ben with its default ("not sent", "not spent", "not published"). One yes can cover a template and its caps; it never covers a different template.
- **Defaults are stricter than the charter's pre-authorizations.** `charter/authority.md` pre-authorizes posting and sign-ups within a venture's spec. Phase 6 does not rely on that: the venture's first action in every class waits for Ben, which matches D-037 (Ben's list: spending, accounts, messages to other people, legal commitments), and the charter's pre-authorization is exercised only as the standing approvals he grants.
- **Replies from people.** Inbound replies are read by the Operator and drafted; a drafted reply is a `message_person` request. Until Ben grants a standing approval for a named reply class (default after he reviews the first 10 drafts), every reply draft waits in his queue as a one-line approve.
- **Dry-run first (D-035).** Every venture starts `dry_run`: the Executor writes the action to `outbox/` and performs nothing. Setting a venture `live` is Ben's yes (**needs Ben**) after one full cycle's outbox and evidence are shown to him. Live adapters are exercised once against the real service before the first live cycle, and that first live cycle is watched.
- **Records.** Every request, decision and execution is a ledger event (`venture` action kinds `request`, `decision`, `executed`, `refused`), with the approval id it relied on. A refusal costs nothing and is counted; an Operator that repeatedly requests forbidden or unapproved actions is scored like any false claim (Phase 2 scores).

## 5. Money

- **No autonomous spending.** Phase 6 gives Forge no way to spend: the Executor's `spend` adapters exist only behind an approval of class `spend`, and no venture is given a payment credential in an agent's reach. Money out always passes through Ben's yes, with a standing approval allowed only for a named line item he sets with a cap.
- **The profit rule is information plus a hard stop (D-011).** `core/venture_money.py` computes per venture `revenue_cents` (money actually received, never invoiced or pending), `costs_cents` (costs already paid, with receipts) and `profit_cents = revenue - costs`. Before any `spend` request reaches Ben, plain code attaches `covered: true | false` and the shortfall. An uncovered spend is a hard stop: a queue item with the shortfall and the default "not spent". A covered spend still needs Ben's yes.
- **Revenue and cost events carry provider evidence.** `venture` action kinds `revenue` and `cost` require a provider reference (payment or receipt id) and are written only by plain code from a **payment source** adapter (`PaymentSource.list_payments(since) -> [{id, amount_cents, currency, status, payer_ref, created, refunded, livemode}]`): Stripe through a read-only restricted key, a platform payout read (for example Whop) where its API allows, or, as a fallback, an entry Ben confirms in the queue with the provider reference (`record_payment`). A refund or chargeback writes a negative `revenue` event.
- **Cash before cost.** Where the venture's model allows it (the postcard method collects before printing), the loop orders a cost only after the matching revenue is received, and the spend request cites it.
- **Per-venture and daily caps** from the venture spec and `charter/limits.json` (`venture_messages_per_day`, `venture_posts_per_day`, defaults in code) bound volume independently of approvals.
- **Currency and rounding** are integer cents everywhere; no floats in money code.

## 6. The venture loop

- **One cycle is a ledger contract of kind `venture_cycle`**, id `VC-<slug>-<n>`, with the stages of the venture's spec as ordered tasks. The conductor runs a venture cycle when its timer is due (`state/venture_timers.json`, the scheduled venture timer D-007 already wakes on) and no build task has priority over a safety item. Venture work runs at below-normal priority and pauses while Ben is active, like other heavy work (D-019).
- **Stages.** Spec (once), then each cycle: **Make** (Operator produces drafts and requests), **Check** (section 7), **Publish** (the Executor performs approved requests; stages with class `message_person`, `publish_public` or `collect_payment` are covered by section 4), **Learn** (plain-code readers pull analytics and provider data into ledger metric rows; the Learner, Phase 4, and the Optimizer, Phase 5, consume them).
- **One variable per cycle.** A cycle plan names the single variable it changes (hook, price, wording, timing) as `{name, from, to}`; the judge rejects a plan that changes more than one. The result is attributed to that variable in the Learn stage.
- **Official sources only.** Readers and the Executor use official APIs and connectors (n8n, Higgsfield, Windsor.ai, Stripe and the like). The judge refuses a request whose channel is not `official_api` in the spec.
- **Canaries.** Each stage's `canary` runs daily (log in, dry run, confirm the output shape). A failed canary opens a repair task under the blocker protocol (D-031) and pauses only that stage. Where Phase 4 runbooks exist the loop replays them; before that, cycles are exploratory and every success is recorded for the Learner.
- **Kill criteria.** Each cycle the judge evaluates the spec's `kill_criteria` against ledger metrics. A hit auto-pauses the venture (`paused`, fully reversible) and asks Ben with the default "stay paused". Only Ben can `resume` or `kill` it; Forge never kills a venture on its own.
- **Venture order.** The runner takes ventures in ROADMAP order among those not `waiting` and not `paused`, and may run several if the caps allow. Tariff refunds are first in order and time-critical (refund windows close in early 2027), but they are `waiting` until a licensed customs broker partner agrees (**needs Ben**: contacting that broker is a message to another person).
- **The venture's software is built by Forge's normal pipeline** from its approved spec (a separate queue per venture, loaded after Ben approves that venture's spec), not by this phase.

## 7. The Check stage

- **A request that would publish, message or collect payment is checked before it can be performed.** Two checks, both required:
  - the **venture judge** (plain code) verifies the hard rules it can verify: a rights record for any source material (own, licensed or a program that pays for clips), no automated engagement, official API only, required disclosures present (such as an unsubscribe line on commercial email), message count and post count under the caps, recipient inside the approved class, and no D-015 pattern;
  - the **venture checker** (Codex, read-only, fresh session) judges what code cannot: platform policy and quality rubric, claims in the copy that cannot be backed (for example never promising a number of customers), mass-produced near-identical content. It answers `S_VCHECK`: `pass`, `fail` with reasons and rule ids, or `needs_ben`.
- **A plain-code failure cannot be overridden by the checker**, and a checker `fail` cannot be overridden by the Operator. `needs_ben` becomes a queue item with a default of "not performed".
- **Independence.** The Operator never sees the checker's reasons until a fail returns them as feedback to the next attempt, like the Layer 1 Reviewer.
- **Records.** Each Check writes a ledger event with the verdict, rule ids and run id. Check outcomes feed the Operator's score and the weekly report.

## 8. The human call queue

- **Calls are Ben's (D-014).** When the spec's `human_calls` rule matches (a hesitant but worthwhile client, a negotiation, an attorney or broker step), plain code creates a queue item of kind `call`, which is an instant kind under D-023 ("a customer call needs him").
- **The item is self-contained:** who, why the call is worth it, the conversation so far, a short brief and suggested points, the best times from the thread, the venture's price and rules, and the default ("Forge keeps the lead warm with the approved follow-up template and does not call"). Forge never phones anyone (D-015: no AI cold calls).
- **After the call** Ben answers with a one-line outcome by email reply or the status page; the outcome is a ledger event and feeds the loop. A call that is never answered is not retried more than once a day and never blocks the cycle.
- **Quiet hours** (23:00 to 07:00) hold a `call` item to the morning unless it is marked urgent by the spec (for example a filing deadline).

## 9. The weekly report

- **One report per venture per week** (`core/venture_report.py`), generated Monday 08:00 local, archived at `ventures/<slug>/reports/<yyyy>-W<ww>.md`, attached as a section of that day's digest (one email, so `mail_per_day` is respected) and shown on the status page.
- **Contents:** the metric against its target and window; revenue, costs and profit this week and to date (section 5, with provider references); the one variable tested and its result; pipeline counts (drafted, checked, refused by the gate, performed, awaiting Ben); account health and canary results; kill-criteria status; blockers and what clears each; human calls queued and answered; spend requests and their outcome; and the next cycle's planned variable.
- **Every number comes from plain code** reading the ledger and provider rows. A short Claude narrative may be added, and plain code drops it if any number in it does not appear in the data block (evidence over claims); the report is then sent as a table only.
- **Missing data is shown as missing**, never as zero or an estimate. A venture with no revenue shows "no revenue received", not 0 invented from a forecast.
- **Silence is also a report:** a venture that is `waiting` or `paused` appears with the reason and what would change it.

## 10. GitHub App identity for pushing

- **Why.** D-006 deferred "a separate GitHub identity for agents (GitHub App)" to the first layer that pushes to GitHub. Venture repos are that layer. Today pushes use Ben's own token; a venture must not.
- **The App.** A GitHub App `forge-agents`, installed on **selected repositories only: the venture repos listed in each `venture.json`**. Permissions: Contents (read and write), Pull requests (read and write), Metadata (read). No Administration, Actions, Workflows, Secrets, Issues-on-the-Forge-repo or organization permissions.
- **It is never installed on `benjaminanderson0802/forge`.** D-010 says Forge's agents must never hold credentials that can apply the `human-approved` label, and pull request write permission can apply labels. Keeping the App off the Forge repo keeps D-010 true by construction.
- **The token broker** (`core/gh_app.py`): `installation_token(repo) -> str` signs a short-lived JWT with the App private key and exchanges it for an installation token of at most one hour, kept in memory only, passed to git through an environment-based credential helper, and never written to disk, logs, the ledger or any prompt. The private key lives in Windows Credential Manager (`forge-github-app`) with the record copy in Bitwarden (D-029); agents never see it.
- **Guards in plain code before any push:** the repo is in the venture's `repos` list; it is not the Forge repo; the push is to a branch (a pull request), not to a default branch of a protected repo; the commits are authored as the App's bot identity; and `publish_public` rules apply if the push changes what the public can see (a public repo, a site deploy).
- **Failure and revocation.** A refused or failed mint is a capability failure (readiness check `github_app`), not a stop. Revoking the App's installation or key is one action for Ben at github.com; Forge's pushes simply fail closed.
- **Creating and installing the App is Ben's** (**needs Ben**): it is an account and permission grant with human-only steps (confirm creation, generate the key, choose repositories). Forge prepares everything before that step: the App manifest, the exact permission list, and the one command line for Ben to store the key. Creating each venture repo under Ben's account is also his, or an explicit yes.

## 11. The first venture and the first paying customer

- **Which venture is first.** The gate needs any one venture to reach a paying customer. Default by ROADMAP order among ventures that are not `waiting`: tariff refunds if its broker partner is agreed, otherwise the next whose model has customers who pay Forge (9x12 postcards, advertisers paying before printing). Whop clipping earns platform payouts for views, which are revenue but not a paying customer, so it counts for the gate only if Ben says so. Ben chooses (**needs Ben**), default "the first by order that is not waiting and has customers".
- **Verification, not a claim.** `python -m core.venture verify-first-customer <slug>` exits 0 only if all hold:
  - the ledger has a `revenue` event with a provider reference (section 5) for the venture;
  - plain code re-reads that payment from the provider with the read-only credential, and it is paid, amount above zero, not refunded, not a test-mode payment (`livemode` true), and in the venture's currency;
  - the payer is not in `self_refs` (Ben, his aliases, any Forge-controlled account), so self-dealing cannot pass;
  - the ledger holds the approved chain that led to it: a `request`, a Check `pass`, an `approval_granted` it relied on and an `executed` event for the message, post or payment link tied to that customer or campaign reference;
  - the venture was `live` with the first-cycle watch evidence on file (D-035);
  - Ben has confirmed "this is a real customer" in the queue (**needs Ben**, an event from `benjamin`, no default: the gate waits for it).
- **Evidence.** The command writes `state/gates/phase-6-first-customer.json` (provider ids, event hashes, the approval chain, amounts) and prints a one-line result. It never prints a secret.

## 12. Surfaces

- **CLI.** `python -m core.venture list|show <slug>|validate <slug>|status`, `python -m core.venture pause|resume <slug>` (resume needs Ben's answer), `python -m core.venture verify-first-customer <slug>`, `python -m core.venture report <slug> [--week <yyyy-Www>]`.
- **Ben's queue kinds** (each with its default, D-023): `venture_spec` (default not approved), `blocker_cleared` (default still waiting), `approval` (default not granted), `go_live` (default stays dry-run), `spend` (default not spent), `account` (default not created), `call` (instant), `resume` (default stays paused), `customer_confirm` (no default). `spend` and `call` are instant under D-023; the rest go to the digest.
- **Status page** (`core/status_page.py`): a "Ventures" section: each venture's state (`waiting`, `dry_run`, `live`, `paused`), its blockers, this week's metric against target, profit to date, pending approvals, standing approvals with expiry, canary health, and the last report. A "Revoke" control per standing approval.
- **Digest:** the Monday weekly reports; daily lines for refusals, pauses, canary failures and calls awaiting.

## 13. Gate drills

- **Mechanical drills** (fake agents, fake providers, fake clock, no network, in `drills/run_drills.py`, next free numbers at plan time): the spec validator rejects each D-015 pattern, a non-profit-only budget and a missing kill criterion; an agent identity cannot write `approval_granted`; a request outside any approval is refused and queued; an expired or over-cap approval is refused; a request for a different template than the approved one is refused; an uncovered spend becomes a hard-stop question with the shortfall; a covered spend still waits for Ben; revenue without a provider reference is refused; a refund writes a negative revenue event; a plan changing two variables is rejected; a kill-criteria hit pauses and asks; a `waiting` venture is skipped and the next runs; the weekly report's numbers equal the ledger and a narrative with an unsupported number is dropped; a dry-run venture performs nothing; the broker refuses the Forge repo and an unlisted repo and never logs a token.
- **Gate drill (a): a dry-run venture end to end.** `python -m core.gate_drills_p6 dry-run-venture`. Fixture `drills/fixtures/phase6/venture/` holds a small fixture venture (a spec, a source doc and a fake provider) with a planted trap in each class: a request to post without an approval, a message outside the approved template, a spend over venture profit, a forbidden `automated_engagement` stage in a variant spec, and a self-payment from an address in `self_refs`. The drill runs full cycles through the **real** conductor path, venture judge, gate, Executor (dry-run adapters) and the **real** Codex venture checker. It passes only if every trap is refused or queued, nothing external is performed, a legitimate approved request is written to `outbox/` with its approval id, the self-payment is not counted as revenue, one cycle's report numbers equal the ledger, and a `call` item is created for the planted hesitant-client case. A control run with the traps removed completes cleanly.
- **Gate drill (b): the App's reach.** `python -m core.gate_drills_p6 app-scope` (live; runs only after Ben has created and installed the App). It passes only if the installation's repository list contains no `benjaminanderson0802/forge`, its permission set is within section 10's list, a token mints, a test push to a throwaway venture repo creates a branch, a push to the Forge repo is refused by the guard and, tried directly with the token, by GitHub (403 or 404), the label endpoint on the Forge repo is refused, and a scan of `state/`, the ledger and the logs finds no token or key material.
- **Gate drill (c): the phase gate.** `python -m core.venture verify-first-customer <slug>` for the first venture (section 11). This is the ROADMAP gate itself and it passes on the real world, so it cannot be drilled with fakes; the mechanical and dry-run drills above are what must be green before Ben turns a venture `live` (D-035).
- **Evidence.** Each live drill writes `state/gates/phase-6-<drill>.json` and prints a one-line result; exit code 0 only on a pass.
- **Phase 6 is done** when the mechanical drills and the core suite pass, gate drills (a) and (b) pass on Ben's PC, and `verify-first-customer` exits 0 for a first venture, with the evidence attached to the layer pull request (a phase closes on its roadmap gate, D-039). Which date that lands on depends on Ben's steps below and on third parties, not on the build.

## 14. Build order and dependencies

- **P6A Venture spec, Interviewer, ledger** (`docs/superpowers/plans/2026-10-02-phase-6a-spec-interviewer.md`): the ledger contract kind `venture` and action `venture` (kinds `approval_granted` human-only, `request`, `decision`, `executed`, `refused`, `revenue`, `cost`, `cycle`, `paused`, `resumed`, `killed`, `report`, `first_customer`), identities, `core/venture_spec.py` and the validator, `S_INTERVIEW`, `agents/interviewer.md`, the blockers and `waiting` logic. No dependency on another phase.
- **P6B Action gate, Executor, money** (`...phase-6b-gate-money.md`, `depends_on` P6A): `core/venture_gate.py`, standing approvals, `core/venture_exec.py` (dry-run adapter, adapter interface), `core/venture_money.py`, `PaymentSource` and the Stripe read-only and manual adapters, the dry-run to live switch.
- **P6C Loop, Check, calls** (`...phase-6c-loop-check.md`, `depends_on` P6A, P6B): cycle contracts, timers, the one-variable rule, kill criteria, canary hooks, the Check stage (judge plus Codex venture mode, `S_VCHECK`), the human call queue.
- **P6D Weekly report and surfaces** (`...phase-6d-report-surfaces.md`, `depends_on` P6B, P6C): `core/venture_report.py`, narrative validation, the digest and status page sections, the `core/venture.py` CLI.
- **P6E GitHub App identity** (`...phase-6e-github-app.md`, `depends_on` P6A): `core/gh_app.py` broker with a fake HTTP layer in tests, the guards, the `github_app` readiness check, the manifest and exact steps for Ben.
- **P6F Gate drills and first-customer verifier** (`...phase-6f-gate.md`, `depends_on` P6B, P6C, P6D, P6E): the fixtures, `core/gate_drills_p6.py`, `verify-first-customer`.
- **What can be built now, in parallel:** P6A first; then P6B and P6E (they touch different modules); P6C after P6B; P6D after P6C; P6F last. Everything is buildable against fakes today.
- **What waits on another phase:** the Check stage needs the Phase 2 Auditor (fake until merged; the gate drill needs the real one); account preparation up to the human-only step and the enforced spend cap are Phase 3's (this phase's gate stays stricter and independent of it); runbook record and replay and canary repair are Phase 4's (loops are exploratory until then); metrics and tuning of cycle variables are Phase 5's (Phase 6 only supplies the rows).
- **What waits on Ben or third parties, not on code:** creating the GitHub App; payment-provider accounts and a read-only key; each venture's spec approval, blockers, `live` switch and first-customer confirmation (all listed under Needs Ben).
- **Starting the lane:** `python -m core.bootstrap init --lane p6 --layer phase-6 --tasks docs/specs/phase-6-queue.json --spec docs/specs/phase-6-design.md`. `init` refuses to replace a queue that still has tasks unless `--force` is given. A venture's own software gets its own queue and lane after Ben approves that venture's spec.

## Risks

- **An agent finds a path around the gate.** Mitigation: agents have no credential and no network write; the Executor is the only performer and checks the approval itself; the ledger refuses `approval_granted` from an agent identity; the mechanical drills include direct attempts; a drill checks the broker never leaks a token.
- **Standing approvals drift into blanket permission.** Mitigation: they name an exact template, recipient class, caps and an expiry; plain code matches slots exactly; a different template is a new question.
- **Revenue is faked or self-dealt.** Mitigation: revenue is written only from a provider read, re-read at verification, with `self_refs` and test-mode checks, and Ben's confirmation.
- **Platform bans from mass or near-identical content.** Mitigation: the Check stage, cadence and daily caps, official APIs only, one variable per cycle.
- **Legal exposure** (TCPA, CAN-SPAM, platform terms, licensing for refund filing, antitrust on pricing data). Mitigation: D-015 patterns are rejected by plain code; `legal` is always Ben's; unsubscribe and disclosure rules are judged in plain code; Forge never phones anyone.
- **Money-code defects.** Integer cents, provider references on every event, and the revenue and spend drills exist for this.
- **A venture's real-world blockers move the dates.** The runner skips waiting ventures; dates and windows (tariff refund windows, Colorado's May 1 to June 8 appeal window) are in each spec's blockers and shown on the status page.
- **Phase boundaries.** If Phase 3's spend-cap or Account Factory design differs from section 4, Phase 3 wins on mechanism and this spec's stricter ask-first default stays.

## Needs Ben

| Item | When | Default if no answer |
|---|---|---|
| Approve each venture's spec; any later spec amendment | after the interview | not approved |
| Clear each blocker (broker partner agreed, attorney review done, registration approved, partner application approved, supplier found) | the third party acts | still waiting |
| Contact any third party (a licensed broker, an attorney, a printer, a supplier, a prospect, a customer) | any `message_person` outside an approved template | not sent |
| Standing approval per class and template: public posting, messages, replies, payment links | first action in each class | not granted |
| Any spend, subscription or API fee; a standing line item with a cap | every spend request | not spent |
| Create any account (venture email alias, platform, payment provider, print API) up to and including the human-only step | an `account` request | not created |
| Create and install the GitHub App; store its key; create venture repos | before P6E goes live | pushes stay off |
| Provide a payment-provider read-only key, or confirm payments manually | before revenue can be counted | no revenue counted |
| Switch a venture from `dry_run` to `live` after the watched first cycle (D-035) | after one dry cycle | stays dry-run |
| Calls with clients (the `call` queue) | a `call` item | lead kept warm, no call |
| Contracts, terms, registrations, filings, any legal commitment | any `legal` request | not done |
| Resume or kill a paused venture | a kill criterion hit | stays paused |
| Choose the first venture for the gate | before P6F's live run | first by order that is not waiting and has customers |
| Confirm the first paying customer is real | after the verifier's other checks pass | none: the gate waits |
| Edits to protected files this phase adds (`roles.json`, `agents/*`, `charter/limits.json`) | build time | pre-approved to build under D-037; runtime changes need Ben |

## Proposed decisions (not yet in DECISIONS.md)

**D-064 (proposed) Agents request, plain code performs, and Ben's approval gates every outside effect.** In a venture, agents only draft and file action requests. The Executor performs a request only if it matches an approval of its class: public posting, messages to people, payment links, spending, accounts, legal and irreversible actions all wait for Ben, with a standing approval allowed only for an exact template, recipient class, cap and expiry that Ben grants himself. Agents cannot create or widen approvals. Ventures start in dry-run and go live only on Ben's yes after one watched cycle (D-035, D-037).

**D-065 (proposed) Forge never spends on its own; the profit rule is a hard stop, not a permission.** Every spend request goes to Ben with whether the venture's collected profit covers it (D-011); uncovered spend is a hard stop; covered spend still needs his yes unless he has set a named line item with a cap. Revenue counts only when the payment provider confirms money received.

**D-066 (proposed) Agents push to GitHub through a GitHub App that is never installed on the Forge repo.** The App has contents and pull request permissions on listed venture repos only; its key stays in Credential Manager; tokens are short-lived and in memory. This keeps D-010 true by construction and supersedes the deferral in D-006.

**D-067 (proposed) The first paying customer is verified against the payment provider, never by an agent's report.** The gate command re-reads the payment, excludes Forge-controlled payers and test mode, requires the approved action chain in the ledger and Ben's confirmation that the customer is real.

**D-068 (proposed) Weekly venture reports contain only numbers plain code read from the ledger and providers; a narrative with an unsupported number is dropped, and missing data is shown as missing.**
