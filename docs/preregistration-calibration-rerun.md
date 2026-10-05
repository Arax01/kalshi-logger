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
