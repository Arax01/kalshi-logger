"""Calibration study report: do contracts priced at X cents win X% of the time?

Reads the sampled trades (calib_trades) and final results (calib_markets) and writes a plain-English
report, reports/calibration_study.txt.

Method, fixed before looking at results:
* Holdout: patterns are looked for only in trades before HOLDOUT_START; trades from HOLDOUT_START on
  are used once, to confirm or reject them.
* Each trade is bucketed by its YES price. Within a bucket: average price paid vs how often YES won,
  weighted by contracts. Confidence ranges treat each EVENT as one unit (strikes in the same event win
  or lose together), so both contract and event counts are shown.
* Returns per $1 after fees for buying YES and buying NO at the traded price (taker fee), and
  separately for the taker (who crossed the spread, pays the taker fee) and the maker (who was resting,
  pays the maker fee if the series charges one), using the fee in effect at the time of the trade.
* A "pattern" in the discovery data must have >= MIN_EVENTS events, a calibration gap whose 95% range
  excludes zero, and a positive return after fees for at least one side. Patterns are then checked on
  the holdout and broken down by quarter to see whether they are shrinking.
"""
import bisect
import calendar
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import config, fees

HOLDOUT_START = calendar.timegm((2026, 7, 1, 0, 0, 0))
HOLDOUT_LABEL = "July 1, 2026"
BUCKETS = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.0001]
LABELS = ["1-5c", "5-10c", "10-20c", "20-30c", "30-40c", "40-50c", "50-60c", "60-70c", "70-80c", "80-90c",
          "90-95c", "95-99c"]
MIN_EVENTS = 30            # fewer events than this: too few to judge
THIN_MARKET_VOLUME = 1000  # median lifetime contracts per market below this: too thin to trade
MAKER_SHARE = {"quadratic": 0.0, "quadratic_with_maker_fees": 0.25, "quadratic_with_combo_maker_fees": 0.5}


# ---------- fees ----------

class FeeBook:
    """Fee type in effect for a series at a given time."""

    def __init__(self, conn):
        self.changes = defaultdict(list)
        for r in conn.execute("SELECT * FROM calib_fee_changes ORDER BY scheduled_ts"):
            self.changes[r["series_ticker"]].append((r["scheduled_ts"], r["fee_type"], r["fee_multiplier"]))
        self.current = {r["series_ticker"]: (r["fee_type"], r["fee_multiplier"])
                        for r in conn.execute("SELECT series_ticker, fee_type, fee_multiplier FROM series")}

    def at(self, series, ts):
        ch = self.changes.get(series)
        if ch:
            i = bisect.bisect_right([c[0] for c in ch], ts) - 1
            if i >= 0:
                return ch[i][1], ch[i][2]
            return "quadratic", 1.0        # before the first recorded change: the standard schedule
        ft, fm = self.current.get(series, ("quadratic", 1.0))
        return ft or "quadratic", 1.0 if fm is None else fm


def _fee(price, count, share, mult):
    """Fee in dollars for one fill: share x 0.07 x mult x C x P x (1-P), rounded up to the cent."""
    if share <= 0 or mult <= 0:
        return 0.0
    raw = share * fees.TAKER_RATE * mult * count * price * (1 - price)
    return math.ceil(round(raw * 100, 6)) / 100.0


def trade_fees(fee_type, mult, price, count):
    if fee_type not in MAKER_SHARE:
        fee_type = "quadratic"
    return _fee(price, count, 1.0, mult), _fee(price, count, MAKER_SHARE[fee_type], mult)


# ---------- accumulating, with event-clustered ranges ----------

WAYS = ("yes_t", "no_t", "yes_m", "no_m")   # the four real ways to hold a side of a trade


class Acc:
    """Per-event sums so every ratio gets a range that treats each event as one unit."""

    def __init__(self):
        self.ev = defaultdict(lambda: defaultdict(float))
        self.trades = 0
        self.markets = set()

    def add(self, event, market, p, c, won, taker_yes, f_t, f_m):
        e = self.ev[event]
        self.trades += 1
        self.markets.add(market)
        e["C"] += c
        e["W"] += c * won
        e["P"] += c * p
        # Calibration gap per contract: (won - price), numerator over contracts.
        e["gap_n"] += c * (won - p)
        e["gap_d"] += c
        # The two real sides of this trade. The taker crossed the spread (taker fee); the maker was resting
        # (maker fee, usually zero). yes_t = YES bought by a taker, yes_m = YES bought by a resting order, etc.
        yes_side, no_side = ("t", "m") if taker_yes else ("m", "t")
        fee = {"t": f_t, "m": f_m}
        e[f"yes_{yes_side}_n"] += c * won
        e[f"yes_{yes_side}_d"] += c * p + fee[yes_side]
        e[f"no_{no_side}_n"] += c * (1 - won)
        e[f"no_{no_side}_d"] += c * (1 - p) + fee[no_side]
        t_price, t_win = (p, won) if taker_yes else (1 - p, 1 - won)
        e["taker_n"] += c * t_win
        e["taker_d"] += c * t_price + f_t
        e["maker_n"] += c * (1 - t_win)
        e["maker_d"] += c * (1 - t_price) + f_m

    def n_events(self):
        return len(self.ev)

    def contracts(self):
        return sum(e["C"] for e in self.ev.values())

    def ratio(self, name):
        """Returns (value, low, high). For gap: per-contract difference; for others: return per $1."""
        N = sum(e[f"{name}_n"] for e in self.ev.values())
        D = sum(e[f"{name}_d"] for e in self.ev.values())
        if D <= 0:
            return None, None, None
        r = N / D
        var = sum((e[f"{name}_n"] - r * e[f"{name}_d"]) ** 2 for e in self.ev.values()) / (D * D)
        se = math.sqrt(var)
        off = 0.0 if name == "gap" else 1.0
        return r - off, r - off - 1.96 * se, r - off + 1.96 * se

    def avg_price(self):
        C = self.contracts()
        return sum(e["P"] for e in self.ev.values()) / C if C else None

    def win_rate(self):
        C = self.contracts()
        return sum(e["W"] for e in self.ev.values()) / C if C else None

    def top_event_share(self):
        C = self.contracts()
        return max(e["C"] for e in self.ev.values()) / C if C else None


# ---------- loading ----------

def _bucket(p):
    return min(max(bisect.bisect_right(BUCKETS, p) - 1, 0), len(LABELS) - 1)


def _quarter(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d.year} Q{(d.month - 1) // 3 + 1}"


def load(conn):
    """Yield one dict per usable trade (market settled YES or NO)."""
    book = FeeBook(conn)
    sql = ("SELECT t.ticker, t.ts, t.yes_price, t.count, t.taker_side, w.source, m.event_ticker, m.series_ticker, "
           "m.category, m.result, m.volume FROM calib_trades t JOIN calib_markets m ON m.ticker=t.ticker "
           "JOIN calib_windows w ON w.window_id=t.window_id WHERE m.result IN ('yes','no')")
    for r in conn.execute(sql):
        p = r["yes_price"]
        if not 0 < p < 1:
            continue
        ft, fm = book.at(r["series_ticker"], r["ts"])
        f_t, f_m = trade_fees(ft, fm if fm is not None else 1.0, p, r["count"])
        yield {"ticker": r["ticker"], "event": r["event_ticker"] or r["ticker"], "cat": r["category"] or "Other",
               "p": p, "c": r["count"], "won": 1.0 if r["result"] == "yes" else 0.0,
               "taker_yes": r["taker_side"] == "yes", "f_t": f_t, "f_m": f_m, "ts": r["ts"],
               "split": "holdout" if r["ts"] >= HOLDOUT_START else "discovery", "bucket": _bucket(p),
               "quarter": _quarter(r["ts"]), "source": r["source"], "volume": r["volume"]}


# ---------- report ----------

def _c(x, d=1):
    return "n/a" if x is None else f"{x * 100:+.{d}f}c"


def _pct(x, d=0):
    return "n/a" if x is None else f"{x * 100:+.{d}f}%"


def _rng(v, lo, hi, fmt):
    return "n/a" if v is None else f"{fmt(v)} ({fmt(lo)} to {fmt(hi)})"


def _table(headers, rows):
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), "  ".join("-" * w for w in widths)]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(out) if rows else "(nothing to show)"


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def thin_flag(acc, volumes):
    """Why a bucket's result isn't tradeable or trustworthy, or '' if it looks fine."""
    reasons = []
    if acc.n_events() < MIN_EVENTS:
        reasons.append(f"<{MIN_EVENTS} events")
    mv = _median(volumes)
    if mv is not None and mv < THIN_MARKET_VOLUME:
        reasons.append(f"thin markets (median {mv:,.0f} contracts)")
    if acc.top_event_share() and acc.top_event_share() > 0.25:
        reasons.append("one event >25% of contracts")
    return "; ".join(reasons)


def _bucket_rows(accs, vols):
    rows = []
    for b, label in enumerate(LABELS):
        a = accs.get(b)
        if not a or not a.trades:
            continue
        gap = a.ratio("gap")
        rows.append([label, f"{a.trades:,}", f"{a.contracts():,.0f}", a.n_events(),
                     f"{a.avg_price() * 100:.1f}c", f"{a.win_rate() * 100:.1f}%", _rng(*gap, _c)] +
                    [_pct(a.ratio(w)[0]) for w in WAYS] + [thin_flag(a, vols.get(b, [])) or "ok"])
    return rows


BUCKET_HEADERS = ["Price", "Trades", "Contracts", "Events", "Avg price paid", "YES won", "Won minus price (95% range)",
                  "YES taker", "NO taker", "YES maker", "NO maker", "Tradeable?"]


def build(conn):
    groups = {}          # (split, cat, bucket) -> Acc
    vols = defaultdict(list)
    quarters = {}        # (cat, bucket, quarter) -> Acc
    sources = defaultdict(int)
    for t in load(conn):
        key = (t["split"], t["cat"], t["bucket"])
        if key not in groups:
            groups[key] = Acc()
        groups[key].add(t["event"], t["ticker"], t["p"], t["c"], t["won"], t["taker_yes"], t["f_t"], t["f_m"])
        vols[key].append(t["volume"])
        qk = (t["cat"], t["bucket"], t["quarter"])
        if qk not in quarters:
            quarters[qk] = Acc()
        quarters[qk].add(t["event"], t["ticker"], t["p"], t["c"], t["won"], t["taker_yes"], t["f_t"], t["f_m"])
        sources[(t["split"], t["source"].split(":")[0])] += 1
    return groups, vols, quarters, sources


def find_patterns(groups, vols):
    out = []
    for (split, cat, b), a in groups.items():
        if split != "discovery" or a.n_events() < MIN_EVENTS:
            continue
        gap, lo, hi = a.ratio("gap")
        if gap is None or (lo <= 0 <= hi):
            continue
        returns = {m: a.ratio(m)[0] for m in WAYS}
        vals = [(v, m) for m, v in returns.items() if v is not None]
        if not vals:
            continue
        best = max(vals)
        if best[0] <= 0:
            continue
        out.append({"cat": cat, "bucket": b, "gap": (gap, lo, hi), "returns": returns, "best": best[1],
                    "events": a.n_events(), "thin": thin_flag(a, vols[(split, cat, b)])})
    out.sort(key=lambda x: -abs(x["gap"][0]))
    return out


def _side_label(m):
    return {"yes_t": "YES as taker", "no_t": "NO as taker", "yes_m": "YES as resting maker",
            "no_m": "NO as resting maker"}[m]


def build_report(conn):
    groups, vols, quarters, sources = build(conn)
    cats = sorted({c for (_, c, _) in groups},
                  key=lambda c: -sum(a.n_events() for (s, cc, _), a in groups.items() if cc == c))
    n_trades = {s: sum(a.trades for (sp, _, _), a in groups.items() if sp == s) for s in ("discovery", "holdout")}
    n_events = {s: len({e for (sp, _, _), a in groups.items() if sp == s for e in a.ev}) for s in ("discovery", "holdout")}
    L = ["KALSHI CALIBRATION STUDY: DO CONTRACTS PRICED AT X CENTS WIN X% OF THE TIME?", "=" * 72, "",
         "DATA",
         "A random sample of Kalshi trades from January 2025 to now (150 random time windows per month, about "
         "1,000 trades each), plus extra sampled events for categories that came out thin. Only markets that "
         "settled YES or NO are used.",
         f"Holdout (fixed before looking at results): patterns were looked for only in trades before {HOLDOUT_LABEL} "
         f"(the 'discovery' data) and then checked once on trades from {HOLDOUT_LABEL} on (the 'holdout').",
         _table(["", "Trades", "Events"],
                [["Discovery (before cutoff)", f"{n_trades['discovery']:,}", f"{n_events['discovery']:,}"],
                 ["Holdout (from cutoff)", f"{n_trades['holdout']:,}", f"{n_events['holdout']:,}"]]),
         f"Trades from the random sample: {sources[('discovery', 'random')] + sources[('holdout', 'random')]:,}; "
         f"from the thin-category top-up: {sources[('discovery', 'topup')] + sources[('holdout', 'topup')]:,}.", ""]

    patterns = find_patterns(groups, vols)
    # Holdout check for each pattern.
    for pt in patterns:
        h = groups.get(("holdout", pt["cat"], pt["bucket"]))
        pt["h"] = h
        if h is None or h.n_events() < MIN_EVENTS:
            pt["verdict"] = "not enough holdout data"
            continue
        hg, hlo, hhi = h.ratio("gap")
        hret = h.ratio(pt["best"])[0]
        same_dir = (hg > 0) == (pt["gap"][0] > 0)
        if same_dir and (hlo > 0 or hhi < 0) and hret is not None and hret > 0:
            pt["verdict"] = "CONFIRMED"
        elif same_dir and hret is not None and hret > 0:
            pt["verdict"] = "same direction, not significant"
        else:
            pt["verdict"] = "did not hold up"

    L += ["SUMMARY"]
    confirmed = [p for p in patterns if p["verdict"] == "CONFIRMED"]
    if not patterns:
        L.append("- In the discovery data, no category and price bucket was mispriced by more than chance AND "
                 "profitable after fees on either side.")
    else:
        L.append(f"- {len(patterns)} category/price buckets looked mispriced in the discovery data and were profitable "
                 f"after fees on at least one side. {len(confirmed)} of them held up on the holdout.")
        for p in confirmed:
            r = p["h"].ratio(p["best"])[0]
            L.append(f"  * {p['cat']}, {LABELS[p['bucket']]}: {_side_label(p['best'])} returned {_pct(r)} per $1 "
                     f"after fees on the holdout ({p['h'].n_events()} events){'; ' + p['thin'] if p['thin'] else ''}.")
        taker_ok = [p for p in confirmed if max(p["h"].ratio("yes_t")[0] or -1, p["h"].ratio("no_t")[0] or -1) > 0]
        maker_only = [p for p in confirmed if p not in taker_ok]
        if confirmed:
            L.append(f"- Capturable as a taker (paying the spread and taker fee): {len(taker_ok)}; "
                     f"only as a resting maker order: {len(maker_only)}.")
    L += ["- Sample sizes are counted in events as well as contracts: strikes of the same event win or lose "
          "together, so the event count is the honest measure of how much evidence there is.", ""]

    L += ["HOW TO READ THE TABLES",
          "- 'Avg price paid' is the average YES price of trades in the bucket; 'YES won' is how often YES "
          "actually won (both weighted by contracts). 'Won minus price' is the gap per contract: positive means "
          "YES was cheap, negative means YES was expensive. The range is a rough 95% range counting each event "
          "once.",
          "- 'YES taker' / 'NO taker' are returns per $1 for whoever bought that side by crossing the spread "
          "(paying the ask and the taker fee). 'YES maker' / 'NO maker' are returns for whoever bought that side "
          "with a resting order that got filled (paying the maker fee, which most series don't charge). Fees use "
          "the schedule in effect at the time and are rounded up to the cent per trade. A positive taker number "
          "means you could have captured it by simply buying; a positive maker number only means patient resting "
          "orders were rewarded.",
          f"- 'Tradeable?' flags fewer than {MIN_EVENTS} events, markets whose median lifetime volume is under "
          f"{THIN_MARKET_VOLUME:,} contracts, or one event making up over a quarter of the contracts.",
          "- The sample takes the same number of windows from every month, so 2025 is over-represented relative "
          "to its (much lower) real volume. That is deliberate: it gives older periods enough data to compare.",
          ""]

    L += ["1. CALIBRATION BY CATEGORY (discovery data, before the cutoff)", ""]
    for cat in cats:
        accs = {b: a for (s, c, b), a in groups.items() if s == "discovery" and c == cat}
        if not accs:
            continue
        cv = {b: vols[("discovery", cat, b)] for b in accs}
        n_ev = len({e for a in accs.values() for e in a.ev})
        L += [f"{cat} ({n_ev:,} events)", _table(BUCKET_HEADERS, _bucket_rows(accs, cv)), ""]

    L += ["2. PATTERNS FOUND ON DISCOVERY DATA, CHECKED ON THE HOLDOUT", ""]
    if not patterns:
        L += ["None met the bar (enough events, gap clearly different from zero, and profitable after fees).", ""]
    else:
        rows = []
        for p in patterns:
            h = p["h"]
            rows.append([p["cat"], LABELS[p["bucket"]], p["events"], _rng(*p["gap"], _c), _side_label(p["best"]),
                         _pct(p["returns"][p["best"]]), h.n_events() if h else 0,
                         _rng(*h.ratio("gap"), _c) if h else "n/a", _pct(h.ratio(p["best"])[0]) if h else "n/a",
                         p["verdict"], p["thin"] or "ok"])
        L += [_table(["Category", "Price", "Events (disc.)", "Gap (disc.)", "Best side", "Return/$1 (disc.)",
                      "Events (holdout)", "Gap (holdout)", "Return/$1 (holdout)", "Verdict", "Tradeable?"], rows), ""]

    L += ["3. TAKERS VS MAKERS: CAN THE PATTERN BE CAPTURED BY CROSSING THE SPREAD?", "",
          "For each pattern, the same trades seen from both sides: the taker (crossed the spread, paid the taker "
          "fee) and the maker (was resting, paid the maker fee if the series charges one; most series don't). "
          "If only the maker side made money, you'd have to post resting orders and wait to be filled, and "
          "you'd only get filled when someone chose to trade with you.", ""]
    if patterns:
        rows = []
        for p in patterns:
            for label, a in (("discovery", groups[("discovery", p["cat"], p["bucket"])]), ("holdout", p["h"])):
                if a is None:
                    continue
                rows.append([p["cat"], LABELS[p["bucket"]], label, a.n_events()] +
                            [_rng(*a.ratio(w), _pct) for w in WAYS])
        L += [_table(["Category", "Price", "Data", "Events", "YES as taker", "NO as taker", "YES as maker",
                      "NO as maker"], rows), "",
              "Each cell is return per $1 after the fee that side actually pays (95% range). A taker who bought "
              "YES paid the ask; a resting YES buyer was filled at their bid when someone sold into it.", ""]
    else:
        L += ["No patterns to break down.", ""]
    # Overall taker vs maker by category, for context.
    rows = []
    for cat in cats:
        for split in ("discovery", "holdout"):
            a = Acc()
            for (s, c, b), g in groups.items():
                if s == split and c == cat:
                    for ev, sums in g.ev.items():
                        for k, v in sums.items():
                            a.ev[ev][k] += v
                    a.trades += g.trades
            if a.trades:
                rows.append([cat, split, a.n_events(), _rng(*a.ratio("taker"), _pct), _rng(*a.ratio("maker"), _pct)])
    L += ["All trades by category (takers vs makers overall):",
          _table(["Category", "Data", "Events", "Taker return/$1", "Maker return/$1"], rows), ""]

    L += ["4. EDGE DECAY: IS EACH PATTERN SHRINKING AS VOLUME GREW?", ""]
    if not patterns:
        L += ["No patterns to track.", ""]
    for p in patterns:
        qs = sorted(q for (c, b, q) in quarters if c == p["cat"] and b == p["bucket"])
        rows = []
        for q in qs:
            a = quarters[(p["cat"], p["bucket"], q)]
            rows.append([q, a.trades, a.n_events(), _rng(*a.ratio("gap"), _c), _pct(a.ratio(p["best"])[0]),
                         "holdout" if q >= _quarter(HOLDOUT_START) else ""])
        L += [f"{p['cat']}, {LABELS[p['bucket']]} ({_side_label(p['best'])}):",
              _table(["Quarter", "Trades", "Events", "Won minus price", f"Return/$1 ({p['best']})", ""], rows), ""]

    L += ["5. HOLDOUT CALIBRATION BY CATEGORY (from the cutoff on)", ""]
    for cat in cats:
        accs = {b: a for (s, c, b), a in groups.items() if s == "holdout" and c == cat}
        if not accs:
            continue
        cv = {b: vols[("holdout", cat, b)] for b in accs}
        n_ev = len({e for a in accs.values() for e in a.ev})
        L += [f"{cat} ({n_ev:,} events)", _table(BUCKET_HEADERS, _bucket_rows(accs, cv)), ""]

    L += ["CAVEATS",
          "- Prices are those of actual trades; you might not get the same price, and size at that price varied.",
          "- Fees: the taker fee is 0.07 x price x (1 - price) per contract times the series multiplier, rounded "
          "up to the cent per trade. Maker fees apply only to series with maker fees (0.25x the taker rate; 0.5x "
          "for combo makers). Kalshi's fee-change history starts in October 2025; before a series' first "
          "recorded change we assume the standard schedule (taker fee, no maker fee).",
          "- Only markets that had settled when the data was pulled are included, so long-dated markets are "
          "under-represented in recent months.",
          "- Many buckets are compared, so a few will look unusual by chance; that is why patterns must hold "
          "up on the holdout.",
          ""]
    return "\n".join(L)


def write_report(conn):
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORT_DIR / "calibration_study.txt"
    path.write_text(build_report(conn) + "\n", encoding="utf-8")
    return path
