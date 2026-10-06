# Kalshi Logger

A program that runs on your laptop and quietly records Kalshi prices into a file, so we can study
where Kalshi prices are least efficient. **It never trades.** It has no ability to place, change or
cancel orders, and it needs no Kalshi account or API key. Everything it reads is public.

## What it records

| What | How often | Why |
|---|---|---|
| **Every open Kalshi market** (about 130,000): best bid and ask, the spread, size at those prices, volume, price change since the last check. Sorted by category and series, with player props broken out by sport and stat type (points, rebounds, passing yards...). Busy combos (parlays) are recorded from Kalshi's public trade feed. | every 20 min | Find where spreads are wide or prices jump around relative to trading |
| **Bitcoin and Ethereum price markets**: our own "fair" probability from the live price and options-market volatility, next to Kalshi's prices, size, and the gap after Kalshi's fees, for buying and for selling | every 2 min | A control group: a market where fair value can be calculated |
| **NFL, NBA, MLB, NHL and college football games in progress**: game-winner prices next to the score, quarter or inning, clock and last play | every 60 s | Later: do prices overreact to big moments and then come back? |
| **The final result of every market above** | hourly | To check whether fair values and price moves were right |

Reports are written to the `reports` folder in plain English:
- **Daily crypto summary**: how many gaps over 3 cents after fees appeared, how long they lasted,
  how much you could have traded, what taking them would actually have returned, and how our
  fair values compared with what happened.
- **Weekly scanner report**: which categories, series and player prop types have the widest spreads
  among actively traded markets, which move the most for the amount traded, how combos are
  priced compared with their individual legs (split into same-game and cross-game combos), and a
  crypto check of whether short-term gaps are real or a sign our volatility input is off, and a
  longshot check: do crypto contracts priced under 10 cents win as often as their price says? It
  also shows what the in-game logger collected. If the scanner didn't run that week, the report
  says so and still includes the crypto, longshot and in-game sections.

## One-time setup (Windows)

1. **Install Python.** Go to https://www.python.org/downloads/ and click the yellow "Download
   Python 3.x" button. Run the installer and **tick "Add python.exe to PATH"** at the bottom of
   the first screen, then click "Install Now".
2. **Download this project.** Easiest: install GitHub Desktop (https://desktop.github.com), sign
   in, choose *File > Clone repository*, pick `Arax01/kalshi-logger`, and note the folder it saves to.
   (Or, on github.com, open the repository, click the green **Code** button, then **Download ZIP**,
   and extract it somewhere like `Documents\kalshi-logger`. If Windows later warns about the files,
   right-click the ZIP before extracting, choose *Properties*, tick *Unblock*, and click OK.)
   Until the work is merged into `main`, choose the branch `claude/blissful-hawking-f8wpco` first
   (GitHub Desktop: *Current branch*; website: the branch drop-down above the file list).
3. **Open the project folder and double-click `setup.bat`.** It takes a minute and says
   "Setup complete" when done.

## Everyday use

Double-click these files in the project folder:

| File | What it does |
|---|---|
| `start.bat` | Starts logging in a small minimized window called "Kalshi Logger". |
| `stop.bat` | Stops logging cleanly (it finishes what it is doing first, usually within seconds). |
| `status.bat` | Shows whether it is running, what it has collected, and any gaps in the data. |
| `report.bat` | Writes any reports that are due, plus a preview of the latest data, and opens the reports folder. |
| `calibration.bat` | One time (about 1.5-2 hours, ~250 MB): samples historical Kalshi trades and writes the calibration study. Can be stopped and restarted. |
| `calibration_rerun.bat` | The pre-registered clean re-test of the calibration study. Run on or after January 15, 2027, and again on or after July 15, 2027. |
| `resting_orders.bat` | One time (about 1 hour, after `calibration.bat`): replays Kalshi's trade history to see whether a ~$100 resting order would have filled and what it would have earned. Writes `reports/resting_orders.txt`. Places no orders. |
| `backfill.bat` | One time (about 20-30 min): downloads this season's NFL and college football games from Kalshi and writes the football overreaction report. Re-run it later to add newer games. |

**To start it automatically when you log in:** press `Windows key + R`, type `shell:startup`,
press Enter, then right-click `start.bat` > *Show more options* > *Create shortcut* and move the
shortcut into the folder that opened.

## When the laptop sleeps

Nothing breaks. When the laptop wakes up the logger carries on, and it records the missing period
as a "gap" so no one mistakes it for a quiet market. Price changes are never calculated across a
gap. Reports that should have been written while the laptop was asleep are written as soon as
the logger is running again. `status.bat` lists recent gaps.

The same applies if you close the logger's window instead of using `stop.bat`: the data saved up to
that moment is kept, and the downtime is recorded as a gap ("closed without stop.bat"). `stop.bat`
is still the tidier way to stop it.

## Where things are kept

- `data\kalshi.db` is all the recorded data, in one SQLite database file. Expect up to about 110 MB a day
  on busy sports days, less on quiet days (roughly 1.7-3.3 GB a month). That includes about 5-10 MB a day
  of order-book snapshots for Entertainment and Mentions markets. To start over, stop the logger and delete the `data`
  folder.
- `reports\` holds the reports. `logs\` holds a technical log, useful if something goes wrong.
- None of these are uploaded anywhere or saved to GitHub.

## Updating

GitHub Desktop: click *Fetch origin* then *Pull*. ZIP: download again and copy your old `data` and
`reports` folders into the new folder. Run `setup.bat` again after updating. Existing data is
kept and upgraded automatically.

## Good to know

- **Kalshi's limits:** Kalshi allows a basic account 20 requests per second. Kalshi doesn't publish
  limits for public (no-login) access, so this program sends at most 2 per second (a tenth of that)
  and averages about 0.5 per second. If Kalshi ever says "slow down", it automatically backs off.
- **Fees:** the gaps are measured after Kalshi's taker fee (7% x price x (1 - price) per contract,
  rounded up to the cent, from Kalshi's own documentation).
- **Live scores** come from Kalshi's own free game feed (provided by Stats Perform). It runs
  somewhere between a few seconds and tens of seconds behind the real game. That's fine for
  after-the-fact analysis, not for trading.
- More detail (the crypto model, exactly what is stored, known limitations):
  [docs/how-it-works.md](docs/how-it-works.md). Research on sports odds data for later:
  [docs/odds-data-sources.md](docs/odds-data-sources.md).
