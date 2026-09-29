> Snapshot of https://claude.ai/code/artifact/86ce6d3c-5a98-4bc8-83e3-2839696a52ae taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 5: Government Supply Orders (Revisited)

Sep 28, 2026 · @Benjamin Anderson

## In plain terms

The military's supply agency posts thousands of small shopping requests online every day, such as "we need 40 of this exact bolt, delivered to this base." Forge finds requests for ordinary parts that normal suppliers carry, asks a supplier what the part costs, and offers to sell it to the government for a bit more. The lowest valid offer usually wins, often picked by software. When Forge wins, it buys the part from the supplier, has it shipped to the base, and the government pays the invoice about a month later. The profit is the difference between the two prices.

It's a middleman business run at machine speed, where the buyer is also a machine.

## Bottom line

On a second look, this is the most machine-friendly of all the ventures, if it's narrowed to one channel: the Defense Logistics Agency's bid board, DIBBS. It posts thousands of mostly small quote requests daily ($500–$10,000 typical), and many are evaluated and awarded by software with no contracting officer involved ([DLA Blueprint](https://dlablueprint.com/blog/posts/dibbs-dla-contracts.html)). A software buyer on one side and Forge on the other is a real, unexpected edge.

Your hesitation is justified on two points:

- **Certification risk.** A false "made in a compliant country" certification is a False Claims Act case. A Connecticut reseller paid $300,000 in 2025 for shipping Chinese products it had certified as compliant ([GSA OIG](https://www.gsaig.gov/news/connecticut-company-and-owner-settle-liability-false-claims-related-violations-buy-american)).
- **Cash.** You pay suppliers before the government pays you. At 20 awards a week, that's roughly $200,000 of working capital tied up.

**Recommendation: go, with hard limits.**

- DIBBS only, open-competition items only.
- Only products with documented, compliant country of origin from authorized distributors.
- Order-size and monthly exposure caps enforced by the core.
- Start at 5 awards a week and scale only as payments come in.

## How small government buying works

There are four doors into small government orders. Only one is both high-volume and machine-readable end to end.

| Channel | How orders happen | Fit for autonomy |
| --- | --- | --- |
| **DIBBS (Defense Logistics Agency)** | Thousands of small quote requests daily for specific part numbers. Many are auto-evaluated on price and compliance, and DLA publishes award history with winning prices per part ([DLA Blueprint](https://dlablueprint.com/blog/posts/dibbs-dla-contracts.html)) | **Best.** Structured requests, price history, software evaluation |
| Micro-purchases (up to $15,000) | Agency cardholders buy directly from vendors they find; no competition required if the price is reasonable. Not usually posted ([SLED.AI](https://www.sledai.com/blog/easiest-federal-contracts-under-25k/)) | Weak. Depends on being found (SAM profile, GSA Advantage, outreach), not on quoting |
| SAM.gov simplified acquisitions (up to $350,000) | Posted requests for quotes, readable through the public [Get Opportunities API](https://open.gsa.gov/api/get-opportunities-public-api/) | Medium. Less standardized; many need written responses |
| State and city bid portals | Hundreds of separate portals and formats | Weak at first; add later |

To quote on DIBBS you need, in order: SAM.gov registration, a CAGE code (issued through SAM), then a DIBBS account. Some parts are restricted to approved sources, but "plenty of buys are fully competitive." Most new vendors lose on compliance details, not price: incomplete specs, restricted parts, missed delivery times, military packaging costs (MIL-STD-2073) and shipping terms.

## Rules that can't be broken

In this venture, a wrong checkbox is a legal event, not a bug. So every certification is decided by plain code from documents on file, never by an agent's judgment.

| Rule | What it means for a reseller | How Forge enforces it |
| --- | --- | --- |
| Country-of-origin certifications (Trade Agreements Act, Buy American Act) | Certifying origin you can't document is a False Claims Act risk; the 2025 Connecticut case cost $300,000 ([GSA OIG](https://www.gsaig.gov/news/connecticut-company-and-owner-settle-liability-false-claims-related-violations-buy-american)) | No quote unless the supplier's written country-of-origin for that exact part is on file and matches the requirement. Missing document means no quote, with no exceptions |
| Nonmanufacturer rule on set-asides | Applies above $350,000 for general small-business set-asides, and above the micro-purchase threshold for socioeconomic set-asides. A reseller must then supply a U.S. small-business manufacturer's product or have a waiver ([SBA](https://www.sba.gov/partners/contracting-officials/small-business-procurement/nonmanufacturer-rule)) | Start with unrestricted (not set-aside) requests under the threshold; skip socioeconomic set-asides entirely |
| Approved sources and traceability | Restricted parts need approved-source status; all parts need proof they're genuine | Skip restricted items; buy only from authorized distributors that supply certificates of conformance |
| Packaging and delivery terms | Military packaging and shipping terms are part of the award | Parsed from each request; the quote includes the full packaging cost or isn't sent |
| Delivery promises | Late delivery hurts the vendor's performance record and future awards | Quote only lead times the distributor confirms, plus a buffer |

I'm not a lawyer. A government-contracts attorney should review the certification rules and the first month of quotes.

## The autonomous loop

The loop runs daily. The compliance gate is plain code in the trusted core, so no agent can talk its way past it.

&#91;embedded content: Government supply loop · daily\]

1. **Pull requests.** Every new DIBBS request for quote, daily.
2. **Filter.** Keep open-competition, unrestricted requests in the product families Forge has sourcing for, under the order-size cap.
3. **Source.** Find the part at authorized distributors, and record price, lead time, certificate of conformance and written country of origin.
4. **Price.** Compare with DLA's published award history for that part number, then price to win while staying above the floor (cost + packaging + shipping + margin minimum).
5. **Compliance gate.** Origin document on file and compliant; packaging cost included; lead time confirmed; order and monthly exposure within caps. Any failure means skip.
6. **Quote.** Submitted on DIBBS. Whether DIBBS allows batch quote uploads is to be confirmed at account setup; if not, the one browser step runs on the agent computer.
7. **Win and fulfill.** Order from the distributor, ship with the required packaging and labels, and upload tracking.
8. **Get paid.** Invoice through DoD's invoicing system after delivery.
9. **Learn.** Win and loss prices by part and family, supplier reliability, and which families to expand.

**How it improves itself.** It tests one change each week:

- **What changes:** the margin over cost, product families, supplier mix and lead-time buffer.
- **What's scored:** win rate, gross margin per award, on-time delivery and days to payment.
- **Scale rule:** order caps rise only after on-time delivery stays at 100% and payments arrive on schedule.

## Data and tool stack

The government side is free to access; the cost is in suppliers and working capital.

| Job | Source or service | Notes |
| --- | --- | --- |
| Registration | SAM.gov, then a CAGE code, then a [DIBBS](https://www.dla.mil/Working-With-DLA/Applications/Details/Article/2921495/dibbs-dla-internet-bid-board-system/) account | One-time; you do it |
| Quote requests and award history | DIBBS | Confirm bulk download and batch quote options at setup |
| Wider federal requests | [SAM.gov Get Opportunities API](https://open.gsa.gov/api/get-opportunities-public-api/) | Public key; daily limits depend on account role |
| Parts, prices, lead times, origin documents | Authorized industrial distributors with online catalogs or APIs | Each supplier onboarded once; origin documents stored per part |
| Packaging and labels | Distributor-packed to spec, or a packaging vendor | Cost built into each quote |
| Invoicing | DoD's invoicing system after delivery | One-time setup |
| Payment to suppliers | Business card or line of credit | Monthly exposure cap in the core |
| Ledger, gate and learning | Forge's trusted core | Certifications decided by plain code |

## Economics and cash flow

Profit here is margin on each order, and cash is the constraint. The margins below are assumptions to be replaced by real award data within the first month.

The example: an average award of $2,000 (inside DIBBS's typical $500–$10,000 range), with about 6 weeks from paying the supplier to being paid by the government.

| Awards a week | Margin | Gross profit a year | Cash tied up |
| --- | --- | --- | --- |
| 5 | 10% | $52,000 | about $54,000 |
| 20 | 10% | $208,000 | about $216,000 |
| 20 | 5% | $104,000 | about $228,000 |

This is the one venture where the pricing strategy needs care. The award goes to the lowest compliant price, so undercutting is the whole game. But the floor (cost, packaging, shipping and a minimum margin) is enforced by the core, so the loop can't win orders at a loss.

The scaling path:

- start at about 5 awards a week, funded by a business card or a small credit line
- grow as payments arrive on schedule
- let the Optimizer lower margins only while win rate and on-time delivery hold

## Risks, human steps and drills

| Risk | Guard |
| --- | --- |
| False country-of-origin certification | No origin document on file, no quote; enforced by the core |
| Supplier ships late or wrong part | Only distributors with confirmed stock and lead time; buffer on every promise; track and escalate |
| Cash squeeze | Monthly exposure cap; caps rise only after on-time payments |
| Losing money on a win | Floor price enforced by the core |
| Restricted or set-aside requests | Filtered out |

**Human steps:**

- one-time SAM, CAGE and DIBBS registration and banking
- an attorney review of the certification rules
- approving each cap increase

**Drills before launch:**

1. A part with no origin document → no quote.
2. A quote below the floor → blocked.
3. A request that would exceed the monthly exposure cap → skipped.
4. A restricted or set-aside request → filtered out.
5. A supplier lead time longer than the requested delivery → no quote.
6. An award → exactly one supplier order and one invoice.

## Sources

- [DIBBS DLA contracts: how the bid board works](https://dlablueprint.com/blog/posts/dibbs-dla-contracts.html) (DLA Blueprint)
- [DIBBS](https://www.dla.mil/Working-With-DLA/Applications/Details/Article/2921495/dibbs-dla-internet-bid-board-system/) (Defense Logistics Agency)
- [Nonmanufacturer rule](https://www.sba.gov/partners/contracting-officials/small-business-procurement/nonmanufacturer-rule) (SBA)
- [Connecticut company settles False Claims Act liability](https://www.gsaig.gov/news/connecticut-company-and-owner-settle-liability-false-claims-related-violations-buy-american) (GSA OIG, May 2025)
- [SAM.gov Get Opportunities Public API](https://open.gsa.gov/api/get-opportunities-public-api/) (GSA)
- [Easiest federal contracts under $25K](https://www.sledai.com/blog/easiest-federal-contracts-under-25k/) (SLED.AI)
- [SAT and micro-purchase thresholds 2026](https://casrai.org/guides/simplified-acquisition-threshold-and-micro-purchase-threshold) (CASRAI)
