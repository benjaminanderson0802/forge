> Snapshot of https://claude.ai/code/artifact/78f529b4-fd1d-4c16-bbec-ddc43887f306 taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 6: Tariff Refund Recovery for Small Importers

Sep 28, 2026 · @Benjamin Anderson

## Bottom line

**In plain terms:** in February 2026 the Supreme Court ruled that a set of 2025 tariffs was illegal, so the government owes importers their money back. The refund isn't automatic: each importer has to file with Customs, and big companies did it fast while small ones are falling behind. Forge finds small importers still owed money, prepares and fixes their refund filings, and a licensed customs broker partner files them. Forge takes a share of what comes back.

The window is real but closing:

- **73% already repaid.** About 73% of the roughly $166B has been repaid as of Sept 11, 2026, leaving tens of billions ([TariffsTool tracker](https://www.tariffstool.com/tariff-refund-tracker)).
- **Small importers lag.** In July, approved refunds covered 60% of the money but only 30% of entries, which means small importers are the ones left behind ([Cato](https://www.cato.org/blog/ieepa-refunds-update-good-progress-still-ways-go)).

Two hard constraints shape everything:

- **Broker partner required.** Preparing refund documents for someone else is "customs business" under federal rules, which requires a licensed customs broker ([19 CFR 111.1](https://www.ecfr.gov/current/title-19/chapter-I/part-111/subpart-A/section-111.1)).
- **Some refunds need a lawsuit.** Older, fully-settled entries now require a Court of International Trade suit filed by an attorney.

Recommendation: launch now, as a broker partnership, focused on unfiled and failed small-importer entries.

## Where refunds stand

Customs runs refunds through an online system called CAPE, in phases. Each phase covers a different kind of entry.

| Phase | Covers | Status | What small importers need |
| --- | --- | --- | --- |
| 1 | Entries not yet settled, or settled within the last 80 days | Live since Apr 20, 2026 | File a declaration; fix rejected rows |
| 2 | Reconciliation entries and anti-dumping cases | Live since Jun 29, 2026 | Same; 4.36 million entries had failed Customs' checks, mostly on fixable errors like mismatched importer IDs, entry-number mistakes and spreadsheet format ([Diaz Trade Law](https://diaztradelaw.com/cape-phase-two-refund-failures/)) |
| 3 | Fully settled ("finally liquidated") entries, about $11.4B | Delayed as of Aug 25, 2026; refunds only for importers who sued ([National Law Review](https://natlawreview.com/article/ieepa-tariff-refunds-critical-developments-phase-iii-delays-and-action-steps)) | A lawsuit at the Court of International Trade within 2 years of the entry. The earliest deadlines fall around February–April 2027 |

Other facts that matter:

- **Who gets paid:** refunds go to the importer of record (or their broker) and need bank details in the Customs portal. In July, 8,384 approved declarations were stuck on missing banking information alone.
- **What's eligible:** only the emergency (IEEPA) tariffs, paid Feb 4, 2025–Feb 20, 2026. Section 232, Section 301 and normal duties are not refundable.
- **Interest:** refunds earn statutory interest, 6% a year for corporations, compounded daily.

## Legal structure

Forge can't be the filer. It can be the engine inside a licensed broker's operation, which is also what makes it trustworthy in a market full of refund scams.

| Role | Who | Why |
| --- | --- | --- |
| Finds importers, pulls and checks their entry data, drafts and repairs refund files, tracks status | Forge, working under the broker's supervision | Document preparation for others is "customs business" ([19 CFR 111.1](https://www.ecfr.gov/current/title-19/chapter-I/part-111/subpart-A/section-111.1)), so it must happen under a licensed broker's responsible supervision |
| Reviews and files declarations with Customs | Licensed customs broker partner | The legal filer; holds the importer's power of attorney |
| Lawsuits for fully settled entries | Trade attorneys | Court filings. Forge sends them organized files; any payment arrangement must follow attorney ethics rules on fee sharing |
| Signs the power of attorney, confirms bank details | The importer | Their refund, their account |

You don't need a license for this model. Forge recruits established licensed brokers as partners, brings them small-importer clients with files already prepared, and the broker files under their own license. You're the middleman supplying clients and prep work.

How the partnership works:

- **One or more established brokers** who want small-importer volume but lack staff to process it. Forge brings them prepared, pre-checked files.
- **Written agreement** covering the fee split, data handling and who supervises what.
- **Longer-term option:** you or a hire earns a customs broker license, which brings the whole venture in-house.

**Scam-proofing is built in.** The US Chamber warns importers to work only with established, legitimate brokers ([US Chamber](https://www.uschamber.com/economy/tariff-refunds-faq-what-small-businesses-need-to-know-after-supreme-courts-ruling)). So:

- no upfront fees, ever
- refunds go straight to the importer's own bank account, never through Forge
- the broker's license number is shown on every outreach
- the importer sees their own entry data before signing

I'm not a lawyer; the partnership agreement needs a trade attorney's review.

## The autonomous loop

Forge does the volume work; the broker's review is the gate before anything reaches Customs.

&#91;embedded content: Tariff refund loop · per importer\]

1. **Find importers.** Small importers who brought in goods from Feb 2025 to Feb 2026, found through public shipping-manifest data vendors and trade directories.
2. **Outreach.** A broker-branded letter and email with a plain explanation, the broker's license number, and a link to check eligibility.
3. **Sign up.** A power of attorney to the broker, and the importer adds bank details in the Customs portal.
4. **Pull entries.** Entry-level reports from the Customs portal, via the broker.
5. **Sort by phase.** Phase 1 or 2 files go forward; fully settled entries go to a partner trade attorney with the lawsuit deadline flagged.
6. **Triage and repair.** Fix the errors that sank millions of filings: importer/filer mismatches, entry-number mistakes and spreadsheet format.
7. **Broker reviews and files.**
8. **Refund paid.** Straight to the importer's bank.
9. **Bill share.** Only after the money lands.
10. **Learn.** Which errors cause rejections, and which importer profiles convert.

This is a sprint, not a forever loop. When the refund pool winds down, the same machinery (importer lists, broker partnership, entry-data tooling) carries over to other customs refunds and to import compliance work.

## Economics and pricing

The pool is large, but Forge's cut depends on three unknowns: the typical small importer's refund, the fee the market will accept, and the split with the broker. Treat the figures below as a model to replace with real numbers from the first 20 importers.

Pricing follows the venture strategy of undercutting clearly:

- **Launch fee:** 10% of refunds actually received, with nothing upfront and nothing if no refund arrives.
- **Split:** shared with the broker partner under the written agreement.
- **Interest:** the refund's statutory interest (6% a year for corporations) also goes to the importer, which makes the offer easier to accept.

| Importers served | Average refund (assumed) | Fee at 10% | Forge share at a 50/50 split (assumed) |
| --- | --- | --- | --- |
| 100 | $15,000 | $150,000 | $75,000 |
| 300 | $15,000 | $450,000 | $225,000 |
| 1,000 | $15,000 | $1,500,000 | $750,000 |

Costs: shipping-data subscription, mailing, broker onboarding, Claude usage.

This venture is front-loaded. The best months are the next few, before the remaining filings are done and lawsuit deadlines start passing in early 2027.

## Risks, human steps and drills

| Risk | Guard |
| --- | --- |
| Acting as an unlicensed customs broker | Forge never files or signs; every file is reviewed and filed by the licensed broker; the core blocks direct submission |
| Looking like a refund scam | No upfront fees; refunds go straight to the importer; broker license shown on every contact |
| Claiming non-refundable duties | Only IEEPA tariffs paid Feb 4, 2025–Feb 20, 2026; Section 232, 301 and normal duties are excluded by rule |
| Missing a lawsuit deadline | Fully settled entries are routed to an attorney immediately, with each entry's 2-year deadline tracked |
| Handling sensitive trade data | Access through the broker only; data deleted after the refund closes |
| Window closes | Launch fast; reuse the machinery for other customs refunds |

**Human steps:**

- **You:** sign one or two broker partners, attorney review of the agreement, and an introduction to a trade litigation firm.
- **The broker:** reviews and files each declaration.
- **The importer:** signs the power of attorney and confirms bank details.

**Drills before launch:**

1. An attempt to submit to Customs without broker sign-off → blocked.
2. A Section 301 or 232 duty line → excluded from the claim.
3. A fully settled entry → routed to the attorney queue with its deadline, never to CAPE.
4. An importer with no bank details on file → flagged before filing.
5. A refund recorded as received → exactly one invoice at the locked rate; no refund → no invoice.

## Sources

Customs' own IEEPA refund page returned an error, so phase details come from the secondary sources below.

- [IEEPA tariff refund tracker, Sept 11, 2026](https://www.tariffstool.com/tariff-refund-tracker) (TariffsTool)
- [IEEPA refunds update: good progress, still a ways to go](https://www.cato.org/blog/ieepa-refunds-update-good-progress-still-ways-go) (Cato, Jul 2026)
- [CAPE Phase 2 update: 4.36 million entries fail](https://diaztradelaw.com/cape-phase-two-refund-failures/) (Diaz Trade Law, Jul 2026)
- [IEEPA refunds stall for finally liquidated entries](https://natlawreview.com/article/ieepa-tariff-refunds-critical-developments-phase-iii-delays-and-action-steps) (National Law Review, Aug 2026)
- [Tariff refunds FAQ for small businesses](https://www.uschamber.com/economy/tariff-refunds-faq-what-small-businesses-need-to-know-after-supreme-courts-ruling) (US Chamber)
- [IEEPA tariff refunds are moving forward](https://nrf.com/blog/ieepa-tariff-refunds-are-moving-forward) (NRF)
- [19 CFR 111.1: definition of customs business](https://www.ecfr.gov/current/title-19/chapter-I/part-111/subpart-A/section-111.1) (eCFR)
