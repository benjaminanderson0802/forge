> Snapshot of https://claude.ai/code/artifact/af7a545f-0393-4440-98b7-02e0f0279202 taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 4: Restaurant Delivery Refund Recovery

Sep 28, 2026 · @Benjamin Anderson

## Bottom line

The research changed this venture. DoorDash's terms forbid third parties from accessing the Merchant Portal and from submitting error-charge disputes on a restaurant's behalf ([DoorDash Help](https://help.doordash.com/en-us/merchants/article/what-are-order-error-adjustments)). So Forge can't file disputes for restaurants on DoorDash, and it won't, even though some competitors appear to.

The compliant version:

- **What Forge does:** detects every error charge, builds the evidence, pre-writes each dispute, and finds the causes so errors stop happening.
- **What the restaurant does:** its own manager submits each prepared dispute in one tap, about 5 minutes a day.
- **How it's paid:** a share of the money the restaurant actually recovers, and nothing in months with no recoveries.

The money at stake is real. Operators report 2.5–3% of total revenue tied up in delivery disputes, about 20% of their delivery profit ([Restaurant Business](https://www.restaurantbusinessonline.com/technology/restaurants-say-theyre-bearing-brunt-delivery-chargebacks)). But this is the least autonomous of the four new ventures, so I'd run it as an add-on for restaurants already buying 9x12 spots rather than as a standalone venture.

## Where restaurants lose money

Delivery apps charge restaurants when a customer reports a missing or wrong item, and many of those charges are disputable. Restaurants lose them mainly because the dispute windows are short and filing is tedious.

| Platform | Dispute window | How disputes are filed | Third-party filing |
| --- | --- | --- | --- |
| DoorDash | 14 days from delivery | One at a time in the Merchant Portal (Financials → Dispute Charge), by an Admin or Store Manager; resolved within hours. Heavy dispute volume can pause the button for up to 72 hours | Forbidden by the Portal terms ([DoorDash Help](https://help.doordash.com/en-us/merchants/article/what-are-order-error-adjustments)) |
| Uber Eats | 14 days from the adjustment notice under the contract (help center says 30 days), per a [UK source](https://bronze.vision/en/blog/refund-dispute-deadlines.html); US terms to be confirmed | Uber Eats Manager or email | Not confirmed; Uber's third-party applications page returned an error |

What it costs restaurants:

- **Revenue at stake:** 2.5–3% of total revenue tied up in delivery disputes.
- **Win rates:** one owner estimates winning about 60% of the time; a McDonald's franchisee contests about $500 a month per location and wins often.
- **Fraud:** chargeback rates on food and delivery orders rose 31% year over year in Q1 2023 (Sift) ([Restaurant Business](https://www.restaurantbusinessonline.com/technology/restaurants-say-theyre-bearing-brunt-delivery-chargebacks)).

California's AB 578 (effective Jan 1, 2026) requires delivery apps to give customers full cash refunds for wrong or undelivered orders. Operators worry more of that cost lands on restaurants ([Smith Allen Group](https://smithallengroup.com/insights/california-ab-578-delivery-app-refund-law-restaurant-impact/)).

## Access: what's allowed

Forge only uses official data routes and never logs into a restaurant's merchant portal. That rules out the easy but prohibited route competitors may use.

| Route | Allowed? | Use in Forge |
| --- | --- | --- |
| DoorDash Reporting API: payouts, transaction details, order details, cancellations, menu item errors, customer feedback | Yes, for merchants and authorized partners. Early access by application; current partners include Checkmate and Nextbite ([DoorDash Help](https://help.doordash.com/en-us/merchants/article/how-can-i-access-the-doordash-reporting-api)) | Apply to become a partner. Until approved, the restaurant connects through an existing partner or forwards its report exports |
| Uber Eats Marketplace APIs (store, orders, analytics) | Yes, with Uber's written approval; POS-style integration takes about 4–8 weeks ([Vorp Labs](https://vorplabs.com/agent-tools/uber-eats-cli)) | Apply; order-level data for detection |
| Report exports the restaurant emails to Forge | Yes; the restaurant shares its own data | Day-one fallback: a forwarding rule sends reports to a Forge inbox |
| Logging into the Merchant Portal as the restaurant | No; against DoorDash's Portal terms | Never |
| Submitting disputes for the restaurant | No on DoorDash; unconfirmed on Uber Eats | The restaurant's own manager submits |

Whether the DoorDash financial reports list error charges as separate line items isn't documented. Confirming that is the first task once API access is granted.

## The loop

Everything runs on Forge's side except the one tap to submit, which the platform's terms keep with the restaurant.

&#91;embedded content: Refund recovery loop · daily per restaurant\]

1. **Pull reports.** Daily, via the Reporting API or forwarded exports.
2. **Detect charges.** Every error charge and adjustment still inside its dispute window.
3. **Build the case.** The likely reason (for example, an item charged as missing that the order record shows was included), with the evidence and the pre-selected dispute reason.
4. **Daily queue.** Sorted by dollar value and closing deadline, sent to the manager's phone as one link per dispute.
5. **Manager submits.** Opens the portal and submits the prepared dispute; about 5 minutes a day.
6. **Track results.** Wins and losses from the payout reports.
7. **Prevent and learn.** Which items, times and staff shifts produce errors, turned into packing checklists and menu fixes. Stopping errors is worth more than disputing them.
8. **Bill.** A share of money actually recovered, from the payout data.

**How it improves itself:** it learns which reasons and evidence win on each platform, skips disputes that never win (to stay clear of DoorDash's rate limit), and measures whether prevention tips cut the error rate.

## Competition and pricing

This space already has AI-driven players, so the edge has to be price and compliance, not novelty. None of them publishes pricing ([Danetsoft](https://www.danetsoft.com/post/top-doordash-chargeback-recovery-solutions)).

| Competitor | Claim |
| --- | --- |
| [Voosh](https://www.voosh.ai/automated-delivery-dispute-resolution) | Automated dispute resolution; 500+ restaurant brands |
| KoreFi | Reports recovering 85%+ of disputed charges; independents and groups up to $10M revenue |
| Orders.co | Order and dispute management across 30+ POS systems |

Pricing follows the venture pricing strategy in the main plan:

- **Launch price:** 15% of money actually recovered, no monthly fee, no setup fee. Locked for life for first-year customers.
- **Floor:** API and usage cost per restaurant.
- **Bundle:** free for any restaurant that buys a 9x12 spot, which turns this into a retention tool for the postcard venture.

The 15% is a starting guess, since competitors' prices aren't public. The Optimizer tunes it against sign-up and retention.

## Economics

Each restaurant is a small account, so this only adds up at volume or as a bundle. At the one reported benchmark ($500 contested a month per location, about 60% won) and a 15% fee, a location pays about $45 a month, or $540 a year.

| Restaurants | Fee revenue a year |
| --- | --- |
| 25 | $13,500 |
| 100 | $54,000 |
| 250 | $135,000 |

Those figures rest on one operator's numbers; real volumes vary widely with delivery share. A restaurant doing most of its sales through apps (like the one reporting 2–3 chargebacks a night) is worth several times the benchmark.

Costs: API access (if DoorDash charges partners; not documented), SMS and email, Claude usage.

The larger value is indirect. It keeps restaurants inside the 9x12 loop and gives Forge a reason to talk to them every week.

## Risks, human steps and drills

The main risk is breaking platform terms, and the guards make that impossible by design.

| Risk | Guard |
| --- | --- |
| Violating DoorDash terms | Forge never holds portal credentials and never submits disputes; the core rejects any task that asks for either |
| Getting a restaurant's dispute button paused (heavy volume triggers a pause of up to 72 hours) | Queue only disputes above a win-probability threshold; cap daily volume per store |
| Weak or false disputes | Every dispute needs evidence from the order record; no evidence, no queue item |
| API access denied or delayed | Day-one fallback on forwarded report exports |
| Billing disputes | The fee is computed only from recovered amounts visible in payout reports |

**Human steps (the restaurant's, not yours):**

- the manager submits queued disputes, about 5 minutes a day
- a one-time report-forwarding or API connection

**Your steps:** the DoorDash Reporting API partner application and the Uber Eats developer application.

**Drills before launch:**

1. A task requests portal login or dispute submission → rejected by the core.
2. A charge older than its window → not queued.
3. A dispute without order-record evidence → not queued.
4. Daily queue exceeds the per-store cap → trimmed to the highest-value items.
5. A recovery appears in the payout report → exactly one invoice line at the locked rate.

## Sources

Uber's third-party applications help page returned an error, so Uber Eats' stance on third-party filing is unconfirmed.

- [Understanding error charges and disputes](https://help.doordash.com/en-us/merchants/article/what-are-order-error-adjustments) (DoorDash Help)
- [How to access the DoorDash Reporting API](https://help.doordash.com/en-us/merchants/article/how-can-i-access-the-doordash-reporting-api) (DoorDash Help)
- [About DoorDash Reporting API](https://developer.doordash.com/en-US/docs/reporting/overview/about_reporting/) (DoorDash Developer)
- [Uber Eats CLI and API: partner access and agent boundaries](https://vorplabs.com/agent-tools/uber-eats-cli) (Vorp Labs)
- [Refund dispute deadlines by platform](https://bronze.vision/en/blog/refund-dispute-deadlines.html) (UK)
- [Restaurants say they're bearing the brunt of delivery chargebacks](https://www.restaurantbusinessonline.com/technology/restaurants-say-theyre-bearing-brunt-delivery-chargebacks) (Restaurant Business)
- [California AB 578 delivery app refund law](https://smithallengroup.com/insights/california-ab-578-delivery-app-refund-law-restaurant-impact/) (Smith Allen Group)
- [Top DoorDash chargeback recovery solutions](https://www.danetsoft.com/post/top-doordash-chargeback-recovery-solutions) (Danetsoft)
- [Voosh automated delivery dispute resolution](https://www.voosh.ai/automated-delivery-dispute-resolution) (Voosh)
