"""Plain-English reports, written as text files into the reports/ folder.

* Daily crypto summary: one per calendar day (your computer's local time), written after 01:30
  the next day so overnight markets have settled.
* Weekly scanner report: one per Monday-Sunday week, written after 01:30 on the Monday.

The reports job runs at start-up and every 15 minutes. Any report that is due but missing
(e.g. because the laptop was asleep at the scheduled time) is generated then.
"""
import json
import logging
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, db, fees, paper, results, volsurface

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


def _gap_episodes(rows):
    """Gap episodes (both sides) with duration, size, edge and the result if taken at first sight."""
    thr = config.CRYPTO_GAP_THRESHOLD
    max_step = config.GAP_FACTOR * config.CRYPTO_INTERVAL_SEC
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
    return all_eps


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

    all_eps = _gap_episodes(rows)

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


def _combo_type(legs):
    """cross-game: every leg from a different game; same-game: all legs from one game; mixed: in between.

    The game is identified by the event part of each leg's ticker (e.g. 26OCT03SYRCONN), which Kalshi
    shares across a game's winner, spread, total and player-prop markets.
    """
    games = [leg["market"].split("-")[1] if leg["market"].count("-") >= 1 else leg["market"] for leg in legs]
    distinct = len(set(games))
    if distinct == len(games):
        return "cross-game"
    if distinct == 1:
        return "same-game"
    return "mixed"


def _legs_bucket(n):
    return "2 legs" if n <= 2 else "3-4 legs" if n <= 4 else "5-8 legs" if n <= 8 else "9+ legs"


def _combo_rows(conn, start, end):
    """Each sampled combo trade window with its legs' implied price (product of leg mids)."""
    rows = conn.execute(
        "SELECT ct.*, m.combo_detail, m.combo_legs, m.result FROM combo_trades ct "
        "JOIN markets m USING(market_id) WHERE ct.window_end_ts >= ? AND ct.window_end_ts < ? "
        "AND m.combo_detail IS NOT NULL", (start, end)).fetchall()
    ids = {}
    out = []
    for r in rows:
        legs = json.loads(r["combo_detail"])
        implied = 1.0
        for leg in legs:
            t = leg["market"]
            if t not in ids:
                row = conn.execute("SELECT market_id FROM markets WHERE ticker=?", (t,)).fetchone()
                ids[t] = row[0] if row else None
            snap = ids[t] and conn.execute(
                "SELECT bid_cc, ask_cc FROM scan_snapshots WHERE market_id=? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1", (ids[t], r["window_end_ts"])).fetchone()
            if not snap or snap["bid_cc"] is None or snap["ask_cc"] is None:
                implied = None
                break
            p = (snap["bid_cc"] + snap["ask_cc"]) / 20000
            implied *= p if leg.get("side") == "yes" else 1 - p
        out.append({
            "type": _combo_type(legs), "legs": _legs_bucket(r["combo_legs"] or len(legs)),
            "trades": r["n_trades"], "contracts": r["contracts"] or 0.0, "price": r["vwap_yes"],
            "implied": implied if implied is not None and implied >= 0.001 else None,
            "won": None if r["result"] not in ("yes", "no") else (1.0 if r["result"] == "yes" else 0.0),
        })
    return out


def _combo_stats(items):
    priced = [x for x in items if x["implied"] and x["price"] is not None]
    settled = [x for x in items if x["won"] is not None and x["price"] is not None]
    c_priced = sum(x["contracts"] for x in priced)
    c_settled = sum(x["contracts"] for x in settled)
    paid = sum(x["contracts"] * x["price"] for x in settled)
    return {
        "trades": sum(x["trades"] for x in items),
        "contracts": sum(x["contracts"] for x in items),
        "n_priced": len(priced),
        "median_ratio": _median([x["price"] / x["implied"] for x in priced]),
        # Contract-weighted: total paid / total the legs imply, over the same trades.
        "weighted_ratio": (sum(x["contracts"] * x["price"] for x in priced)
                           / sum(x["contracts"] * x["implied"] for x in priced)) if c_priced else None,
        "n_settled": len(settled),
        "avg_paid": paid / c_settled if c_settled else None,
        "win_rate": sum(x["contracts"] * x["won"] for x in settled) / c_settled if c_settled else None,
        # Buyers' return per $1 staked, before Kalshi's fee on the combo.
        "buyer_return": (sum(x["contracts"] * x["won"] for x in settled) / paid - 1) if paid else None,
    }


def _ratio(x):
    return "n/a" if x is None else f"{x:.2f}x"


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
        "Combos have no standing order book on Kalshi (market makers price each one on request), so "
        "'spread' does not apply. Instead we compare each traded combo's price with the product of its "
        "legs' Kalshi prices: the fair price if the legs were unrelated.",
    ]
    if s["trunc"]:
        lines.append(f"Note: on {s['trunc']} scans the trade feed was too busy to read in full; "
                     "those scans cover only the most recent trades.")
    lines.append("")

    items = _combo_rows(conn, start, end)
    if not items:
        return lines + ["None of this week's sampled combos could be matched to their legs yet.", ""]
    by_type = defaultdict(list)
    for x in items:
        by_type[x["type"]].append(x)
    stats = {t: _combo_stats(v) for t, v in by_type.items()}
    overall = _combo_stats(items)

    cross = stats.get("cross-game")
    lines.append("SUMMARY")
    if cross and cross["weighted_ratio"]:
        survive = None
        if overall["weighted_ratio"] and overall["weighted_ratio"] > 1:
            survive = (cross["weighted_ratio"] - 1) / (overall["weighted_ratio"] - 1)
        lines.append(
            f"- Cross-game combos (every leg from a different game, so the legs are close to unrelated): "
            f"buyers paid {_ratio(cross['weighted_ratio'])} what the legs imply (contract-weighted; "
            f"median combo {_ratio(cross['median_ratio'])}, {cross['n_priced']} combos).")
        if survive is not None:
            lines.append(f"- Across all combos the premium is {_ratio(overall['weighted_ratio'])}; "
                         f"about {max(survive, 0) * 100:.0f}% of that premium is still there in cross-game "
                         "combos, where correlation can't explain it.")
    else:
        lines.append("- Not enough cross-game combos matched to their legs yet.")
    same = stats.get("same-game")
    if same and same["weighted_ratio"]:
        lines.append(f"- Same-game combos: {_ratio(same['weighted_ratio'])}. Their legs are related (a team "
                     "winning and its quarterback throwing for 300 yards tend to happen together), so the "
                     "product of leg prices understates their fair price and part of this is not premium.")
    lines.append("")

    def stat_rows(groups):
        out = []
        for key, st in groups:
            out.append([*key, f"{st['trades']:,}", f"{st['contracts']:,.0f}", st["n_priced"],
                        _ratio(st["median_ratio"]), _ratio(st["weighted_ratio"]),
                        f"{st['avg_paid'] * 100:.1f}% / {st['win_rate'] * 100:.1f}% (n={st['n_settled']})"
                        if st["n_settled"] else "pending",
                        f"{st['buyer_return'] * 100:+.0f}%" if st["buyer_return"] is not None else "pending"])
        return out

    headers = ["Trades", "Contracts", "Combos priced", "Median vs legs", "Weighted vs legs",
               "Avg price / won", "Buyer return per $1"]
    order = ["cross-game", "mixed", "same-game"]
    lines += ["By type:", _table(["Type"] + headers, stat_rows(
        [((t,), stats[t]) for t in order if t in stats])), ""]
    for t in order:
        if t not in by_type:
            continue
        legs = defaultdict(list)
        for x in by_type[t]:
            legs[x["legs"]].append(x)
        lines += [f"{t.capitalize()} combos by number of legs:", _table(["Legs"] + headers, stat_rows(
            [((k,), _combo_stats(legs[k])) for k in ("2 legs", "3-4 legs", "5-8 legs", "9+ legs") if k in legs])), ""]
    lines += [
        "How to read this:",
        "- 'vs legs' = combo price divided by the product of its legs' Kalshi mid prices. 1.00x = priced "
        "exactly as unrelated legs imply; 1.30x = buyers paid 30% more.",
        "- 'Mixed' combos have some legs from the same game and some from different games.",
        "- 'Avg price / won' and 'Buyer return per $1' use settled combos only: if buyers pay 5.0% on "
        "average but combos win 3.0% of the time, buyers lose about 40 cents per dollar. Kalshi's fee on "
        "the combo comes on top. Treat these as noisy until there are several hundred settled combos, "
        "because rare big wins swing them.",
        "- Covers the most-traded combos sampled each scan; leg prices come from the nearest scan "
        "(up to 20 minutes earlier).",
        "",
    ]
    return lines


TTC_BUCKETS = [(0, 900, "under 15 min"), (900, 1800, "15-30 min"), (1800, 3600, "30-60 min"),
               (3600, 3 * 3600, "1-3 hours"), (3 * 3600, 8 * 3600, "3-8 hours"), (8 * 3600, 86400, "8-24 hours")]
HOUR_BLOCKS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)]


def _ttc_label(secs):
    for lo, hi, label in TTC_BUCKETS:
        if lo <= secs < hi:
            return label
    return None


def _hour_label(ts):
    h = datetime.fromtimestamp(ts).hour
    for lo, hi in HOUR_BLOCKS:
        if lo <= h < hi:
            return f"{lo:02d}:00-{hi:02d}:00"


def _implied_vol(r):
    """The volatility at which our model reproduces Kalshi's mid price (above/below markets only)."""
    if r["strike_type"] not in ("greater", "greater_or_equal") or not r["floor_strike"]:
        return None
    if r["yes_bid"] is None or r["yes_ask"] is None:
        return None
    target = (r["yes_bid"] + r["yes_ask"]) / 2
    secs = r["seconds_to_close"] - 40
    a = math.log(r["spot"] / r["floor_strike"])
    if secs <= 0 or not 0.03 < target < 0.97 or abs(a) < 1e-4:
        return None
    tau = secs / volsurface.YEAR_SEC
    f = lambda v: volsurface.prob_above(r["spot"], r["floor_strike"], v, tau) - target
    lo = 0.005
    # Above the strike, probability falls steadily as vol rises. Below it, probability rises then
    # falls; we search only the rising part, which is the economically sensible answer.
    hi = 5.0 if a > 0 else math.sqrt(2 * abs(a) / tau)
    hi = max(min(hi, 5.0), lo * 2)
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def _actual_vol(conn, start, end):
    """Volatility that actually happened, from our own logged spot prices, by hour block."""
    pts = conn.execute(
        "SELECT asset, ts, AVG(spot) spot FROM crypto_fv WHERE ts >= ? AND ts < ? GROUP BY asset, ts "
        "ORDER BY asset, ts", (start, end)).fetchall()
    acc = defaultdict(lambda: [0.0, 0.0])  # block -> [sum of squared returns, seconds]
    prev = None
    for p in pts:
        if prev and prev["asset"] == p["asset"] and 0 < p["ts"] - prev["ts"] <= 300:
            r = math.log(p["spot"] / prev["spot"])
            for key in (_hour_label(p["ts"]), "all"):
                acc[key][0] += r * r
                acc[key][1] += p["ts"] - prev["ts"]
        prev = p
    return {k: math.sqrt(v[0] / v[1] * volsurface.YEAR_SEC) for k, v in acc.items() if v[1] > 3600}


def crypto_weekly_section(conn, start, end):
    lines = ["CRYPTO: ARE INTRADAY GAPS REAL, OR IS OUR VOLATILITY INPUT WRONG?", ""]
    rows = conn.execute(
        "SELECT f.*, m.ticker, m.result, m.strike_type, m.floor_strike, m.cap_strike FROM crypto_fv f "
        "JOIN markets m USING(market_id) WHERE f.ts >= ? AND f.ts < ? AND f.seconds_to_close < 86400 "
        "ORDER BY f.market_id, f.ts", (start, end)).fetchall()
    if not rows:
        return lines + ["No intraday crypto data this week.", ""]
    lines += [
        "This looks only at BTC/ETH markets closing within 24 hours. Three tests separate a real "
        "mispricing from a bad volatility input:",
        "1. Accuracy (Brier score, lower is better) of three forecasts against actual outcomes: our fair "
        "value with options volatility, the same model with recently measured volatility, and Kalshi's "
        "own price. If Kalshi beats our options-vol fair value, the 'gap' was mostly our input being wrong.",
        "2. 'Kalshi-implied vol' is the volatility that makes our model match Kalshi's price. We compare it "
        "with the options vol we used and with the volatility that actually happened. If Kalshi's number "
        "is closer to what happened, Kalshi had the better input.",
        "3. 'Result if taken' is what you would actually have made per contract, after fees, by taking "
        "every gap over 3c the moment it appeared. Positive and steady means a real pricing difference.",
        "",
    ]
    eps = _gap_episodes(rows)
    actual = _actual_vol(conn, start, end)

    def summarize(keyfn, order, extra_actual=False):
        groups = defaultdict(lambda: {"rows": [], "eps": []})
        for r in rows:
            k = keyfn(r)
            if k:
                groups[k]["rows"].append(r)
        for e in eps:
            k = keyfn(e["first"])
            if k:
                groups[k]["eps"].append(e)
        out = []
        for k in order:
            if k not in groups:
                continue
            g = groups[k]
            two = [r for r in g["rows"] if r["yes_bid"] is not None and r["yes_ask"] is not None]
            res = [r for r in two if r["result"] in ("yes", "no")]
            o = lambda r: 1.0 if r["result"] == "yes" else 0.0
            b_iv = _brier([(r["fair_yes"], o(r)) for r in res])
            b_rv = _brier([(r["fair_yes_realised"], o(r)) for r in res])
            b_mk = _brier([((r["yes_bid"] + r["yes_ask"]) / 2, o(r)) for r in res])
            pnls = [e["pnl"] for e in g["eps"] if e["pnl"] is not None]
            row = [k, len(two), len(res),
                   f"{b_iv:.3f}" if b_iv is not None else "n/a",
                   f"{b_rv:.3f}" if b_rv is not None else "n/a",
                   f"{b_mk:.3f}" if b_mk is not None else "n/a",
                   _pct(_median([r["vol"] for r in two])),
                   _pct(_median([r["vol_realised"] for r in two])),
                   _pct(_median([_implied_vol(r) for r in two]))]
            if extra_actual:
                row.append(_pct(actual.get(k)))
            row += [len(g["eps"]), f"{_c(statistics.mean(pnls))} (n={len(pnls)})" if pnls else "pending"]
            out.append(row)
        return out

    base = ["Checks", "With result", "Brier: ours (options vol)", "Brier: ours (recent vol)",
            "Brier: Kalshi", "Options vol", "Recent vol", "Kalshi-implied vol"]
    tail = ["Gaps >3c", "Avg result if taken"]
    lines += ["By time left until the market closes:",
              _table(["Time to close"] + base + tail,
                     summarize(lambda r: _ttc_label(r["seconds_to_close"]), [b[2] for b in TTC_BUCKETS])), ""]
    lines += [f"By time of day (your computer's local time; check time, not close time). Volatility that "
              f"actually happened over the whole week: {_pct(actual.get('all'))}.",
              _table(["Time of day"] + base + ["Actual vol"] + tail,
                     summarize(lambda r: _hour_label(r["ts"]), [f"{lo:02d}:00-{hi:02d}:00" for lo, hi in HOUR_BLOCKS],
                               extra_actual=True)), ""]

    # Plain-English verdict, only when there is enough data to say anything.
    res_rows = [r for r in rows if r["result"] in ("yes", "no") and r["yes_bid"] is not None
                and r["yes_ask"] is not None]
    pnls = [e["pnl"] for e in eps if e["pnl"] is not None]
    lines.append("What this suggests so far:")
    if len({r["market_id"] for r in res_rows}) < 50 or len(pnls) < 30:
        lines.append(f"- Not enough settled data yet to judge ({len({r['market_id'] for r in res_rows})} "
                     f"intraday markets with results, {len(pnls)} settled gaps). Needs roughly 50+ markets "
                     "and 30+ gaps.")
    else:
        o = lambda r: 1.0 if r["result"] == "yes" else 0.0
        b_iv = _brier([(r["fair_yes"], o(r)) for r in res_rows])
        b_mk = _brier([((r["yes_bid"] + r["yes_ask"]) / 2, o(r)) for r in res_rows])
        avg_pnl = statistics.mean(pnls)
        iv = _median([r["vol"] for r in res_rows])
        kv = _median([_implied_vol(r) for r in res_rows])
        act = actual.get("all")
        if b_mk < b_iv:
            lines.append(f"- Kalshi's prices forecast outcomes better than our options-vol fair value "
                         f"({b_mk:.3f} vs {b_iv:.3f}).")
        else:
            lines.append(f"- Our options-vol fair value forecast outcomes at least as well as Kalshi "
                         f"({b_iv:.3f} vs {b_mk:.3f}).")
        if iv and kv and act:
            closer = "Kalshi's" if abs(kv - act) < abs(iv - act) else "the options market's"
            lines.append(f"- Volatility that actually happened was {_pct(act)}; options implied {_pct(iv)}, "
                         f"Kalshi's prices implied {_pct(kv)}. {closer} estimate was closer.")
        lines.append(f"- Taking every gap at first sight returned {_c(avg_pnl)} per contract after fees "
                     f"on average (n={len(pnls)}).")
        if b_mk < b_iv and avg_pnl <= 0:
            lines.append("- Verdict: these gaps look like our volatility input being wrong for short "
                         "horizons, not a real pricing difference.")
        elif b_mk >= b_iv and avg_pnl > 0:
            lines.append("- Verdict: these gaps look like a real pricing difference, not a model error.")
        else:
            lines.append("- Verdict: mixed signals; keep collecting and check the breakdowns above for "
                         "where the gaps do and don't hold up.")
    lines.append("")
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
    if stats:
        lines += _scanner_sections(stats, meta)
    else:
        lines += ["NOTE: THE SCANNER COLLECTED NO DATA THIS WEEK.",
                  "Sections 1 and 2 (spreads and price movement) need scanner data and are skipped; section 3 "
                  "(combos) also comes from the scanner. Sections 4-6 come from the crypto and in-game loggers, "
                  "which run separately, so they are still included below. See the gaps listed above for why "
                  "the scanner did not run.", ""]
    lines += ["3. " + "\n".join(_combo_section(conn, start, end))]
    lines += ["4. " + "\n".join(crypto_weekly_section(conn, start, end))]
    lines += ["5. " + "\n".join(longshot_section(conn, start, end))]
    lines += ["6. " + "\n".join(ingame_section(conn, start, end))]
    lines += ["7. " + "\n".join(paper.report_section(conn, start, end))]
    return "\n".join(lines)


def _scanner_sections(stats, meta):
    """Summary plus sections 1-2 (spreads and price movement), which need scanner data."""
    lines = []

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
    return lines


def ingame_section(conn, start, end):
    """Coverage of the in-game logger: what was collected, so gaps are visible. No analysis yet."""
    lines = ["IN-GAME SPORTS LOGGING (what was collected; no analysis yet)", ""]
    gaps_text, gap_sec = _gaps_text(conn, ["ingame"], start, end)
    rows = conn.execute(
        "SELECT g.league, g.milestone_id, g.ts, g.live_updated_ts, m.result FROM game_snapshots g "
        "JOIN markets m USING(market_id) WHERE g.ts >= ? AND g.ts < ?", (start, end)).fetchall()
    lines += [f"Time without in-game data: {_dur(gap_sec) if gap_sec else 'none'}.", gaps_text, ""]
    if not rows:
        return lines + ["No games in progress were logged this week.", ""]
    by = defaultdict(lambda: {"games": defaultdict(list), "settled": set(), "lag": [], "rows": 0})
    for r in rows:
        b = by[r["league"]]
        b["rows"] += 1
        b["games"][r["milestone_id"]].append(r["ts"])
        if r["result"] in ("yes", "no"):
            b["settled"].add(r["milestone_id"])
        if r["live_updated_ts"]:
            b["lag"].append(r["ts"] - r["live_updated_ts"])
    trows = []
    for league, b in sorted(by.items(), key=lambda kv: -len(kv[1]["games"])):
        mins = [(max(t) - min(t)) / 60 for t in b["games"].values()]
        lag = _median(b["lag"])
        trows.append([league, len(b["games"]), len(b["settled"]), f"{b['rows']:,}", f"{_median(mins):.0f} min",
                      f"{lag:.0f} s" if lag is not None else "n/a"])
    lines += [_table(["League", "Games logged", "With final result", "Price+score snapshots",
                      "Typical minutes logged per game", "Typical score-feed age"], trows), "",
              "'Score-feed age' is how old the score feed's last update was when we read it (a lower bound on "
              "its delay; n/a when the feed doesn't report its update time, as for baseball).", ""]
    return lines


# ---------- longshots ----------

LONGSHOT_MAX_PRICE = 0.10
LONGSHOT_BUCKETS = [(0.0, 0.015, "1c"), (0.015, 0.035, "2-3c"), (0.035, 0.065, "4-6c"), (0.065, 0.10, "7-9c")]


def _wilson(wins, n, z=1.96):
    """Rough 95% range for a win rate from n tries (Wilson interval)."""
    if n == 0:
        return None, None
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _longshot_items(conn, start, end):
    """One item per (market, side) that was offered under 10c: average price, fee, our fair value, outcome."""
    sql = (
        "SELECT x.market_id, x.fair_yes, x.yes_bid, x.yes_ask, x.bid_size, x.ask_size, x.seconds_to_close, "
        "m.event_ticker, m.result, s.fee_type, s.fee_multiplier FROM {t} x JOIN markets m USING(market_id) "
        "LEFT JOIN series s ON s.series_ticker = m.series_ticker WHERE x.ts >= ? AND x.ts < ? "
        "AND (x.yes_ask < ? OR x.yes_bid > ?)")
    acc = {}
    for table in ("crypto_fv", "crypto_far"):
        for r in conn.execute(sql.format(t=table), (start, end, LONGSHOT_MAX_PRICE, 1 - LONGSHOT_MAX_PRICE)):
            sides = []
            if r["yes_ask"] is not None and r["yes_ask"] < LONGSHOT_MAX_PRICE:
                sides.append(("YES", r["yes_ask"], r["ask_size"], r["fair_yes"]))
            if r["yes_bid"] is not None and 1 - r["yes_bid"] < LONGSHOT_MAX_PRICE:
                sides.append(("NO", 1 - r["yes_bid"], r["bid_size"], 1 - r["fair_yes"]))
            for side, price, size, fair in sides:
                fee = fees.taker_fee_per_contract(price, size or 1, r["fee_type"] or "quadratic",
                                                  r["fee_multiplier"] if r["fee_multiplier"] is not None else 1.0)
                a = acc.setdefault((r["market_id"], side), {
                    "side": side, "event": r["event_ticker"], "result": r["result"], "prices": [], "fees": [],
                    "fairs": [], "intraday": r["seconds_to_close"] < 86400})
                a["prices"].append(price)
                a["fees"].append(fee or 0.0)
                a["fairs"].append(fair)
    items = []
    for a in acc.values():
        item = {"side": a["side"], "event": a["event"], "intraday": a["intraday"],
                "price": statistics.mean(a["prices"]), "fee": statistics.mean(a["fees"]),
                "fair": statistics.mean(a["fairs"]), "won": None}
        if a["result"] in ("yes", "no"):
            item["won"] = 1.0 if a["result"].upper() == a["side"] else 0.0
        items.append(item)
    return items


def _longshot_row(label, items):
    n = len(items)
    wins = sum(x["won"] for x in items)
    cost = sum(x["price"] + x["fee"] for x in items)
    lo, hi = _wilson(wins, n)
    return [label, n, len({x["event"] for x in items}),
            f"{statistics.mean(x['price'] for x in items) * 100:.1f}c",
            f"{statistics.mean(x['fair'] for x in items) * 100:.1f}%",
            f"{wins / n * 100:.1f}% ({lo * 100:.1f}-{hi * 100:.1f}%)",
            f"{(wins - cost) / cost * 100:+.0f}%"]


def longshot_section(conn, start, end):
    lines = ["LONGSHOTS: CRYPTO CONTRACTS PRICED UNDER 10 CENTS", ""]
    items = _longshot_items(conn, start, end)
    settled = [x for x in items if x["won"] is not None]
    pending = len(items) - len(settled)
    if not settled:
        return lines + [f"No settled longshot contracts this week yet ({pending} still open).", ""]
    n = len(settled)
    wins = sum(x["won"] for x in settled)
    cost = sum(x["price"] + x["fee"] for x in settled)
    avg_price = statistics.mean(x["price"] for x in settled)
    lines += [
        "Longshot bias = cheap contracts winning less often than their price suggests, so buying them loses "
        "money and selling them (buying the other side) earns it. Each BTC/ETH market counts once per side, "
        "at the average price it was offered at while under 10c. A cheap YES means the price needs a big move "
        "to reach the strike; a cheap NO means YES is nearly certain.",
        "",
        f"SUMMARY: {n:,} longshot contracts settled ({pending:,} still open). They cost {avg_price * 100:.1f}c on "
        f"average and won {wins / n * 100:.1f}% of the time. Buying every one would have returned "
        f"{(wins - cost) / cost * 100:+.0f}% per $1 after Kalshi's fees. Our model put their chance at "
        f"{statistics.mean(x['fair'] for x in settled) * 100:.1f}%.",
        "",
    ]
    headers = ["Group", "Contracts", "Events", "Avg price paid", "Our fair value", "Actually won (95% range)",
               "Buyer return per $1 after fees"]
    rows = []
    for lo, hi, label in LONGSHOT_BUCKETS:
        grp = [x for x in settled if lo <= x["price"] < hi]
        if grp:
            rows.append(_longshot_row(label, grp))
    lines += ["By price:", _table(headers, rows), ""]
    rows = []
    for side in ("YES", "NO"):
        for intraday, h in ((True, "closes within 24h"), (False, "closes later")):
            grp = [x for x in settled if x["side"] == side and x["intraday"] == intraday]
            if grp:
                rows.append(_longshot_row(f"cheap {side}, {h}", grp))
    lines += ["By side and horizon:", _table(headers, rows), ""]
    lines += [
        "How to read this:",
        "- If 'Actually won' is below 'Avg price paid' (and the 95% range stays below it), longshots are "
        "overpriced: a sign of longshot bias that favours the seller.",
        "- 'Events' matters: strikes in the same event (same coin, same close time) tend to win or lose "
        "together, so the evidence is closer to the number of events than the number of contracts. Expect "
        "this to stay noisy until there are several hundred events.",
        "- Fees use the full size shown at the price, as elsewhere. Buying a single contract can cost more, "
        "because each order's fee rounds up to a whole cent.",
        "- Far-away strikes are checked every 30 minutes, closer strikes every 2 minutes.",
        "",
    ]
    return lines


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
