> Snapshot of https://claude.ai/code/artifact/7b1aa652-8f4d-47da-b7f2-71c460954cfa taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 1: Autonomous Truck Dispatch

Sep 28, 2026 · @Benjamin Anderson

## Bottom line

Forge acts as the dispatcher for independent owner-operator truckers. It finds and books loads, does the paperwork, and gets them paid, keeping 5–10% of each load's gross. At a typical $275,000 gross per truck a year and a 7% fee, that's about $19,000 per truck a year (about $370 a week). 10 trucks earn about $190,000 a year and 25 trucks about $480,000.

Why it fits:

- **Low AI adoption.** Transportation and warehousing sit under 10%.
- **Heavy repetitive work.** The job is load searching, checking brokers, paperwork and invoicing.
- **Loyal customers.** Carriers stay as long as the loads keep coming.

By my estimate, most of the work can run autonomously. The remaining human work:

- phone negotiation on loads that can't be booked online or by email
- a monthly legal and compliance check

Three things are non-negotiable:

- **Staying a bona fide agent of each carrier,** so no broker authority is needed.
- **Verifying every broker,** because freight fraud is the industry's biggest current threat.
- **Never letting one load be offered to two carriers.**

## How dispatch works today

A human dispatcher spends the day on a phone and a load board, doing the same steps for every truck, every day. Owner-operators are a large, underserved market:

- FMCSA counted 922,854 independent contractors in late 2023.
- Each grosses $200,000–$350,000 a year over about 94,000 miles.
- 85–90% of new owner-operator businesses fail within a few years, mostly from cash-flow and cost-tracking problems ([AtoB](https://atob.com/blog/owner-operator-statistics)).

| Step | What a dispatcher does by hand | What Forge replaces it with |
| --- | --- | --- |
| Find loads | Scrolls DAT and Truckstop for each truck's location, equipment and hours left | Searches boards through their APIs for every truck at once, using live position and hours from the truck's electronic logbook (ELD) |
| Price | Guesses from experience | Compares each offer to market rates for the lane, including the trip back |
| Vet the broker | Often skipped when rushed | Checks the broker's operating authority, age, credit and payment speed, and fraud flags, on every load |
| Book | Calls the broker, negotiates, sends the carrier packet | "Book Now" through the API, or email booking. Phone negotiation goes to a human queue |
| Confirm | Reads the rate confirmation by eye | Checks every field against what was agreed: rate, stops, times, detention terms |
| Run the load | Check calls to driver and broker | Tracks the truck through the logbook data and sends updates to the broker automatically |
| Get paid | Collects proof of delivery, invoices, submits to factoring | The driver's proof-of-delivery photo triggers the invoice and factoring submission |
| Plan ahead | Rarely | Plans each truck's next 2–3 loads to cut empty miles (16.7% on average) |

The market right now (DAT, week of Sept 13–19, 2026) is tight, which helps carriers negotiate:

- **Spot rates per mile:** van $2.96, reefer $3.59, flatbed $3.55.
- **Loads per truck:** van 11.2, reefer 19.1, flatbed 40.5.
- **Diesel:** above $6 a gallon for the first time on EIA record ([DAT report](https://www.ajot.com/news/dat-truckload-market-report-sept-1319-2026-spot-rates-climb-on-higher-diesel-prices)).

## Legal setup: an agent, not a broker

Forge can dispatch without broker authority, which would otherwise require FMCSA registration and a $75,000 bond, only if it stays a bona fide agent of each carrier. FMCSA's final guidance sets the line ([Overdrive](https://www.overdriveonline.com/regulations/article/15540708/fmcsas-final-guidance-on-broker-authority-for-dispatchers)):

| Requirement | How Forge enforces it |
| --- | --- |
| A written agreement with each carrier, defining the dispatcher's duties | A signed dispatch agreement is the first step of onboarding. No agreement means no searching or booking for that carrier |
| No "allocating traffic": no discretion over which carrier gets a load | Carriers are segmented by equipment type and home region, so no two carriers compete for the same load. The trusted core rejects any attempt to offer one load to two carriers |
| Paid only by carriers, never by shippers or brokers | The only revenue is the carrier's fee on their own loads. Referral fees or kickbacks from brokers are refused |
| Acting in the carrier's name | Loads are booked under the carrier's own MC number and paperwork; Forge never appears as the carrier or as a broker |

Other requirements:

- **A business entity** (LLC) and business insurance (general and errors-and-omissions) for the dispatch company.
- **Identity checks.** FMCSA's financial responsibility rule, effective Jan 16, 2026, added identity proofing for new carriers and brokers. Only carriers who pass it are onboarded ([iDispatchHub](https://www.idispatchhub.com/fmcsas-new-carrier-fraud-prevention-rules-are-live-what-every-dispatcher-must-know-in-2026/)).
- **Texts to drivers.** They are clients who consent in the dispatch agreement, so automated texts to them are allowed.
- **Calls to brokers** are the gray zone. AI-voice calls count as "artificial" calls under the robocall law (TCPA), and many broker numbers are cell phones. Broker contact stays with Book Now, email and a human phone queue until a transportation attorney signs off on anything else.

I'm not a lawyer. Have a transportation attorney review the dispatch agreement and the carrier-segmentation rules once, before the first carrier signs.

## The autonomous loop

The venture runs two loops. A weekly one wins carriers; a continuous one dispatches every truck and improves itself from each load.

&#91;embedded content: Dispatch venture loop · acquisition + daily cycle\]

**Carrier acquisition (weekly)**

1. **Find.** Pull newly authorized carriers and small fleets from FMCSA data (the free QCMobile API plus daily new-authority lists), filtered by equipment and home region to fill open segments.
2. **Reach out.** Send a short email and a mailed postcard (reusing the 9x12 mail pipeline) with a concrete offer, such as a free first week or a lower fee for the first month.
3. **Onboard.** The signed dispatch agreement, identity check, ELD connection, factoring setup and carrier packet (W-9, insurance certificate, authority letter) all go through e-signature and upload links.

**Daily dispatch cycle (every truck)**

1. **Truck status.** Live position and remaining driving hours from the ELD API.
2. **Search and price.** Query DAT and Truckstop through their APIs for loads matching the truck's segment, then score each by rate per mile, the market rate for the lane and the next load out of the destination.
3. **Vet the broker.** Authority age, credit and days-to-pay, fraud flags, and a contact domain that matches the registration. Fail any check and the load is skipped.
4. **Book.** Book Now through the API, or by email from the carrier's dispatch address. Loads worth negotiating by phone go to the human queue with a one-line target rate.
5. **Check the rate confirmation.** Every field is compared with what was agreed; a mismatch goes back to the broker automatically.
6. **Track and deliver.** Position updates go to the broker from ELD data, and the driver gets stop details and reminders by text.
7. **Get paid.** The proof-of-delivery photo triggers the invoice to the broker or factoring company. Forge's fee is invoiced to the carrier weekly.
8. **Learn.** Update lane, broker and timing scores.

**How it improves itself.** Each week tests one change against the baseline:

- **What changes:** the lane mix, the minimum acceptable rate, how far ahead loads are planned, the broker shortlist, and the outreach offer to new carriers.
- **What's scored:** revenue per truck per week, empty-mile share, days-to-pay, carrier retention and hours in the human queue.
- **What happens next:** winners become defaults. A segment with idle trucks triggers recruiting for that region; a segment with too many trucks pauses it.

## Tool stack and data sources

Every step uses an official API or a service the carrier already has. Forge builds only the glue and the decision rules.

| Job | Service | Access |
| --- | --- | --- |
| Load search and Book Now | [DAT APIs](https://www.dat.com/resources/api-integration) (Load Board, BookNow, Tracking, Freight Posting) | Developer portal account plus a DAT subscription |
| Second load board | [Truckstop API integrations](https://marketplace.truckstop.com/t/api-integrations) | Truckstop subscription plus integration |
| Truck position and driving hours | ELD APIs such as [Samsara](https://developers.samsara.com/docs/rest-api-overview) or Motive | The carrier grants access at onboarding |
| Carrier and broker lookup | [FMCSA QCMobile API](https://mobile.fmcsa.dot.gov/QCDevsite/docs/qcApi) (authority, insurance, safety) | Free API key |
| Broker credit and days-to-pay | Load-board credit data, plus a dedicated vetting service if needed | Part of the subscriptions |
| Getting paid | The carrier's factoring company | The carrier's account; Forge submits the paperwork |
| Agreements and carrier packets | E-signature and upload links | Standard e-sign service |
| Email booking | A dedicated dispatch domain, with email authentication and multi-factor login | Your domain |
| Driver texts | SMS provider, with consent in the dispatch agreement | Standard SMS API |
| Ledger, audit and learning | Forge's trusted core | Already built |

The per-truck software cost depends on subscription tiers. Check exact pricing when opening the accounts; I haven't priced them here.

## Economics

Revenue scales with trucks, not hours. At the midpoint (a $275,000 gross truck at a 7% fee), each truck earns about $19,250 a year.

The inputs:

- **Owner-operator gross:** $200,000–$350,000 a year ([AtoB](https://atob.com/blog/owner-operator-statistics)).
- **Typical dispatcher fee:** 5–10% of gross load revenue ([iDispatchHub](https://idispatchhub.com/what-percentage-do-freight-dispatchers-charge-2026/)).

| Fee | $200k truck | $275k truck | $350k truck |
| --- | --- | --- | --- |
| 5% | $10,000 | $13,750 | $17,500 |
| 7% | $14,000 | $19,250 | $24,500 |
| 10% | $20,000 | $27,500 | $35,000 |

At the midpoint, 5 trucks earn about $96,000 a year, 10 about $193,000, 25 about $481,000 and 50 about $963,000.

Those are fee revenue before costs:

- load-board subscriptions
- insurance for the dispatch company
- SMS and email
- Claude usage

The edge over human dispatchers is capacity. A person handles a limited number of trucks. Forge's limit is load-board API limits and the human phone queue, and the learning loop keeps shrinking that queue.

The best pitch to carriers is outcome-based: an introductory fee below the usual 5–10% range, no monthly minimum, and a weekly report of rate per mile and empty-mile share.

## Risks and guards

Freight fraud is the risk that can end this venture. Cargo theft losses reached about $725 million in 2025, up 60% on 2024, and strategic theft by deception rose 1,475% from 2022 to 2024 ([TruckingInfo](https://www.truckinginfo.com/digital-cover-features/cargo-thefts-new-playbook-strategic-fraud-double-brokering-and-cybercrime-hit-trucking)). An autonomous dispatcher is an attractive target, so its guards are hard-coded rules, not judgment calls.

| Risk | How it happens | Guard (enforced by the core, not by an agent) |
| --- | --- | --- |
| Fake or double broker | A fraudster posing as a real broker books the carrier's truck, then steals the freight or doesn't pay | Every broker is checked against FMCSA and credit data. Contact details must match the registration; new or mismatched brokers are rejected |
| Mid-haul reroute | A spoofed email tells the driver to deliver somewhere else | Delivery changes are never taken by email alone. They need a call-back to the broker's registered number, which is a human-queue item |
| Phishing and account takeover | Fake load-board login emails steal credentials | Multi-factor login on every account, a dedicated domain, and no credentials ever typed from links in emails |
| Acting as a broker by accident | One load offered to two carriers, or payment from a shipper | Segmentation by equipment and region; the core rejects double-offers; revenue only from carriers |
| Carrier non-payment of fees | The carrier stops paying weekly | Fees billed weekly on delivered loads; dispatch pauses automatically after a missed payment |
| Carrier churn | Loads or rates disappoint | The weekly report shows rate per mile against the market; a falling score triggers review of that truck's lanes |
| Bad rate confirmation | A missing detention clause or wrong rate | Every field is checked against the agreed terms before the driver moves |
| Cash-flow failure for the carrier | 85–90% of new owner-operators fail | Onboarding checks that factoring is set up; target new carriers with some operating history, not only brand-new authorities |

## What stays human, and the drills this venture adds

The human work is small and queued; nothing waits on you except the items below.

| Human task | When | Why it can't be automated yet |
| --- | --- | --- |
| One-time setup: LLC, insurance, load-board accounts, attorney review of the agreement | Before the first carrier | Legal identity and signatures |
| Phone negotiation on loads worth more by phone | Queued, each with a target rate | AI-voice calls to broker cell numbers are a TCPA gray area |
| Verified call-back on any delivery change | Rare | Fraud guard; must be a real person on the registered number |
| Carrier escalations the agent scores as high-value | Rare | Keeps top carriers happy |

Before this venture goes live, it adds these drills to Forge's list. Each must pass in a sandbox:

1. A load is offered to two carriers → the core rejects the second offer.
2. A broker with authority younger than the threshold, or a mismatched email domain → the load is skipped and logged.
3. A delivery-change email from an unverified sender → the driver is told nothing changes; a call-back task is queued.
4. A rate confirmation with a lower rate than agreed → flagged and sent back before dispatch.
5. A carrier misses a weekly fee payment → dispatch for that carrier pauses automatically.
6. A carrier without a signed agreement → no search or booking runs for that carrier.
7. An agent tries to accept a payment or referral fee from a broker → rejected by the core.

The first milestone is 3 carriers in one equipment segment, run for 2 weeks with every drill passing. Then scale by segment.

## Add-ons: detention recovery and fuel-tax filing

Two add-ons raise revenue per truck without new customers, and both reuse data Forge already collects.

**Detention and extra-fee recovery.** Detention costs trucking about $15 billion a year, and small carriers get paid on fewer than half of their detention invoices ([Innovative Logistics](https://innovativelogisticsgroup.io/profitability-and-cost-control/driver-detention-costs-the-industry-15-billion-a-year-how-small-carriers-build-a-real-accessorial-strategy-in-2026/)). Forge already has the evidence: logbook timestamps for arrival and departure at every stop, and the rate confirmation's detention terms.

1. Detect every stop over the free time in the rate confirmation.
2. Send the broker a detention invoice with timestamps within their billing window.
3. Chase unpaid invoices, and add lumper and layover charges the same way.
4. Fee: a share of what's collected. The loop learns which brokers pay, and dispatch scoring downgrades those that don't.

**Quarterly fuel-tax (IFTA) filing.** Carriers file fuel tax by state every quarter. Forge has the miles by state from logbook GPS and fuel purchases from fuel cards, so it prepares the return automatically for a flat low fee. The carrier (or their licensed tax filer) submits it.

**Drills added:**

1. A stop within free time → no detention invoice.
2. A detention invoice without logbook timestamps → blocked.
3. A fuel-tax return where miles don't reconcile with logbook data → flagged, not filed.

## Sources

- [FMCSA's final guidance on broker authority for dispatchers](https://www.overdriveonline.com/regulations/article/15540708/fmcsas-final-guidance-on-broker-authority-for-dispatchers) (Overdrive)
- [FMCSA's new carrier fraud prevention rules, 2026](https://www.idispatchhub.com/fmcsas-new-carrier-fraud-prevention-rules-are-live-what-every-dispatcher-must-know-in-2026/) (iDispatchHub)
- [What do freight dispatchers charge? 2026](https://idispatchhub.com/what-percentage-do-freight-dispatchers-charge-2026/) (iDispatchHub)
- [Owner-operator statistics 2026](https://atob.com/blog/owner-operator-statistics) (AtoB)
- [DAT Truckload Market Report, Sept 13–19, 2026](https://www.ajot.com/news/dat-truckload-market-report-sept-1319-2026-spot-rates-climb-on-higher-diesel-prices) (AJOT)
- [DAT APIs](https://www.dat.com/resources/api-integration) (DAT)
- [Truckstop API integrations](https://marketplace.truckstop.com/t/api-integrations) (Truckstop)
- [Samsara REST API overview](https://developers.samsara.com/docs/rest-api-overview) (Samsara)
- [FMCSA QCMobile API](https://mobile.fmcsa.dot.gov/QCDevsite/docs/qcApi) (FMCSA)
- [Cargo theft's new playbook](https://www.truckinginfo.com/digital-cover-features/cargo-thefts-new-playbook-strategic-fraud-double-brokering-and-cybercrime-hit-trucking) (TruckingInfo)
- [The rise of "Book it now"](https://www.ccjdigital.com/business/article/14940049/the-rise-of-book-it-now-options-across-freight-networks) (CCJ, 2020)
- [AI adoption by industry 2026](https://axis-intelligence.com/ai-adoption-by-industry-statistics/) (citing Census BTOS)
- [FCC: TCPA applies to AI-generated voices](https://www.fcc.gov/document/fcc-confirms-tcpa-applies-ai-technologies-generate-human-voices) (FCC)
