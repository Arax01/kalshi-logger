# Pre-registration: clean re-test of the calibration study

Written on October 5, 2026, before any trade from October 6, 2026 onward has been looked at. The git
commit that adds this file is the timestamp. Nothing below may change after that commit. If something
must change, add a dated note at the end saying what changed and why, and treat any affected test as
no longer clean.

## Why

The first calibration study's holdout (trades from July 1, 2026) was looked at before its rules were
final (see section 6 of that report). This re-test uses data nobody has seen.

## Data

- **Fresh test set:** sampled Kalshi trades from **October 6, 2026 00:00 UTC** onward. The sampling
  method is the same as the first study: 150 random windows per complete calendar month, sized for
  about 1,000 trades each, with fixed seeds.
- **Earlier data:** all sampled trades before October 6, 2026 (including the old July-September
  holdout) are earlier data only. They may be used for exploration, never to judge the hypotheses
  below.
- **Results:** only markets that settled YES or NO by the time of each look are used.

## Rules (frozen)

The rules are the calibration report's code as of this commit (`kalshi_logger/calib_report.py`):
- price buckets, fees (the fee in effect at trade time), and returns per $1 split four ways
  (YES or NO, as taker or as resting maker);
- 95% ranges clustered by event;
- the effective-events measure, with a minimum of 30;
- tradeability flags.

## Hypotheses (named in advance)

Each hypothesis is tested only on the fresh test set.

| # | Category, YES price bucket | Claim | Side that must make money |
|---|---|---|---|
| H1 | Game winners, 95-99c | YES wins more often than its price | YES as resting maker |
| H2 | Crypto, 95-99c | YES wins more often than its price | YES as taker |
| H3 | Combos, 1-5c | YES wins less often than its price | NO as resting maker |
| H4 | Combos, 90-95c | YES wins more often than its price | **YES as taker** |

H1-H3 are the patterns confirmed on the contaminated holdout. H4 is the borderline pattern found under
the original rules (about 43 effective holdout events).

**A hypothesis is SUPPORTED at a look only if all of these hold on the fresh test set:**
1. at least 30 events, and at least 30 effective events;
2. the gap (win rate minus price) is in the claimed direction, and its range excludes zero;
3. the named side's return per $1 after fees is above zero, and its range excludes zero.

**Ranges for this test:** because there are two looks, the ranges are 97.5% instead of 95%
(z = 2.24). That keeps the chance of a false "supported" across both looks near 5%.

**Other outcomes:** a hypothesis that has enough events but fails conditions 2 or 3 is NOT SUPPORTED.
One without enough events is NOT YET TESTABLE.

## Looks (fixed in advance)

| Look | Date (run on or after) | Test data |
|---|---|---|
| 1 | January 15, 2027 | Oct 6 - Dec 31, 2026 (complete months only) |
| 2 (final) | July 15, 2027 | Oct 6, 2026 - Jun 30, 2027 |

- **How the dates were chosen:** from the July-September 2026 data, about 2-3 months of fresh data
  should be enough for H2 and H4 to reach 30 effective events, and to be decided if their effects are
  as large as they looked. H1 needs about 2.6 months at its observed size. Effects found by searching
  usually shrink on re-test, so H1 and H3 may stay NOT YET TESTABLE or undecided until look 2.
- **Before look 1:** the report shows only how many events have accumulated, never results.
- **After look 2:** no further looks at these hypotheses.

## Exploratory part

The re-test report may also search the earlier data for new patterns, with the same rules as the first
study. Anything found there is labelled exploratory and would need its own future test set.

## Addendum, October 6, 2026: resting-order hypotheses H5 and H6

Added at the owner's request on October 6, 2026, before any trade from October 6 onward had been
pulled or looked at. The fresh re-test pull has not been run yet, and nothing in the logger's live data
from October 6 on has been examined. H1-H4 and every rule above are unchanged. The git commit that adds
this section is the timestamp for H5 and H6. The rules are the code in `kalshi_logger/rest_study.py`
(`RERUN_GROUPS`, `simulate`, `rerun_results`, `rerun_verdict`) as of that commit.

| # | Orders | Claim |
|---|---|---|
| H5 | Mentions: resting NO order at the best NO bid when NO costs 30-60c | makes money after fees on the contracts that fill |
| H6 | Entertainment: the same | the same |

**Order moments.** Every trade in the fresh sample (from October 6, 2026, complete months only) in that
category with a YES price of 40-70c, at most 3 per market per month (fixed seed per month). The order
goes in at the close of the 1-minute candle containing the trade, at the best NO bid then (1 minus the
YES ask). It is kept only if that NO price is 30-60c.

**Fill rules (the resting-order study's, with realistic sizing):**
- About $100 per order, worked through smaller pieces. At most a set number of contracts rest at a time:
  24 for Mentions, 200 for Entertainment. That is the median size at the best price in the logger's
  snapshots, frozen here.
- Each piece waits behind a queue of the same size (24 / 200 contracts), frozen here. When a piece has
  fully filled, the next is posted at the same price at the back of the queue.
- A taker buying YES at our price fills the queue first, then us, contract for contract.
- A taker buying YES at a higher price clears the queue ahead, and fills us with that trade's own
  contracts only.
- Pieces stop when $100 has filled or the wait ends. Unfilled pieces are cancelled at no cost.
- Three wait times: 5 minutes, 1 hour, until close.

**Measure.** Return per $1 on the filled contracts at settlement, after the maker fee in effect at the
order time. Ranges are clustered by event. Effective events are computed from dollars filled per event.

**SUPPORTED at a look** if, at any one of the three wait times, all of these hold:
- there are at least 30 events with fills, and at least 30 effective events;
- the return's range is above zero.

The range is 99.2% (z = 2.64): 97.5% for two looks, split again three ways for the three wait times. The
other outcomes are NOT SUPPORTED (testable but no wait time passes) and NOT YET TESTABLE. Looks and the
results-hidden rule are the same as H1-H4.

**Forward paper trading** (the logger's live simulated orders, also from October 6, 2026) is a separate
measurement and is not this test. Its weekly numbers will be visible, so they must not be used to
change H5 or H6. Any change would need a new pre-registration and a new test period.
