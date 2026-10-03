# Sports odds data sources (Priority 4 research; nothing built yet)

Researched 2026-10-03. **How far this is verified:** this environment couldn't open the providers'
websites directly. Facts come from search results limited to each provider's own site, so treat
prices as very likely current and check the linked page before paying. Anything marked **not
verified** couldn't be confirmed. Nothing here needs a paid signup until you decide to buy.

## What we'd need

- **(a)** De-vigged game-winner lines from a *sharp* sportsbook (Pinnacle, Circa, BookMaker, BetCRIS),
  for a fair probability on Kalshi game-winner markets.
- **(b)** Player prop odds across several sportsbooks, ideally including a sharp one, for fair
  values on Kalshi player props.

"De-vig" means removing the bookmaker's margin so the two sides add to 100%. For a two-way
moneyline that's simple arithmetic, so a raw sharp line is enough for (a).

## Comparison

| Provider | Cost (per month) | Free tier | Sharp books? | Sharp **player props**? | Gives no-vig line? | Request limits / speed |
|---|---|---|---|---|---|---|
| **TheRundown** | Starter $49, Pro $149, higher tiers to $2,499 | 20K data points/day; only BetMGM, DraftKings, FanDuel; pre-game; 5-min delay | Yes: Pinnacle, BookMaker, BetCRIS, Circa, LowVig, BetOnline (also Kalshi, Polymarket, ProphetX, Novig) | Props from Starter up; Pinnacle/Circa props listed but depth **not verified** | No (easy to compute); Pro adds opening/closing lines and +EV calculations | Starter 25M data points, 60 s delay, 7 days history; Pro 125M, 30 s delay, 30 days history |
| **The Odds API** | $30, $59, $119, $249 | 500 credits/month | Pinnacle (copied from Pinnacle's public site, may lag), BetOnline, Novig, ProphetX | **No.** Props only from US/AU books | No | Credits per call = markets x regions; history back to June 2020 at 10x credits |
| **SportsGameOdds** | Rookie $149 ($99 if paid yearly), Pro $499 ($299 yearly) | 2,500 objects, 8 leagues, 9 books, 10 req/min | Pinnacle among ~77 books | Claimed, **not verified** | **Yes:** `fairOdds` (a no-vig consensus across books, not Pinnacle-only) | Rookie updates every 3 min; Pro under 1 min; charged per game, not per market |
| **SharpAPI** | Hobby $79, Pro $229, Sharp $399 | DraftKings + FanDuel, 12 req/min | Pinnacle only on $399 tier | Unclear (its own pages disagree) | Yes | n/a |
| **OddsPapi** | Paid tier quoted as both $29 and $49 (**unclear**) | 250 requests/month, all books, history included | Pinnacle, Singbet, SBOBet, Novig, ProphetX | Claimed, **not verified** | Not verified | WebSocket on paid tiers |
| **Unabated** | From $3,000 | None | Circa, BookMaker, a Pinnacle-like book | Yes | Yes (the "Unabated Line") | Real-time |
| OpticOdds (OddsJam) | Quote only | No | 200+ books; Pinnacle **not verified** | Yes, many books | Some | Real-time, aimed at businesses |
| SportsDataIO / Sportradar | Quote only | Trials | No sharp book found | Sportradar props: soft books only | Consensus only | Aimed at businesses |
| Pinnacle directly | Closed to the public since July 2025 | | | | | |
| Betfair Exchange | Blocked from US internet addresses | | | | | |
| Polymarket | Free, no key | Yes | Exchange prices (a cross-check, not a sportsbook) | Limited | Prices are already probabilities | High limits |

Sources: therundown.io/pricing/api, docs.therundown.io/faq, the-odds-api.com (bookmaker list
and historical data pages), sportsgameodds.com/pricing and /docs/info/rate-limiting,
sharpapi.io/features/no-vig-odds-api, oddspapi.io/sportsbooks/pinnacle, unabated.com/get-unabated-api,
developer.opticodds.com, developer.sportradar.com (player props FAQ),
github.com/pinnacleapi/pinnacleapi-documentation, support.developer.betfair.com,
docs.polymarket.com/api-reference/rate-limits.

**Terms of use:** none of the providers' terms could be read here, so whether any restricts using
their data for your own trading decisions is **not verified**. Most terms target redistributing or
displaying data. Check before buying.

## Recommendation

1. **(a) Sharp game-winner lines: TheRundown Starter, $49/month.** It's the cheapest plan with
   Pinnacle, Circa, BookMaker and BetCRIS moneylines; we'd de-vig them ourselves. It also includes
   Kalshi and Polymarket prices for comparison. A 60-second delay is fine for research; Pro ($149)
   halves it. For years of back-history instead, The Odds API ($30-59) is the alternative, but its
   Pinnacle data is copied from Pinnacle's website and may lag.
2. **(b) Player props across books: probably TheRundown too, but test first.** Use its free tier or
   a single month of Starter to check whether Pinnacle and Circa props actually appear for NFL,
   NBA, MLB and NHL. If they're thin, SportsGameOdds Rookie ($99-149) gives a ready-made no-vig
   consensus across ~77 books. Sharp books post few props, so expect fair prop values to come from
   a consensus of many books rather than one sharp line. Unabated has true sharp props but at
   $3,000+ is out of scope.
3. **Is anything free good enough to start?** For spot checks yes, for continuous logging no.
   OddsPapi's free tier (250 calls/month with Pinnacle and history), SportsGameOdds' free tier, and
   The Odds API's 500 free credits are enough to prototype a few games a day. Plan on about
   $49/month once we build the sports fair-value model.
