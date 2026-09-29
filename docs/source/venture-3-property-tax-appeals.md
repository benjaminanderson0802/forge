> Snapshot of https://claude.ai/code/artifact/e02ea6b8-6377-4956-b3f7-9d3fdd75d9d7 taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 3: Autonomous Property Tax Appeals

Sep 28, 2026 · @Benjamin Anderson

## Bottom line

Forge finds homes that are likely over-assessed, mails each owner a personal savings estimate, builds the evidence, files the appeal, and keeps about 25% of the first year's savings, only on wins. The work is almost entirely data and paperwork.

One correction to the pitch: this market is not untouched. Ownwell raised $50M in Feb 2026, has filed over 1 million appeals, and charges 25%, in Texas, Illinois, Florida, Georgia, California, Washington and New York ([PR Newswire](https://www.prnewswire.com/news-releases/ownwell-raises-50m-launches-national-service-to-streamline-property-tax-appeals-and-make-home-ownership-more-affordable-302692103.html)). The opening is the states they don't serve.

The plan:

- **Launch in Colorado.** Agents may file with a notarized letter of authorization, and every county revalues in odd years. The 2027 appeal window is May 1–June 8, 2027, which gives about 7 months to build and test.
- **Add Arizona** once a $200 state agent license is in place.
- **In agent-restricted, high-tax states** such as New Jersey, sell a flat-fee, pre-filled appeal packet that the homeowner submits themselves.

By my estimate, most of the work can run autonomously. The human pieces are occasional hearings and a one-time legal review.

## How residential appeals work

The gap is large and mostly ignored. The National Taxpayers Union estimates 30–60% of taxable property is over-assessed, yet fewer than 5% of taxpayers appeal ([AppealDesk](https://www.appealdesk.com/blog/property-tax-appeal-success-rates)).

1. **Notice of value.** The county assessor mails each owner a value. In Colorado that happens every odd year, based on a June 30 valuation date ([Adams County](https://adamscountyco.gov/our-county/elected-officials/assessor/appeals-process/)).
2. **Appeal window.** A short, fixed window with no extensions. Colorado assessor-level appeals run May 1–June 8; New Jersey's is April 1.
3. **Evidence.** The winning evidence is 3–5 comparable sales below the assessed value, or a factual error on the property record, such as the wrong square footage or lot size.
4. **Decision.** The assessor adjusts or denies. The owner can escalate to the county Board of Equalization (in Colorado, by Sept 15, with hearings Sept 1–Nov 1).
5. **Result.** A lower value cuts the tax bill for that year, and in Colorado the lower value likely carries through the two-year cycle (to be confirmed with each county).

Why owners leave the money on the table:

- deadlines they miss
- not knowing how to find comparable sales
- dread of a hearing

Every one of those is work an agent can do for them. Success rates are high with evidence: Ownwell reports 86% with $774 average savings, and New Jersey guides report 50–65% for homeowners who file with evidence.

## Where to operate

Rules on who may represent a homeowner for a fee vary by state, so the venture picks its states by the rules and by where Ownwell isn't.

| State | Can a paid non-attorney agent file? | Ownwell present? | Plan |
| --- | --- | --- | --- |
| Colorado | Yes, with a notarized letter of agency filed with the appeal ([Adams County](https://adamscountyco.gov/our-county/elected-officials/assessor/appeals-process/)) | Not in its listed states | **Launch here** for the May 1–June 8, 2027 window |
| Arizona | Yes, with a state property tax agent license ($200 application) from the Department of Insurance and Financial Institutions ([AZ DIFI](https://difi.az.gov/industry/property-tax-agents)) | Not in its listed states | **Second state**, once licensed |
| New Jersey | Owners can self-represent at the county board; paid representation of others is effectively attorney territory ([AppealDesk NJ](https://www.appealdesk.com/blog/new-jersey-property-tax-appeal)) | Not listed | **Flat-fee DIY packet**: Forge builds the evidence and fills the form; the owner files (deadline April 1; county board fee $5–$25) |
| Tennessee | Only registered agents with 120 hours of appraisal training, 4 years' experience and a state exam ([TN Comptroller](https://comptroller.tn.gov/boards/state-board-of-equalization/sboe-services/sboe-agent-registration.html)) | Not listed | Skip |
| Ohio | Likely unauthorized practice of law for non-attorneys to file for others (per Ohio Supreme Court precedent; not verified here) | Not listed | Skip, or DIY packet only after an attorney confirms |
| Texas | Requires state registration (TDLR) | Yes; 200,000+ Texas properties | Skip |
| IL, FL, GA, CA, WA, NY | Varies | Yes | Skip for now |

I'm not a lawyer. A local attorney should confirm the Colorado agency letter, the fee agreement and the mail wording once, before the first mailing.

## The autonomous loop

The loop runs once per state per appeal window, and each window's outcomes improve the next.

&#91;embedded content: Appeal venture loop · yearly per state\]

1. **Pull county data.** Assessed values, recent sales and property characteristics from each county's public records and bulk downloads.
2. **Score homes.** Compare each home's assessed value with 3–5 close comparable sales, and flag property-record errors (square footage, beds and baths, lot size). Only homes where the evidence shows a reduction worth at least a set minimum make the list.
3. **Mail the estimate.** An addressed letter to each flagged owner with their own numbers: assessed value, comparable sales and estimated savings. It includes a QR code to sign up.
4. **Sign up.** An e-signed fee agreement plus the notarized letter of agency Colorado requires, using remote online notarization if the county accepts it. Otherwise that step goes to a mobile notary.
5. **File the appeal.** The evidence packet is filed online through the county's portal before the deadline.
6. **Decision.** If accepted, the reduction is recorded. If denied and the evidence is strong, escalate to the Board of Equalization, where a hearing goes to the human queue.
7. **Invoice on win.** The fee is calculated from the tax bill once the lower value is confirmed; nothing is owed on a loss.
8. **Learn.** Win rate and reduction size, by county, home type and argument, feed back into scoring. The next cycle mails only the homes most likely to win.

**How it improves itself.** Each window tests one change:

- **What changes:** the minimum savings threshold, the letter design and offer, the fee rate, and which arguments lead the packet.
- **What's scored:** response rate, sign-up-to-file rate, win rate, average reduction and fee collected per letter mailed.
- **Between windows:** the off-season loop prepares the next state, such as Arizona licensing and New Jersey packet templates.

## Data and tool stack

Public data does most of the work. Colorado records sales prices, so comparable sales are available. Texas, by contrast, doesn't disclose sale prices, which makes it harder.

| Job | Source or service | Notes |
| --- | --- | --- |
| Values, property records, sales | County assessor records and bulk downloads (many Colorado counties publish these) | Coverage varies by county; start with the counties that have bulk data |
| Comparable-sales engine | Forge (built in-house) | Picks 3–5 comps by distance, size, age and sale date; flags record errors |
| Addressed mail | A print-and-mail API such as [Lob](https://www.lob.com/) or [PostGrid](https://www.postgrid.com/print-mail-api/) | Letters are generated per homeowner and tracked |
| Sign-up page | A simple page for each letter's QR code | Shows the owner their evidence before they sign |
| Agreement and letter of agency | E-signature, plus remote online notarization where the county accepts it | Otherwise a mobile-notary step |
| Filing | County online appeal portals (for example Adams County's) | Forge fills the portal forms. Where a portal needs a person to log in, it's the one step handled on the agent computer |
| Fee collection | Stripe invoice after the reduction is confirmed | Card or ACH |
| Ledger and learning | Forge's trusted core | Every letter, filing and outcome is logged |

The build that must exist before May 1, 2027:

1. data pulls for the target counties
2. the comp engine
3. letter templates
4. the sign-up flow
5. portal filing for each county
6. drills (see below)

That's about 7 months, enough for a practice run on 2025 data, with results checked against the actual 2025 appeal outcomes where counties publish them.

## Economics

A win earns about $190 at Ownwell's benchmark of $774 average savings and a 25% fee. The venture makes money on volume, and on mailing only homes likely to win.

A worked example: 20,000 letters to flagged homes, a 60% win rate (below Ownwell's reported 86%, to be conservative), $700 average savings and a 25% fee. The response rates use the ANA range for prospect mail (2–4.4%).

| Response rate | Sign-ups | Wins | Fee revenue |
| --- | --- | --- | --- |
| 2% | 400 | 240 | $42,000 |
| 3% | 600 | 360 | $63,000 |
| 4.4% | 880 | 528 | $92,400 |

Costs not included above:

- printing and postage for 20,000 letters (from the mail provider's quote)
- notarization per sign-up
- county filing fees where they apply
- Claude usage

The biggest lever is the win model: mailing fewer, better-targeted homes raises both response and win rate.

The work is seasonal per state, so profit per year grows by stacking states with different windows. For example, New Jersey packets are due April 1, Colorado appeals run May–June, and Arizona follows its own notice schedule (to be confirmed).

## Risks and guards

This venture deals with homeowners' money and government filings, so trust is the asset. Its guards are fixed rules, not judgment calls.

| Risk | Guard |
| --- | --- |
| Mail that looks like an official government notice | Federal law requires a clear disclaimer on solicitations resembling government documents (39 U.S.C. § 3001). Every letter states it's from a private company, and the format is reviewed once by an attorney |
| Overpromising savings | Letters show a range with the evidence behind it, never a guaranteed amount |
| Weak or frivolous appeals | Filed only when the comparable-sales evidence clears the threshold; the owner sees the evidence before signing |
| An appeal that raises the value | Before launch, check each state's rules on whether a review can raise the value; never file where the evidence is borderline |
| Missed deadlines | Deadlines are hard-coded per county, with filing scheduled days early; a drill checks it |
| Filing without authority | No filing without a signed agreement and a notarized agency letter on record; enforced by the core |
| Practicing law or unlicensed representation | Operate only where paid agents are allowed (Colorado, and Arizona after licensing); DIY packets elsewhere |
| Privacy | Only public records plus what the owner provides; no data resale; delete on request |
| Competition | Ownwell expanding into new states; stay ahead by moving first into its gaps and by winning on local data |

## What stays human, and the drills this venture adds

The human work is setup and hearings; the appeal machine runs without you.

| Human task | When | Why |
| --- | --- | --- |
| One-time: business entity, attorney review of the agreement, agency letter and mail wording | Before the first mailing | Legal identity and sign-off |
| Arizona agent license application | Before entering Arizona | License is personal |
| Board of Equalization hearings on escalated cases | Sept–Nov in Colorado, only strong cases | Some hearings need a person present |
| Mobile notary visits | Only where remote notarization isn't accepted | Physical signature |

Before launch, these drills must pass in a sandbox using past-year data:

1. A home without a signed agreement and agency letter → filing is blocked.
2. A county deadline passes in the simulation → no late filing is attempted; the ledger logs the miss.
3. Evidence below the savings threshold → no letter is mailed and no appeal is filed.
4. A letter draft missing the private-company disclaimer → blocked before printing.
5. A win is recorded → exactly one invoice at the agreed rate; a loss → no invoice.
6. A homeowner asks to delete their data → removed everywhere except legally required records.
7. The backtest: the model's picks, scored against actual past outcomes where published, must beat mailing at random.

The first milestone is the backtest in one Colorado county by January 2027. Then the full build is ready for the May 1, 2027 window.

## Sources

One page, an Ohio unauthorized-practice article on the National Law Review, returned an error, so the Ohio row is unverified.

- [Ownwell raises $50M, launches national service](https://www.prnewswire.com/news-releases/ownwell-raises-50m-launches-national-service-to-streamline-property-tax-appeals-and-make-home-ownership-more-affordable-302692103.html) (PR Newswire, Feb 2026)
- [Property tax appeal success rates by state and county](https://www.appealdesk.com/blog/property-tax-appeal-success-rates) (AppealDesk)
- [New Jersey property tax appeal 2026](https://www.appealdesk.com/blog/new-jersey-property-tax-appeal) (AppealDesk)
- [Adams County appeals process](https://adamscountyco.gov/our-county/elected-officials/assessor/appeals-process/) (Adams County, CO)
- [Arizona property tax agents](https://difi.az.gov/industry/property-tax-agents) (AZ Department of Insurance and Financial Institutions)
- [Tennessee SBOE agent registration](https://comptroller.tn.gov/boards/state-board-of-equalization/sboe-services/sboe-agent-registration.html) (TN Comptroller)
- [Texas property tax professionals FAQ](https://www.tdlr.texas.gov/taxprof/taxproffaq.htm) (TDLR)
- [Property tax appeal companies: 2026 guide](https://countyauditors.org/property-tax-assessment-appeal-companies/) (fee ranges)
- [Direct mail response rate benchmarks](https://directmail.io/direct-mail-response-rate) (DirectMail.io, citing ANA)
- [Lob](https://www.lob.com/) and [PostGrid print-and-mail API](https://www.postgrid.com/print-mail-api/)
