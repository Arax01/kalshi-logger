# How it works

This is the detail behind the README: sources, the crypto model, what is stored, and known
limitations. Everything here was checked against Kalshi's own documentation and live API on
2026-10-03.

## Sources and limits

| Source | Used for | Key needed? |
|---|---|---|
| Kalshi public market data (`api.elections.kalshi.com/trade-api/v2`): markets, series, trade feed, events, game ("milestone") live data, settlement-index chart | everything on Kalshi | No |
| Deribit public API (`www.deribit.com/api/v2/public`) | BTC/ETH index price and option implied volatility | No |
| Coinbase Exchange public ticker | second spot price, cross-check only | No |

**Kalshi's documented rate limits** (docs.kalshi.com, "Rate Limits and Tiers"): requests cost
tokens (10 per request by default); a Basic account refills 200 read tokens per second, which is
20 requests per second, with up to 3 seconds of burst. Over the limit Kalshi answers
`429 Too Many Requests`, with no penalty beyond waiting. Limits for unauthenticated access are
not published. This program caps itself at 2 requests per second across all jobs, and every job
slows down when any request gets a 429. Typical use is about 0.5 requests per second.

## Kalshi fees

From Kalshi's documentation:
- The taker fee per fill is `0.07 x fee_multiplier x contracts x price x (1 - price)`. The 0.07
  comes from Kalshi's "Fee Rounding" worked example ($0.00363825 for 1 contract at $0.055).
  Each series' `fee_type` and `fee_multiplier` come from the API. Some series are fee-free
  (multiplier 0).
- The fee is rounded up to $0.000001, then the balance change is rounded to the account's precision.
  We assume the conservative case and round the order's total fee **up to the next cent**.
- Resting (maker) orders are free on most series. Our gaps assume you **take** the displayed price
  and pay the taker fee.
- Not verified: the "flat" fee table is in a PDF on kalshi.com that blocks automated downloads.
  Markets in "flat" series get no fee estimate rather than a guess. None of the crypto series use it.

## Priority 1: market scanner

Every 20 minutes the scanner reads all open markets except combos (about 130,000, at 1,000 per
request, taking about 4 minutes). For each market it stores best YES bid and ask, size at each,
last price, volume, 24-hour volume and open interest. A row is written only when the bid, ask or
volume changed since the market's last row, or at least every 2 hours. That keeps the database
small; a market missing from a scan was unchanged. Each row also stores the change in mid price
and the contracts traded since the previous row, both left blank if a data gap lies in between.

**Grouping.** Category and tags come from each market's series. Sports markets are grouped by
league and type using Kalshi's ticker naming (for example `KXNFLGAME` is the game winner,
`KXNBAPTS` is player points, `KXMLBHR` is player home runs):
- game winner
- game line (spreads, totals, quarters and halves)
- player prop, with a stat type such as points, rebounds, passing yards or home runs
- other sports (futures, awards and so on)

**Combos** are parlays built from other markets. They have no standing order book (they're priced
on request) and new ones appear constantly: about 36,000 different combos traded in one
15-minute window on a Saturday. So instead of scanning them all, the scanner reads Kalshi's public
trade feed since the last scan. It stores trade statistics (count, contracts, average price, low
and high) for the 2,000 most-traded combos. For the 300 most-traded it also fetches the combo
itself, including its legs, so the weekly report can compare the combo's price with its legs'
prices. On extremely busy days the trade feed is capped at 400,000 trades per scan, and the report
says when that happened.

**Combo premium in the weekly report.** Each sampled combo's traded price is divided by the product
of its legs' Kalshi mid prices, which is the fair price if the legs were unrelated. Combos are split by
how related their legs can be. The game is read from each leg's ticker; for example `26OCT03SYRCONN` is
shared by that game's winner, spread, total and player-prop markets.
- **Cross-game:** every leg from a different game. The legs are close to unrelated, so a price
  above the legs' product is premium.
- **Same-game:** all legs from one game. The legs are correlated, so the product understates fair
  value and some of the difference is not premium.
- **Mixed:** some of each.

The report gives median and contract-weighted ratios, how much of the overall premium survives in
cross-game combos, and, once combos settle, the average price paid versus how often they won and
buyers' return per dollar (before Kalshi's fee).

**Can a regular account sell or quote combos?** In the Kalshi app, no: you request a combo and
accept or reject the price makers offer. Through the API, Kalshi says any member can respond to
combo requests ("Any Kalshi member may respond to RFQs via Kalshi's API, which is accessible to all
traders", a Kalshi spokesperson quoted by Sportico). Kalshi's RFQ documentation describes requests
being "broadcast to all makers" and lists no extra approval step.

In practice it means:
- an API key with trading permission
- software that prices each combo automatically
- confirming within 3 seconds of acceptance (combos are classed as high-volatility markets)
- competing with professional firms

Quoters may also pay a maker fee: Kalshi's changelog describes a 0.5 maker-fee multiplier for
combo quoters in some cases. This logger does none of that; it only reads public data.

**Order-book snapshots (books job).** Every 3 minutes, for each open Entertainment or Mentions market
that traded at least 100 contracts in the last 24 hours (about 750-850 markets), the logger stores
the top 3 price levels on each side: YES bids and NO bids, with the contracts at each. A NO bid at q
is a YES ask at 1 - q.
- **Request rate:** one public request covers 100 markets, so this is about 0.05 requests per second.
- **Storage:** a row is written only when those levels change, or hourly. In testing about 17% of
  books changed in each 3-minute round, so this is about 5 MB a day (up to about 10 MB on busy days).
- **Purpose:** this data narrows the queue assumption in the resting-order study.

## Priority 2: crypto fair value

**Markets covered:** BTC and ETH above/below (hourly, daily, weekly), ranges, 15-minute up/down,
and year-end ranges. Markets that pay if the price touches a level at any time are not covered;
they need a different model.

**The model, in plain English.** Options traders on Deribit put a price on how much BTC or ETH is
likely to move by a given date (its "implied volatility"). We take the current price and that
expected size of moves, and assume moves are random with no up or down bias and roughly
bell-shaped in percentage terms. That gives the probability the price ends above (or below, or
inside) the Kalshi strike. A range is "above the low end" minus "above the high end".

Details that matter:
1. **Volatility at the right strike.** Options price big moves as likelier than a pure bell curve
   would. We use the volatility the options market prices at the market's own strike: a
   "smile", built from out-of-the-money options and interpolated by distance from the price.
2. **Volatility at the right time (interpolation).** Deribit options expire at 08:00 UTC on fixed
   dates; Kalshi markets close at other times. We interpolate between the two Deribit expiries
   either side of the Kalshi close, linearly in total variance (vol squared x time) at the same
   strike. When the Kalshi close is before the first Deribit expiry (more than 1 hour away), we
   interpolate between zero variance now and that first expiry, which means using its vol. These
   rows are labelled `before_first_expiry`. Every row stores the vol used, the method, both Deribit
   expiries and their vols, so any number can be audited.
3. **Settlement averaging.** Kalshi's rules: the result uses "the simple average of the sixty seconds
   of CF Benchmarks' Real-Time Index before" the close. Averaging the last minute removes about
   40 seconds of price variance, so we use time-to-close minus 40 seconds. No fair value is computed
   in the last 2 minutes, when part of the average is already locked in.
4. **Timing.** The spot price is read immediately before and after each batch of Kalshi quotes and
   averaged. If the price moved meaningfully in between, the row is flagged.
5. **Price used.** Deribit's index (an average of major exchanges, like the CF Benchmarks index Kalshi
   settles on). Coinbase's price and Kalshi's own chart of the settlement index are stored alongside
   as cross-checks.

**What gets stored.** To keep the database small without losing any gap:
- every check where taking a price would gain more than 2c after fees is stored, so each gap over
  the 3c threshold is captured from start to finish;
- otherwise, strikes whose fair value is under 2% or over 98% are skipped;
- markets closing more than a day away are stored every 10 minutes;
- markets closing within a day are stored on every 2-minute check.

That's about 80 rows per check, around 10-15 MB a day.

**Far-away strikes** (fair value under 2% or over 98%, with at least one price quoted) go into a
separate table, `crypto_far`, every 30 minutes. They're kept apart so the gap tracking and accuracy
scores above are not affected. That's about 1,300 rows per half hour, roughly 4 MB a day, so crypto
in total stays around 15 MB a day. They feed the longshot section of the weekly report.

**Gap after fees, per contract:**
- Buy YES: `fair - best ask - taker fee` (positive means YES looks cheap)
- Buy NO (the same as selling YES): `best bid - fair - taker fee` (positive means YES looks expensive)

The fee is calculated for the full size shown at that price.

**What we have already seen (one afternoon of testing).** For weekly markets our fair value was
within about 1 cent of Kalshi's mid price on average, which is a good sign the model is sound.
For markets closing within the day the gap averaged 6-7 cents: Kalshi priced a much narrower
spread of outcomes than the 1-day options did. That could be a real inefficiency, or the options
vol could be too high for the next hour, since it includes overnight risk. To help tell which, each
row also stores an **audit fair value** using volatility measured from Kalshi's own 1-second
settlement-index chart over the last 3 hours (`fair_yes_realised`). The daily report scores both
against actual outcomes. Treat `before_first_expiry` gaps with caution until that scoring is in.

**Weekly test: real gap, or wrong volatility input?** The weekly report has a section on markets
closing within 24 hours. It breaks results down by time left to close (under 15 min, 15-30 min,
30-60 min, 1-3 h, 3-8 h, 8-24 h) and by time of day, in 4-hour blocks of your computer's local time.
For each bucket it shows:
- Accuracy (Brier score) against outcomes for three forecasts: our fair value with options vol, the
  same model with recent measured vol, and Kalshi's price.
- Three volatility numbers: the options vol we used, recent measured vol, and **Kalshi-implied vol**
  (the vol at which our model reproduces Kalshi's price, worked out for above/below markets). By
  time of day it also shows the vol that actually happened, measured from the logged spot prices.
- The number of gaps over 3c and the average result per contract had you taken each one, after fees.

**How to read it:**
- **Volatility input is wrong:** Kalshi forecasts better than our options-vol fair value, Kalshi's
  implied vol is closer to what happened, and the gaps lose money when taken.
- **Real pricing difference:** our fair value forecasts at least as well and the gaps make money.

The report states a verdict only once there are at least 50 settled intraday markets and 30 settled gaps.

**Weekly test: longshot bias.** "Longshot bias" means cheap contracts win less often than their price
implies. The weekly report looks at every BTC/ETH contract offered under 10c: YES at the ask, or NO
at 100c minus the YES bid. Data comes from both `crypto_fv` and `crypto_far`. Each market counts once
per side, at its average price while under 10c. For settled contracts it shows:
- average price paid
- our fair value
- how often they actually won, with a rough 95% range
- buyers' return per $1 after Kalshi's taker fee

Results are broken down by price (1c, 2-3c, 4-6c, 7-9c), by side, and by whether the market closes
within 24 hours. Strikes in the same event win or lose together, so the report also counts distinct
events: the evidence is closer to that number than to the number of contracts.

## Priority 3: in-game sports

Kalshi publishes a free live feed for each game (a "milestone") with score, period, clock, last play
and, for baseball, bases and outs, from Stats Perform. Each milestone lists its Kalshi event
tickers, so games are matched to markets exactly, with no team-name matching. That is why college
football could be included from the start.

Every 60 seconds, for each game in progress in NFL, NBA, MLB, NHL or college football, we store each
game-winner market's bid, ask, size, last price and volume, plus score, period and clock. The full
feed payload is stored whenever it changes. The feed's own "last updated" time is stored next to
our fetch time, so its delay can be measured later. Kalshi doesn't publish a delay figure;
expect seconds to tens of seconds behind live play. About 3 requests per minute are needed, even
on a busy Saturday.

## Football backfill and overreaction study

`backfill.bat` downloads finished 2026 NFL and college football games that Kalshi links to
markets, using two public Kalshi sources:
- **play-by-play:** every play, with the score and the time Kalshi's feed first saw it;
- **1-minute candles:** best bid, best ask and last trade price for each team's game-winner market.

Everything goes into separate `bf_` tables, never mixed with live logging:

| Table | One row per |
|---|---|
| `bf_games` | game: league, preseason or regular season, kick-off, and how far its download got |
| `bf_markets` | game-winner market, and which team (home or away) its YES side is |
| `bf_plays` | play: time first seen, quarter, clock, score, play type, turnover |
| `bf_minutes` | market per minute; `observed = 0` marks a minute with no candle |

For a minute with no candle, prices are carried forward from the last real candle and flagged
(`observed = 0`), so analysis can always tell real prices from repeated ones. Games without
timestamped plays are kept but marked `no_timestamps`, and are not used.

It writes `reports/football_overreaction.txt`. The method:
- **Big moments:** touchdowns, field goals, safeties, turnovers, and sudden 2-minute price swings.
  A play counts from the minute Kalshi's feed first saw it.
- **No look-ahead:** we act one full minute after a play, at that minute's closing bid and ask.
  Price swings use only closed minutes. Trades are held a fixed time and closed at the last
  available minute if the price data ends first.
- **Measurement:** the price move around the moment, then where the price is 5, 15 and 30 minutes
  later. Does it keep going or come back?
- **Fade rule:** bet against the initial move, paying the spread and Kalshi's taker fee on both
  trades. Results are per contract; candles do not show how many contracts were available.
- **Train/test split:** the rule's settings are picked using games before September 20, 2026 (US
  Eastern), then applied unchanged to games on or after that date.
- **Preseason:** NFL preseason is kept out of both and reported separately.

## Calibration study

`calibration.bat` asks: across Kalshi, do contracts priced at X cents win X% of the time, and where
are they off by more than fees? It writes `reports/calibration_study.txt`.

**The sample.**
- **Random windows:** January 2025 to now, 150 random time windows per month, each sized for about
  1,000 trades. Windows are longer in quiet months, so 2025 isn't drowned out by 2026. The windows are
  fixed by a seed, so re-running picks the same ones.
- **Results:** for every sampled trade's market, its final result, event, category and lifetime volume.
- **Thin-category top-up:** any category with fewer than 200 settled events gets extra events sampled
  directly from its series, with their trades.
- **Fee history:** Kalshi's record of when each series' fees changed, so every trade gets the fee in
  effect at the time.

Everything goes into separate `calib_` tables. The pull can be stopped and resumed.

**The analysis:**
- **Buckets:** trades are bucketed by YES price (1-5c, 5-10c, 10-20c ... 90-95c, 95-99c). Each bucket
  shows average price paid against how often YES actually won, weighted by contracts.
- **Events, not contracts:** confidence ranges count each event once, since strikes in the same event
  win or lose together. Both event and contract counts are shown.
- **Returns per $1 after fees, four ways:** YES or NO, each bought by a taker (crossed the spread, paid
  the taker fee) or by a resting maker order (paid the maker fee, which most series don't charge).
  A pattern is only capturable as a taker if the taker number is positive.
- **Holdout:** patterns are looked for only in trades before July 1, 2026, then checked once on trades
  from July 1, 2026 on.
- **Edge decay:** each pattern is broken down by quarter, to see whether it shrank as volume grew.
- **Tradeability flag:** a bucket is flagged if it has fewer than 30 events, if its markets' median
  lifetime volume is under 1,000 contracts, or if one event makes up over a quarter of its contracts.

**The holdout is not clean.** While building the report, results that included post-cutoff trades
were seen before three rules were finalised:
- the 'effective events' discount;
- a pattern's side must match the direction of the price gap;
- 'capturable as a taker' needs the taker return's range above zero.

Section 6 of the report shows what is confirmed under the rules as first written and under each
change. The three patterns confirmed under the current rules are confirmed under every rule set.
The extra ones under the original rules rest on very few independent events.

**A clean re-test (pre-registered).** The rules are frozen and four hypotheses are named in
advance in `docs/preregistration-calibration-rerun.md`. One of them is Combos 90-95c YES as a taker.
They are tested only on trades from October 6, 2026. Run `calibration_rerun.bat` on or after January
15, 2027 (first look) and again on or after July 15, 2027 (final look). Before the first look date
the re-test report shows only how much fresh data has accumulated, never results.

Two more hypotheses were added on October 6, 2026, before any October 6+ data was pulled.
- **H5 (Mentions) and H6 (Entertainment):** resting NO orders at 30-60c, under the resting-order
  study's fill rules with realistic sizing, at each of the three wait times.
- **Data:** the re-test pull draws order moments from the fresh sample and replays them once their
  market has settled.

## Resting-order study

`resting_orders.bat` asks: the calibration study says some prices are cheap for a resting (maker)
order, but would a ~$100 resting order actually get filled, and are the fills you get the bad ones?
It writes `reports/resting_orders.txt`. It places no orders; it replays history.

**Which orders.** About 350 moments per group are drawn at random from the calibration study's
sampled trades (at most 3 per market, fixed seed):
- Entertainment and Mentions: a NO bid at the best NO bid when NO costs 30-60c;
- Crypto, as a comparison: a YES bid at the best YES bid when YES costs 95-99c.
Combos are left out. Each order is about $100 (contracts = $100 / price).

**Did it fill.** Kalshi's 1-minute prices give the best bid and ask at that moment. Kalshi's public
trade history then decides the fill: our order fills only after the contracts already waiting at
that price (the queue) have traded, and a trade at a worse price means everything at ours was used up.
The real queue is never visible, so three cases are shown: front (nobody ahead, optimistic),
typical (median logged size at the best price) and long (90th percentile, pessimistic). The sizes
come from the logger's own snapshots (order books, scans, crypto checks); before there are any, it uses
the sizes measured in October 2026. Results are shown for three waits: 5 minutes, 1 hour, and until
the market closes (unfilled orders are cancelled and cost nothing).

**Sizing.** Realistic sizing is the default. The $100 is worked in pieces no bigger than the typical
size at the best price (about 24 contracts in Mentions, 200 in Entertainment). Each piece joins the
back of the queue, and the next is posted only after it fills. A trade at a worse price than ours fills
us only with that trade's own contracts. The first run (October 5) let the whole $100 order rest at
once, and let a single trade through our price fill all of it. The report's last section shows both
ways side by side.

**What it reports.** Fill rate; typical wait to the first fill; return per $1 on the filled
contracts at settlement after the maker fee then in effect (with event-clustered 95% ranges); the
same orders as if every one had filled instantly (the gap between the two is the adverse-selection
cost); expected profit per $100 order placed, counting unfilled orders as zero; and how the price
moved 5 and 30 minutes after a fill.

Its tables (`rest_points`, `rest_tape`, `rest_tape_done`) are separate. The pull can be stopped and resumed.

## Data gaps

Each job (scanner, crypto, in-game, results, reports) records the time of its last success. If a
job hasn't succeeded for more than 2.5 times its interval, the period is written to the `gaps`
table with a reason:
- **computer asleep:** detected when a 1-second wait suddenly takes minutes
- **logger not running:** stopped, or the computer was off
- **logger was closed without stop.bat:** the window was closed, or a crash or power cut
- **errors:** for example, no internet

Reports list gaps and never treat them as quiet markets.

**If the logger is killed instead of stopped** (window closed, crash, power cut), the database is
safe. SQLite keeps every write that was saved before the kill and discards half-finished ones;
this was tested by killing the logger mid-scan, and the database passed SQLite's integrity check.
At the next start, the logger tidies up after the killed run:
- the run is marked as not stopped cleanly;
- a scan that was in progress is marked `interrupted`, keeping the rows it had already saved;
- each job's downtime becomes a gap with the reason above.

The logger saves a heartbeat every minute, so a job that was killed before it ever finished a run
still gets a gap, measured from the last heartbeat. Short interruptions follow the same rule as
sleep: only downtime longer than 2.5 times a job's interval counts as a gap (about 5 minutes for
crypto, 2.5 minutes for in-game, 50 minutes for the scanner). Price changes still record the time
since the previous row, so nothing is misread. `status.bat` shows how the logger last stopped.

## The database (`data/kalshi.db`)

| Table | One row per |
|---|---|
| `markets` | market ever seen: ticker, title, category, group, sport, league, stat type, strikes, close time, **final result** |
| `scan_snapshots` | market per scan when something changed. Prices are in centi-cents (1 = $0.0001). |
| `scans` | scan: timing, status, markets seen, combo trading totals |
| `combo_trades` | combo per scan window: trades, contracts, average price, low and high |
| `crypto_fv` | crypto market per check: spot prices, vol and how it was derived, fair value, audit fair value, quotes, sizes, fees, gaps |
| `crypto_far` | far-away crypto strike per 30 minutes: fair value, vol, quotes and sizes (for the longshot report) |
| `games`, `game_snapshots` | game, and game-winner market per minute while the game is in progress |
| `book_snapshots` | Entertainment/Mentions market per change: top 3 YES and NO bids with sizes |
| `gaps` | period without data, with a reason |
| `process_runs` | each time the logger ran: start, last heartbeat, end, and whether it was stopped cleanly |
| `series` | Kalshi series: category, tags, fee type and multiplier |

All times are Unix seconds (UTC).

## Known limitations

- Kalshi's public access limits are unpublished. Testing from a shared cloud server saw occasional
  "slow down" replies even at 1 request per second, probably from other users on the same server.
  From a home connection this should be rarer. The logger backs off automatically either way.
- Spreads in the weekly report are taken from the rows written when something changed, not
  averaged evenly over time.
- Weekly "contracts traded" counts trading between scans that the logger saw. Trading during a gap
  isn't attributed to any one period.
- Combo price comparisons use the legs' mid prices from the nearest scan (up to 20 minutes earlier).
