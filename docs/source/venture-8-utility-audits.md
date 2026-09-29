> Snapshot of https://claude.ai/code/artifact/d893b5c9-ecc8-4686-90a8-d03b8c241f49 taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 8: Utility Bill Audits for Small Businesses

Sep 28, 2026 · @Benjamin Anderson

## Bottom line

**In plain terms:** utilities sometimes bill businesses wrong, for example on the wrong rate plan, with the wrong meter multiplier, or charging sales tax the business doesn't owe. Few small businesses ever check. Forge reads their bills, compares them with the utility's published rates and the state's tax rules, and asks the utility (or the state) for the money back. It keeps a share of refunds and savings, nothing otherwise.

The gap is small businesses. Existing auditors work on contingency (for example 30% of recoveries and 20% of future savings) and target clients spending $100,000+ a year on utilities ([UtilityAudit](https://www.utilityaudit.com/performance-based-utility-audits/); [OEO](https://oeo.com/utility-bill-audit/)). Businesses spending $10,000–$100,000 (restaurants, laundromats, car washes, small plants, farms) are too small for a human auditor to bother with, but not for Forge.

The clock matters: refund look-backs are typically 3 years, up to 6 in New York, and every month of delay loses a month of refundable history ([UtiliSave](https://utilisave.com/resources/blog/the-statute-of-limitations-on-your-utility-refunds-is-counting-down.html)).

## Where small businesses overpay

Most findings fall into a few repeatable checks. That's what makes this automatable: each check compares a bill line against a published rule.

| Error type | What goes wrong | How Forge checks it |
| --- | --- | --- |
| Wrong rate plan (tariff) | Business kept on an older or costlier rate class it no longer fits | Compare usage pattern against every rate schedule the utility publishes for that class |
| Demand and meter errors | Wrong demand reading or meter multiplier inflates charges | Check demand and multiplier against interval data and meter records |
| Misapplied riders and fees | Surcharges applied that don't fit the account | Match each rider to the tariff's eligibility rules |
| Taxes charged that aren't owed | Sales tax on utilities used mainly for production, farming or R&D, which many states exempt; look-backs typically 3–4 years ([Leyton](https://leyton.com/us/utility-sales-tax-exemption/)) | Flag qualifying businesses; prepare the exemption certificate and refund claim |
| Telecom junk charges | Unused lines, unauthorized carrier fees, "cramming" ([OEO](https://oeo.com/utility-bill-audit/)) | Match lines to actual service in use |

The tax exemption is often the largest single win for small manufacturers and farms. One Ohio auto-parts plant cut $256,500 a year, but that plant is far larger than the target customer. Where only part of the usage qualifies, states can require a formal predominant-use study, which may need an engineer partner.

## Rules and data access

This is the lowest-risk venture legally. The business is asking for its own money back, and Forge acts under a signed authorization.

| Topic | Rule | How Forge handles it |
| --- | --- | --- |
| Look-back limits | Typically 3 years; 6 in New York; some states up to 10 ([UtiliSave](https://utilisave.com/resources/blog/the-statute-of-limitations-on-your-utility-refunds-is-counting-down.html)). Each state utility commission sets its own billing-error rules | Store each state's limit; file the oldest recoverable months first |
| Acting for the business | Utilities accept a signed letter of authorization from the account holder | E-signed authorization at sign-up; Forge contacts the utility in the business's name |
| Tax refunds | Filed with the state revenue department or through the utility with an exemption certificate, depending on the state | Prepare the certificate and claim; the business signs where required |
| Bill and usage data | Green Button "Connect My Data" lets authorized third parties pull usage and billing data from participating utilities ([Green Button](https://www.greenbuttondata.org/cmd.html)); aggregators such as [UtilityAPI](https://utilityapi.com/docs/greenbutton) cover many utilities | Primary route; fall back to emailed PDF bills |

No licenses are needed for bill auditing. Tax-exemption work touches state tax rules, so a tax professional should review the first claim in each state.

## The autonomous loop

The first pass recovers past overcharges; after that, a monthly check catches new errors as they happen.

&#91;embedded content: Utility audit loop · per business, then monthly\]

1. **Find businesses** in high-usage categories (restaurants, laundromats, car washes, small plants, farms) from public business listings; outreach by mail and email.
2. **Sign up.** An e-signed letter of authorization.
3. **Pull bills.** Up to the state's look-back window, via Green Button or an aggregator, or from emailed PDFs.
4. **Check every line** against the utility's published tariffs and the state's tax rules.
5. **File claims** with the utility (billing and rate errors) or the state (tax refunds), with the evidence attached.
6. **Track credits** until they appear on bills or as refund checks.
7. **Bill a share** of refunds and of verified future savings.
8. **Monitor monthly** for new errors and better rate plans.

**How it improves itself:** it learns which utilities and error types pay out, prioritizes the checks with the highest hit rate, and builds a library of each utility's tariffs so every new client in that territory is faster.

## Competition, economics and pricing

Incumbents charge about 30% of recoveries and 20% of future savings, and skip small accounts. Forge undercuts both and serves the accounts they skip.

- **Launch price:** 15% of refunds and 10% of verified savings for 12 months. Nothing upfront, nothing if nothing is found. Locked for first-year customers.
- **Floor:** data-access cost per account plus usage.

The hit rate and size of errors for small accounts aren't published, so the example below assumes one: a business spending $30,000 a year with a 5% overcharge found. That gives a 3-year refund of $4,500 plus $1,500 a year saved, for a fee of about $825.

| Businesses | Fee revenue (assumed $825 each) |
| --- | --- |
| 100 | $82,500 |
| 500 | $412,500 |
| 1,000 | $825,000 |

Many audits will find nothing; the loop's first job is to measure the real hit rate by business type and target only the types that pay. Monthly monitoring adds a small recurring fee from each new error or rate change caught.

**Cross-sell:** restaurants from the refund-recovery add-on and advertisers from the 9x12 postcards are natural first customers.

## Risks, human steps and drills

| Risk | Guard |
| --- | --- |
| Wrong claims annoy utilities and waste goodwill | Claims filed only with evidence tied to a published tariff line or tax rule |
| Switching a business to a worse rate plan | Rate changes proposed only when 12 months of the business's own usage shows savings; owner approves any plan change |
| Tax-exemption mistakes | Exemption certificates only for categories the state clearly exempts; partial-use cases go to an engineer or tax partner |
| Missing look-back deadlines | Oldest months filed first; each state's limit stored in the core |
| Billing disputes over savings | Fee computed only from credits and savings visible on bills |

**Human steps:**

- **You:** a tax professional's review of the first tax claim per state, and an engineer partner for partial-use exemption studies.
- **The business:** e-signs the authorization and any state tax forms.

**Drills before launch:**

1. A claim without a matching tariff or tax rule → not filed.
2. A rate-plan change that would raise cost on 12 months of usage → blocked.
3. A month older than the state's look-back → excluded from the claim.
4. No credit on bills → no invoice.
5. An account without a signed authorization → no contact with the utility.

## Sources

- [Performance-based utility audits](https://www.utilityaudit.com/performance-based-utility-audits/) (UtilityAudit)
- [Utility bill audit program](https://oeo.com/utility-bill-audit/) (OEO Energy Solutions)
- [The statute of limitations on your utility refunds](https://utilisave.com/resources/blog/the-statute-of-limitations-on-your-utility-refunds-is-counting-down.html) (UtiliSave)
- [Complete guide to utility sales tax exemption](https://leyton.com/us/utility-sales-tax-exemption/) (Leyton)
- [Green Button Connect My Data](https://www.greenbuttondata.org/cmd.html) (Green Button)
- [UtilityAPI Green Button docs](https://utilityapi.com/docs/greenbutton) (UtilityAPI)
