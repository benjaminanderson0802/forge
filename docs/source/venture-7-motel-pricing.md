> Snapshot of https://claude.ai/code/artifact/99246beb-9f15-4e0f-8a65-70fef91c41a2 taken 2026-09-29. The claude.ai doc is the editable original; this copy is for agents that cannot reach claude.ai.

# Venture 7: Pricing Management for Independent Motels

Sep 28, 2026 · @Benjamin Anderson

## Bottom line

**In plain terms:** most small independent motels charge the same room price all season, even on nights when every room nearby sells out. Forge sets each night's price automatically, raising it when demand is high and trimming it when it's slow, and keeps a share of the extra money it earns them.

The tools already exist: RoomPriceGenie and PriceLabs cost about €140–250 a month for small properties ([Hotel Tech Insight](https://hoteltechinsight.com/2026/03/04/revenue-management-dynamic-pricing-small-hotels-2026/)). The gap is adoption. Most independents still use fixed seasonal prices because of setup friction, old software and habit.

So the offer isn't software. It's done-for-you pricing:

- zero setup work for the owner
- nothing charged until the extra revenue shows up
- help moving onto booking software that supports automatic pricing, if the motel doesn't have it

Accommodation has the lowest AI adoption of any sector (8%, Census). Roughly 4 in 10 of the US's 91,797 hotels are independent or soft-branded ([Orbital](https://www.withorbital.com/data/how-many-hotels-in-the-us/)).

## The market

Independent motels leave money on the table in predictable ways, and the fix is well proven. What's missing is someone to run it for them.

| Fact | Figure | Source |
| --- | --- | --- |
| US hotels | 91,797 properties, about 63 rooms on average | [Orbital](https://www.withorbital.com/data/how-many-hotels-in-the-us/) |
| Independent or soft-branded | About 40% | Same |
| Revenue lift from automated pricing | 19–21% on average (Lighthouse data); 5–12% for 25–50-room properties in the first 90 days | [Hotel Tech Insight](https://hoteltechinsight.com/2026/03/04/revenue-management-dynamic-pricing-small-hotels-2026/) |
| Existing tools for small properties | RoomPriceGenie about €150–250/month; PriceLabs €140–180 for 30 rooms | Same |
| Managed pricing services | Flat fee or share of revenue, aimed at larger independents | [Revenuenaire](https://revenuenaire.com/hotel-rms-software-vs-managed-dynamic-pricing/) |

Why small motels haven't adopted it:

- they think it's expensive
- old booking software without connections
- manual pricing feels safer
- setup and tuning need skill

Every one of those is removed by a done-for-you service. The lift figures come from vendors, so Forge measures its own results against each motel's past year before charging.

## Access: booking software APIs

Forge changes prices only through the motel's own booking software, never by logging into Booking.com or Expedia. Those sites only connect with certified channel-manager partners.

| Route | What it allows | Use |
| --- | --- | --- |
| Cloudbeds revenue-management API | Read rooms, rates and reservations; push nightly prices and minimum-stay rules (up to 2,500 price updates per 15 minutes) ([Cloudbeds docs](https://developers.cloudbeds.com/docs/revenue-management-system-rms)) | Primary route: apply as a pricing partner |
| Other booking or channel software with pricing-partner APIs | Similar, varies by vendor | Add the ones local motels actually use |
| A motel with no connected software | Nothing to connect to | Forge helps them move to a supported system first; the switch is the only real onboarding step |
| Logging into Booking.com or Expedia as the motel | Not allowed without being a certified connectivity partner | Never |

Market demand signals (local events, competitor rates, holidays) come from public listings and event calendars.

## The autonomous loop

The loop runs nightly for each motel and never prices outside the owner's limits.

&#91;embedded content: Motel pricing loop · nightly per property\]

1. **Read bookings.** Reservations, pickup pace and cancellations from the booking software.
2. **Read demand.** Local events, holidays and nearby properties' public prices.
3. **Set prices.** Each night and room type for the next 90 days, plus minimum-stay rules on peak nights.
4. **Owner limits.** Every price stays between the owner's floor and ceiling, and never moves more than a set step per day. Enforced by the core.
5. **Push prices** through the booking software to every booking site.
6. **Measure.** Revenue per available room against the same period last year and against a hold-out, where some dates stay on old pricing early on.
7. **Bill** a share of the measured gain.
8. **Learn** how demand responds by night type, season and event.

**How it improves itself:** it tests one pricing rule at a time (lead-time curve, weekend premium, event markup, last-minute discount), keeps winners, and reuses general lessons (such as how far ahead to raise prices for events) across markets. It never uses one motel's private bookings or rates to price a competing motel in the same area; see the antitrust guard below.

## Economics and pricing

The fee comes out of new money only, so the owner can't lose. The example motel's figures (30 rooms, 55% occupancy, $90 average rate) are assumptions for illustration.

That motel takes about $542,000 a year in room revenue. At the conservative end of the published first-90-day lift (5%), it gains about $27,000 a year. At 12%, about $65,000.

The launch pricing undercuts the €140–250 monthly tools:

- **No charge** until measured gain exists.
- **After that:** 10% of measured gain, capped at $99 a month for the first year.
- **Locked** for first-year customers.

For the example motel, that's $99 a month (the cap) while it gains the owner over $2,000 a month.

| Motels | Forge revenue a year at $99/month |
| --- | --- |
| 50 | about $59,000 |
| 200 | about $238,000 |
| 500 | about $594,000 |

When the cap lifts after year one, the 10%-of-gain fee alone would be about $2,700 a year per example motel at a 5% lift.

Costs: booking-software partner access (if charged), demand data, Claude usage.

## Risks, human steps and drills

The biggest non-obvious risk is antitrust. A pricing service that uses competing properties' private data to set prices in the same market is exactly what the Justice Department's RealPage case targeted. Its settlement limits live pricing to a client's own data plus public information, and allows only competitor data at least 12 months old, analyzed nationwide ([Fenwick](https://www.fenwick.com/insights/publications/dojs-realpage-settlement-a-blueprint-for-safer-algorithmic-pricing)).

| Risk | Guard |
| --- | --- |
| Algorithmic price-fixing | Each motel's prices use only its own data plus public information (listed prices, events). No client's private bookings or rates ever feed another client's prices. Enforced by data separation in the core |
| Pricing a motel out of business | Owner-set floor, ceiling and maximum daily change; automatic revert if occupancy drops below the owner's threshold |
| Overstated results | Gain measured against last year and hold-out dates; fee only on measured gain |
| Booking-site violations | Prices go only through the motel's own booking software; never logging into booking sites |
| Owner distrust | Weekly report of price changes and revenue; one-tap pause |

**Human steps:**

- **You:** apply as a pricing partner with Cloudbeds and other booking-software vendors.
- **Owner:** connects their booking software (or switches to a supported one) and sets their floor and ceiling.

**Drills before launch:**

1. A price below the owner's floor or above the ceiling → blocked.
2. A daily change larger than the maximum step → capped.
3. A task that reads one client's private data to price another client → rejected by the core.
4. Occupancy falls below threshold → prices revert to the owner's baseline and the owner is alerted.
5. No measured gain → no invoice.

## Sources

- [Dynamic pricing for small hotels: 19–21% revenue lift](https://hoteltechinsight.com/2026/03/04/revenue-management-dynamic-pricing-small-hotels-2026/) (Hotel Tech Insight, Mar 2026)
- [Hotel RMS software vs managed dynamic pricing](https://revenuenaire.com/hotel-rms-software-vs-managed-dynamic-pricing/) (Revenuenaire)
- [How many hotels in the US: 91,797](https://www.withorbital.com/data/how-many-hotels-in-the-us/) (Orbital, Jun 2026)
- [Cloudbeds revenue management system integration](https://developers.cloudbeds.com/docs/revenue-management-system-rms) (Cloudbeds Developers)
- [DOJ's RealPage settlement: a blueprint for safer algorithmic pricing](https://www.fenwick.com/insights/publications/dojs-realpage-settlement-a-blueprint-for-safer-algorithmic-pricing) (Fenwick)
- [AI adoption by industry 2026](https://axis-intelligence.com/ai-adoption-by-industry-statistics/) (citing Census BTOS)
