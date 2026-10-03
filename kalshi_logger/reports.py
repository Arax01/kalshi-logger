"""Plain-English reports, written as text files into the reports/ folder.

* Daily crypto summary: one per calendar day (your computer's local time), written after 01:30
  the next day so overnight markets have settled.
* Weekly scanner report: one per Monday-Sunday week, written after 01:30 on the Monday.

The reports job runs at start-up and every 15 minutes. Any report that is due but missing
(e.g. because the laptop was asleep at the scheduled time) is generated then.
"""
import json
import logging
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, db, results

log = logging.getLogger(__name__)

READY_AFTER = timedelta(hours=1, minutes=30)
MAX_CATCH_UP_DAYS = 21


# ---------- helpers ----------

def _local_midnight(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%a %b %d %H:%M")


def _c(x, digits=1):
    """Dollars -> cents string."""
    return "n/a" if x is None else f"{x * 100:.{digits}f}c"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _dur(sec):
    if sec is None:
        return "n/a"
    if sec < 90:
        return "under 2 min" if sec < 1 else f"{sec / 60:.0f} min"
    if sec < 3600:
        return f"{sec / 60:.0f} min"
    return f"{sec / 3600:.1f} h"


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _table(headers, rows):
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(out) if rows else "(nothing to show)"


def _gaps_text(conn, jobs, start, end):
    rows = conn.execute(
        f"SELECT job, start_ts, end_ts, reason FROM gaps WHERE job IN ({','.join('?' * len(jobs))}) "
        "AND end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (*jobs, start, end),
    ).fetchall()
    if not rows:
        return "No data gaps recorded in this period.", 0
    total = sum(min(r["end_ts"], end) - max(r["start_ts"], start) for r in rows)
    lines = [f"- {r['job']}: {_fmt_ts(max(r['start_ts'], start))} to {_fmt_ts(min(r['end_ts'], end))} "
             f"({_dur(min(r['end_ts'], end) - max(r['start_ts'], start))}) - {r['reason']}" for r in rows]
    return "\n".join(lines), total


def _brier(pairs):
    pairs = [(p, o) for p, o in pairs if p is not None]
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


# ---------- daily crypto summary ----------

def _series_kind(ticker):
    s = ticker.split("-")[0]
    if s.endswith("15M"):
        return "15-minute up/down"
    if s in ("KXBTC", "KXETH"):
        return "hourly/daily range"
    if s in ("KXBTCD", "KXETHD"):
        return "above/below"
    if s in ("KXBTCY", "KXETHY"):
        return "year-end range"
    return s


def _episodes(rows, side_col, threshold, max_step):
    """Group consecutive snapshots of one market where the edge stayed above threshold."""
    eps = []
    by_market = defaultdict(list)
    for r in rows:
        by_market[r["market_id"]].append(r)
    for snaps in by_market.values():
        cur = None
        last_ts = None
        for r in snaps:
            above = r[side_col] is not None and r[side_col] > threshold
            continuous = last_ts is not None and r["ts"] - last_ts <= max_step
            if above and cur is not None and continuous:
                cur["snaps"].append(r)
            elif above:
                cur = {"snaps": [r]}
                eps.append(cur)
            else:
                cur = None
            last_ts = r["ts"]
    return eps


def crypto_daily(conn, day_start, day_end, label):
    lines = [f"DAILY CRYPTO SUMMARY - {label}", "=" * 60, ""]
    thr = config.CRYPTO_GAP_THRESHOLD
    rows = conn.execute(
        "SELECT f.*, m.ticker, m.result FROM crypto_fv f JOIN markets m USING(market_id) "
        "WHERE f.ts >= ? AND f.ts < ? ORDER BY f.market_id, f.ts",
        (day_start, day_end),
    ).fetchall()
    gaps_text, gap_sec = _gaps_text(conn, ["crypto"], day_start, day_end)
    n_markets = len({r["market_id"] for r in rows})
    lines += [
        "WHAT THIS COVERS",
        f"Fair-value checks logged: {len(rows):,} across {n_markets} BTC/ETH markets.",
        f"Time without data: {_dur(gap_sec) if gap_sec else 'none'}.",
        gaps_text, "",
    ]
    if not rows:
        lines.append("No crypto data was collected on this day.")
        return "\n".join(lines)

    max_step = config.GAP_FACTOR * config.CRYPTO_INTERVAL_SEC
    sections = []
    all_eps = []
    for side_col, side_name, price_col, size_col in (
        ("edge_buy_yes", "buy YES (Kalshi price looked too low)", "yes_ask", "ask_size"),
        ("edge_buy_no", "buy NO (Kalshi price looked too high)", "yes_bid", "bid_size"),
    ):
        eps = _episodes(rows, side_col, thr, max_step)
        for e in eps:
            first, last = e["snaps"][0], e["snaps"][-1]
            e.update(side=side_col, side_name=side_name, first=first, ticker=first["ticker"],
                     duration=last["ts"] - first["ts"], kind=_series_kind(first["ticker"]),
                     method=first["vol_method"], size=_median([s[size_col] for s in e["snaps"]]),
                     edge=max(s[side_col] for s in e["snaps"]), price=first[price_col])
            # Result if you had taken the gap at its first moment, per contract, after fees.
            if first["result"] in ("yes", "no"):
                won = 1.0 if first["result"] == "yes" else 0.0
                if side_col == "edge_buy_yes":
                    e["pnl"] = won - first["yes_ask"] - first["fee_buy_yes"]
                else:
                    e["pnl"] = (1 - won) - (1 - first["yes_bid"]) - first["fee_buy_no"]
            else:
                e["pnl"] = None
        all_eps += eps

    lines += [f"GAPS OVER {thr * 100:.0f} CENTS AFTER FEES", ""]
    if not all_eps:
        lines += [f"No moments where Kalshi's best price was more than {thr * 100:.0f}c away from our fair value "
                  "after paying the taker fee.", ""]
    else:
        durs = [e["duration"] for e in all_eps]
        sizes = [e["size"] for e in all_eps if e["size"] is not None] or [0]
        lines += [
            f"{len(all_eps)} gaps appeared (a gap = a market where taking the best price would have been "
            f"worth more than {thr * 100:.0f}c per contract after fees, by our model).",
            f"How long they lasted: half were gone within {_dur(_median(durs))}; the longest lasted "
            f"{_dur(max(durs))}. (Checks run every {config.CRYPTO_INTERVAL_SEC // 60} min, so a gap "
            "seen once lasted somewhere under that.)",
            f"Size available at the gap price: typically {(_median(sizes) or 0):,.0f} contracts "
            f"(range {min(sizes):,.0f} to {max(sizes):,.0f}).", "",
        ]
        by = defaultdict(list)
        for e in all_eps:
            by[(e["kind"], e["side_name"].split(" (")[0], e["method"])].append(e)
        trows = []
        for (kind, side, method), es in sorted(by.items(), key=lambda kv: -len(kv[1])):
            pnls = [e["pnl"] for e in es if e["pnl"] is not None]
            trows.append([kind, side, method, len(es), _dur(_median([e["duration"] for e in es])),
                          f"{_median([e['size'] for e in es]):,.0f}", _c(_median([e["edge"] for e in es])),
                          f"{_c(sum(pnls) / len(pnls))} (n={len(pnls)})" if pnls else "pending"])
        lines += [_table(["Market type", "Side", "Vol method", "Gaps", "Typical length", "Typical size",
                          "Typical edge", "Avg result if taken"], trows), ""]
        lines += [
            "How to read 'Avg result if taken': profit per contract had you taken the gap at the moment it "
            "first appeared, after fees, using the actual outcome. Positive means the gap was real money; "
            "near zero or negative means our fair value was wrong, not the market.",
            "Vol method 'before_first_expiry' means the market closes before the next Deribit option expiry, "
            "so the volatility comes from a longer-dated option. Treat those fair values with more caution.",
            "",
        ]
        top = sorted(all_eps, key=lambda e: -e["edge"])[:10]
        lines += ["Largest gaps of the day:", _table(
            ["Market", "Side", "First seen", "Edge", "Size", "Lasted", "Outcome"],
            [[e["ticker"], e["side_name"].split(" (")[0], _fmt_ts(e["first"]["ts"]), _c(e["edge"]),
              f"{e['size']:,.0f}", _dur(e["duration"]), e["first"]["result"] or "pending"] for e in top]), ""]

    # Fair value vs outcome, for markets that closed today.
    closed = conn.execute(
        "SELECT f.*, m.ticker, m.result FROM crypto_fv f JOIN markets m USING(market_id) "
        "WHERE m.close_ts >= ? AND m.close_ts < ? AND m.result IN ('yes','no')",
        (day_start, day_end),
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(DISTINCT f.market_id) FROM crypto_fv f JOIN markets m USING(market_id) "
        "WHERE m.close_ts >= ? AND m.close_ts < ? AND m.result IS NULL", (day_start, day_end)).fetchone()[0]
    lines += ["FAIR VALUES VS WHAT ACTUALLY HAPPENED", ""]
    if not closed:
        lines += ["No logged crypto markets with a final result closed on this day.", ""]
    else:
        def mid(r):
            return (r["yes_bid"] + r["yes_ask"]) / 2 if r["yes_bid"] is not None and r["yes_ask"] is not None else None
        o = lambda r: 1.0 if r["result"] == "yes" else 0.0
        two_sided = [r for r in closed if mid(r) is not None]
        b_model = _brier([(r["fair_yes"], o(r)) for r in two_sided])
        b_rv = _brier([(r["fair_yes_realised"], o(r)) for r in two_sided])
        b_mkt = _brier([(mid(r), o(r)) for r in two_sided])
        n_mk = len({r["market_id"] for r in closed})
        lines += [
            f"{n_mk} markets that closed today have final results ({len(closed):,} fair-value checks). "
            f"{pending} more are still waiting for Kalshi to finalise.",
            "Accuracy score (Brier score: average squared miss, lower is better, 0 = perfect, "
            "0.25 = coin-flip guessing), on checks where Kalshi had both a bid and an ask:",
            f"  Our fair value (options-implied vol):  {b_model:.4f}" if b_model is not None else "",
            f"  Audit fair value (recent realised vol): {b_rv:.4f}" if b_rv is not None else "",
            f"  Kalshi market price (mid):              {b_mkt:.4f}" if b_mkt is not None else "",
            "If the market scores lower than our fair value, the market was the better forecaster today.",
            "",
            "Calibration (when a forecast said X%, how often did YES happen?):",
        ]
        buckets = defaultdict(list)
        for r in two_sided:
            buckets[min(int(r["fair_yes"] * 10), 9)].append(r)
        lines.append(_table(
            ["Our fair value", "Checks", "Our avg", "Market avg", "Actually YES"],
            [[f"{b * 10}-{b * 10 + 10}%", len(rs), _pct(statistics.mean(r["fair_yes"] for r in rs)),
              _pct(statistics.mean(mid(r) for r in rs)), _pct(statistics.mean(o(r) for r in rs))]
             for b, rs in sorted(buckets.items())]))
        lines.append("")
    return "\n".join(lines)


# ---------- weekly scanner report ----------

def _market_stats(conn, start, end):
    rows = conn.execute(
        "SELECT s.market_id, s.bid_cc, s.ask_cc, s.d_mid_cc, s.d_volume FROM scan_snapshots s "
        "WHERE s.ts >= ? AND s.ts < ?", (start, end)).fetchall()
    stats = defaultdict(lambda: {"spreads": [], "volume": 0.0, "move": 0.0, "rows": 0})
    for r in rows:
        st = stats[r["market_id"]]
        st["rows"] += 1
        if r["bid_cc"] is not None and r["ask_cc"] is not None:
            st["spreads"].append((r["ask_cc"] - r["bid_cc"]) / 10000)
        if r["d_volume"] is not None and r["d_volume"] > 0:
            st["volume"] += r["d_volume"]
        if r["d_mid_cc"] is not None:
            st["move"] += abs(r["d_mid_cc"]) / 10000
    meta = {}
    ids = list(stats)
    for i in range(0, len(ids), 900):
        chunk = ids[i: i + 900]
        for m in conn.execute(
            f"SELECT market_id, ticker, series_ticker, category, market_group, sport, league, stat_type "
            f"FROM markets WHERE market_id IN ({','.join('?' * len(chunk))})", chunk):
            meta[m["market_id"]] = dict(m)
    return stats, meta


def _group_rows(stats, meta, keyfn, min_markets=1):
    groups = defaultdict(lambda: {"markets": 0, "active": 0, "spreads": [], "volume": 0.0, "move": 0.0})
    for mid, st in stats.items():
        m = meta.get(mid)
        if not m:
            continue
        key = keyfn(m)
        if key is None:
            continue
        g = groups[key]
        g["markets"] += 1
        g["volume"] += st["volume"]
        g["move"] += st["move"]
        if st["volume"] >= config.MEANINGFUL_WEEKLY_VOLUME and st["spreads"]:
            g["active"] += 1
            g["spreads"].append(statistics.median(st["spreads"]))
    out = []
    for key, g in groups.items():
        if g["markets"] < min_markets:
            continue
        out.append({
            "key": key, "markets": g["markets"], "active": g["active"],
            "spread": _median(g["spreads"]), "volume": g["volume"],
            "move_per_1k": (g["move"] / g["volume"] * 1000) if g["volume"] > 0 else None,
            "move": g["move"],
        })
    return out


def _spread_table(rows, label, n=20):
    rows = [r for r in rows if r["active"] >= 3 and r["spread"] is not None]
    rows.sort(key=lambda r: -r["spread"])
    return _table([label, "Active markets", "Median spread", "Contracts traded"],
                  [[r["key"], r["active"], _c(r["spread"]), f"{r['volume']:,.0f}"] for r in rows[:n]])


def _move_table(rows, label, n=20, min_volume=None):
    min_volume = min_volume or config.MEANINGFUL_WEEKLY_VOLUME * 10
    rows = [r for r in rows if r["move_per_1k"] is not None and r["volume"] >= min_volume]
    rows.sort(key=lambda r: -r["move_per_1k"])
    return _table([label, "Contracts traded", "Total price movement", "Movement per 1,000 contracts"],
                  [[r["key"], f"{r['volume']:,.0f}", _c(r["move"], 0), _c(r["move_per_1k"])] for r in rows[:n]])


def _prop_key(m):
    if m["market_group"] != "player_prop":
        return None
    return f"{m['league'] or m['sport']} - {m['stat_type']}"


def _combo_section(conn, start, end):
    lines = ["COMBOS (multi-leg parlays)", ""]
    s = conn.execute(
        "SELECT COUNT(*) n, SUM(combo_trade_count) trades, SUM(combo_contracts) contracts, "
        "SUM(combo_feed_truncated) trunc FROM scans WHERE started_ts >= ? AND started_ts < ? AND status='ok'",
        (start, end)).fetchone()
    if not s["n"] or not s["trades"]:
        return lines + ["No combo trading data this week.", ""]
    lines += [
        f"Across {s['n']} scans: {s['trades']:,} combo trades, {s['contracts']:,.0f} contracts.",
        "Combos have no standing order book on Kalshi (they are priced on request), so 'spread' does not "
        "apply. Instead we compare each traded combo's price with what its legs imply.",
    ]
    if s["trunc"]:
        lines.append(f"Note: on {s['trunc']} scans the trade feed was too busy to read in full; "
                     "those scans cover only the most recent trades.")
    lines.append("")
    rows = conn.execute(
        "SELECT ct.*, m.combo_detail, m.combo_legs, m.league, m.result FROM combo_trades ct "
        "JOIN markets m USING(market_id) WHERE ct.window_end_ts >= ? AND ct.window_end_ts < ? "
        "AND m.combo_detail IS NOT NULL", (start, end)).fetchall()
    ticker_ids = {}
    groups = defaultdict(lambda: {"n": 0, "contracts": 0.0, "prem": [], "results": []})
    for r in rows:
        legs = json.loads(r["combo_detail"])
        probs = []
        for leg in legs:
            t = leg["market"]
            if t not in ticker_ids:
                row = conn.execute("SELECT market_id FROM markets WHERE ticker=?", (t,)).fetchone()
                ticker_ids[t] = row[0] if row else None
            mid_id = ticker_ids[t]
            snap = mid_id and conn.execute(
                "SELECT bid_cc, ask_cc FROM scan_snapshots WHERE market_id=? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1", (mid_id, r["window_end_ts"])).fetchone()
            if not snap or snap["bid_cc"] is None or snap["ask_cc"] is None:
                probs = None
                break
            p = (snap["bid_cc"] + snap["ask_cc"]) / 20000
            probs.append(p if leg.get("side") == "yes" else 1 - p)
        games = {leg["market"].split("-")[1] if "-" in leg["market"] else leg["market"] for leg in legs}
        kind = "same-game" if len(games) < len(legs) else "multi-game"
        nlegs = r["combo_legs"] or len(legs)
        bucket = "2 legs" if nlegs <= 2 else "3-4 legs" if nlegs <= 4 else "5-8 legs" if nlegs <= 8 else "9+ legs"
        g = groups[(kind, bucket)]
        g["n"] += r["n_trades"]
        g["contracts"] += r["contracts"]
        if probs and r["vwap_yes"] is not None:
            implied = 1.0
            for p in probs:
                implied *= p
            if implied >= 0.001:
                g["prem"].append(r["vwap_yes"] / implied)
        if r["result"] in ("yes", "no") and r["vwap_yes"] is not None:
            g["results"].append((r["vwap_yes"], 1.0 if r["result"] == "yes" else 0.0))
    trows = []
    for (kind, bucket), g in sorted(groups.items()):
        res = g["results"]
        trows.append([kind, bucket, g["n"], f"{g['contracts']:,.0f}",
                      f"{_median(g['prem']):.2f}x (n={len(g['prem'])})" if g["prem"] else "n/a",
                      f"{statistics.mean(p for p, _ in res) * 100:.1f}% vs {statistics.mean(o for _, o in res) * 100:.1f}% "
                      f"(n={len(res)})" if res else "pending"])
    lines += [_table(["Type", "Legs", "Trades", "Contracts", "Price vs legs' product",
                      "Avg price paid vs how often it won"], trows), "",
              "'Price vs legs' product': the combo's traded price divided by the product of each leg's Kalshi "
              "mid price (median across combos). 1.00x means the combo was priced exactly as its legs imply; "
              "1.30x means buyers paid 30% more. Same-game legs are related, so some difference is expected "
              "there; multi-game combos should be near 1.00x if fairly priced. Covers the most-traded combos "
              "sampled each scan.",
              "'Avg price paid vs how often it won': if buyers pay 5.0% on average but combos win 3.0% of the "
              "time, sellers have the edge.", ""]
    return lines


def scanner_weekly(conn, start, end, label):
    lines = [f"WEEKLY SCANNER REPORT - {label}", "=" * 60, ""]
    scans = conn.execute(
        "SELECT COUNT(*) n, SUM(status='ok') ok FROM scans WHERE started_ts >= ? AND started_ts < ?",
        (start, end)).fetchone()
    gaps_text, gap_sec = _gaps_text(conn, ["scanner"], start, end)
    lines += ["WHAT THIS COVERS",
              f"{scans['n'] or 0} scans ({scans['ok'] or 0} complete). Time without data: "
              f"{_dur(gap_sec) if gap_sec else 'none'}.", gaps_text, "",
              f"'Active' market = traded at least {config.MEANINGFUL_WEEKLY_VOLUME:,} contracts this week. "
              "Spread = best ask minus best bid for YES (the cost of crossing the market, in cents). "
              "Price movement = total up-and-down movement of the mid price.", ""]
    stats, meta = _market_stats(conn, start, end)
    if not stats:
        return "\n".join(lines + ["No scanner data was collected this week."])

    def cat_key(m):
        if m["market_group"] == "combo":
            return None
        return m["category"] or "Unknown"

    def series_key(m):
        return None if m["market_group"] == "combo" else m["series_ticker"]

    def sports_key(m):
        if m["category"] != "Sports" or m["market_group"] in ("player_prop", "combo"):
            return None
        return f"{m['league'] or m['sport']} - {m['market_group'].replace('_', ' ')}"

    by_cat = _group_rows(stats, meta, cat_key)
    by_series = _group_rows(stats, meta, series_key)
    by_sport = _group_rows(stats, meta, sports_key)
    by_prop = _group_rows(stats, meta, _prop_key)
    by_prop_sport = _group_rows(stats, meta, lambda m: (m["league"] or m["sport"]) if _prop_key(m) else None)

    def top_line(rows, what):
        rows = [r for r in rows if r["active"] >= 3 and r["spread"] is not None]
        if not rows:
            return None
        r = max(rows, key=lambda r: r["spread"])
        return f"Widest spreads among active {what}: {r['key']} (median {_c(r['spread'])} across {r['active']} markets)."

    def top_move(rows, what):
        rows = [r for r in rows if r["move_per_1k"] is not None and r["volume"] >= config.MEANINGFUL_WEEKLY_VOLUME * 10]
        if not rows:
            return None
        r = max(rows, key=lambda r: r["move_per_1k"])
        return (f"Most price movement for its volume among {what}: {r['key']} "
                f"({_c(r['move_per_1k'])} of movement per 1,000 contracts traded).")

    summary = [x for x in (top_line(by_cat, "categories"), top_line(by_series, "series"),
                           top_line(by_prop, "player prop types"), top_move(by_series, "series"),
                           top_move(by_prop, "player prop types")) if x]
    lines += ["SUMMARY"] + [f"- {s}" for s in summary] + [""]
    lines += ["1. WIDEST SPREADS WITH MEANINGFUL VOLUME", "",
              "By category:", _spread_table(by_cat, "Category"), "",
              "By series (top 20):", _spread_table(by_series, "Series"), "",
              "Sports (excluding player props), by league and market type:", _spread_table(by_sport, "League - type"), "",
              "Player props by sport/league and stat type:", _spread_table(by_prop, "League - stat type", n=40), "",
              "Player props by league overall:", _spread_table(by_prop_sport, "League"), ""]
    lines += ["2. MOST PRICE MOVEMENT RELATIVE TO VOLUME", "",
              "Lots of price movement with little trading can mean prices are being set by few participants "
              "(possible overreaction or stale quotes). Only groups with at least "
              f"{config.MEANINGFUL_WEEKLY_VOLUME * 10:,} contracts traded are shown.", "",
              "By category:", _move_table(by_cat, "Category"), "",
              "By series (top 20):", _move_table(by_series, "Series"), "",
              "Sports (excluding player props):", _move_table(by_sport, "League - type"), "",
              "Player props by sport/league and stat type:", _move_table(by_prop, "League - stat type", n=40), ""]
    lines += ["3. " + "\n".join(_combo_section(conn, start, end))]
    return "\n".join(lines)


# ---------- scheduling ----------

def _write(conn, report_type, key, text, filename):
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORT_DIR / filename
    path.write_text(text + "\n", encoding="utf-8")
    conn.execute(
        "INSERT OR REPLACE INTO reports_done(report_type, period_key, generated_ts, path) VALUES(?,?,?,?)",
        (report_type, key, int(time.time()), str(path)))
    conn.commit()
    log.info("Wrote report %s", path)
    return path


def _first_data_ts(conn):
    row = conn.execute(
        "SELECT MIN(t) FROM (SELECT MIN(started_ts) t FROM scans UNION ALL SELECT MIN(ts) FROM crypto_fv)"
    ).fetchone()
    return row[0]


def run_due(after_gap=False):
    """Generate every report that is due and missing. Returns the paths written."""
    conn = db.connect()
    first = _first_data_ts(conn)
    if first is None:
        return []
    now = datetime.now().astimezone()
    written = []
    first_day = _local_midnight(datetime.fromtimestamp(first).astimezone())
    earliest = max(first_day, _local_midnight(now) - timedelta(days=MAX_CATCH_UP_DAYS))
    results_checked = False

    day = earliest
    while day + timedelta(days=1) + READY_AFTER <= now:
        key = day.strftime("%Y-%m-%d")
        if not conn.execute(
                "SELECT 1 FROM reports_done WHERE report_type='crypto_daily' AND period_key=?", (key,)).fetchone():
            if not results_checked:
                results.run()
                results_checked = True
            nxt = day + timedelta(days=1)
            text = crypto_daily(conn, int(day.timestamp()), int(nxt.timestamp()), day.strftime("%A %B %d, %Y"))
            written.append(_write(conn, "crypto_daily", key, text, f"crypto_daily_{key}.txt"))
        day += timedelta(days=1)

    week = earliest - timedelta(days=earliest.weekday())
    while week + timedelta(days=7) + READY_AFTER <= now:
        key = week.strftime("%Y-%m-%d")
        if not conn.execute(
                "SELECT 1 FROM reports_done WHERE report_type='scanner_weekly' AND period_key=?", (key,)).fetchone():
            if not results_checked:
                results.run()
                results_checked = True
            end = week + timedelta(days=7)
            label = f"week of {week.strftime('%b %d')} to {(end - timedelta(days=1)).strftime('%b %d, %Y')}"
            text = scanner_weekly(conn, int(week.timestamp()), int(end.timestamp()), label)
            written.append(_write(conn, "scanner_weekly", key, text, f"scanner_weekly_{key}.txt"))
        week += timedelta(days=7)
    return written


def preview(conn, hours=24):
    """Reports for the most recent period, even if incomplete (for report.bat --now)."""
    end = int(time.time())
    paths = []
    text = crypto_daily(conn, end - hours * 3600, end, f"last {hours} hours (preview, not final)")
    paths.append(_write(conn, "crypto_preview", str(end), text, "crypto_preview_latest.txt"))
    text = scanner_weekly(conn, end - 7 * 24 * 3600, end, "last 7 days (preview, not final)")
    paths.append(_write(conn, "scanner_preview", str(end), text, "scanner_preview_latest.txt"))
    return paths
