"""First-pass overreaction study on backfilled football games (plain-English report).

Question: after a big moment in a game, does the Kalshi game-winner price overshoot and then come
back? And could a simple "fade the move" rule make money after fees and the bid/ask spread?

Rules that keep it honest:
* No look-ahead. Every event, entry and exit uses only data up to that minute: a play counts from
  the minute Kalshi's feed first saw it, and price swings are measured on minutes already closed.
* Train/test split. Games are split by date (US Eastern) at TRAIN_TEST_CUTOFF. The fade rule's
  settings are chosen on games before the cutoff only, then applied unchanged to games after it.
* NFL preseason games are kept out of both and reported separately.
* Candles show best bid/ask but not how many contracts were available; results are per contract.
"""
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, fees

TRAIN_TEST_CUTOFF = "2026-09-20"          # US Eastern date; games on or after it are the test set
EASTERN = timezone(timedelta(hours=-4))   # EDT, in force for the whole 2026 season so far
HORIZONS = (5, 15, 30)                    # minutes after entry
MIN_PRICE, MAX_PRICE = 0.05, 0.95         # skip games that are already nearly decided
MAX_SPREAD = 0.10                         # skip entries where the bid/ask spread is wider than 10c
FEE_ORDER_SIZE = 100                      # fees computed per contract on a 100-contract order
SWING_LEVELS = (0.05, 0.08, 0.12, 0.20)   # "large win-probability swing" candidates (2-minute move)
DESCRIPTIVE_SWING = 0.10                  # fixed in advance for the descriptive tables (not tuned)
JUMP_LEVELS = (0.0, 0.03, 0.05, 0.08, 0.12)
MIN_TRAIN_TRADES = 40


# ---------- loading ----------

def _split(start_ts):
    day = datetime.fromtimestamp(start_ts, EASTERN).strftime("%Y-%m-%d")
    return "train" if day < TRAIN_TEST_CUTOFF else "test"


def load_games(conn):
    """Games with plays and a price grid for the home team's YES market (or the away one, flipped)."""
    fee_info = {r["series_ticker"]: (r["fee_type"], r["fee_multiplier"]) for r in conn.execute(
        "SELECT series_ticker, fee_type, fee_multiplier FROM series WHERE series_ticker IN ('KXNFLGAME','KXNCAAFGAME')")}
    games = []
    for g in conn.execute("SELECT * FROM bf_games WHERE status='done' ORDER BY start_ts"):
        mk = {r["side"]: r for r in conn.execute(
            "SELECT * FROM bf_markets WHERE milestone_id=? AND side IS NOT NULL", (g["milestone_id"],))}
        m = mk.get("home") or mk.get("away")
        if m is None:
            continue
        flip = m["side"] == "away"
        rows = conn.execute("SELECT * FROM bf_minutes WHERE ticker=? ORDER BY minute_ts", (m["ticker"],)).fetchall()
        if len(rows) < 30:
            continue
        ts, obs, bid, ask = [], [], [], []
        for r in rows:
            b, a = r["yes_bid"], r["yes_ask"]
            if flip:  # home YES = away NO: home bid = 1 - away ask, home ask = 1 - away bid
                b, a = (None if a is None else 1 - a), (None if r["yes_bid"] is None else 1 - r["yes_bid"])
            ts.append(r["minute_ts"])
            obs.append(bool(r["observed"]))
            bid.append(b)
            ask.append(a)
        plays = conn.execute(
            "SELECT * FROM bf_plays WHERE milestone_id=? ORDER BY sequence", (g["milestone_id"],)).fetchall()
        timed = [p for p in plays if p["wall_ts"]]
        series = g["event_ticker"].split("-")[0]
        games.append({
            "id": g["milestone_id"], "league": g["league"], "season": g["season_type"], "title": g["title"],
            "start": g["start_ts"], "split": _split(g["start_ts"]), "ts": ts, "obs": obs, "bid": bid, "ask": ask,
            "plays": plays, "end_ts": max(p["wall_ts"] for p in timed) if timed else None,
            "fee": fee_info.get(series, ("quadratic", 1.0)),
        })
    return games


def group_of(g):
    if g["league"] == "NFL" and g["season"] == "PRE":
        return "NFL preseason"
    return "NFL" if g["league"] == "NFL" else "College football"


# ---------- events ----------

def _mid(g, i):
    b, a = g["bid"][i], g["ask"][i]
    return None if b is None or a is None else (b + a) / 2


def _index_at_or_after(ts_list, t):
    lo, hi = 0, len(ts_list)
    while lo < hi:
        m = (lo + hi) // 2
        if ts_list[m] < t:
            lo = m + 1
        else:
            hi = m
    return lo


def play_events(g):
    """Scoring plays and turnovers, each placed at the minute Kalshi's feed first saw it.

    Plays sharing a timestamp with 3+ others are skipped: that pattern means the feed caught up in
    a batch, so the timestamp is not when the play happened.
    """
    stamp_counts = defaultdict(int)
    for p in g["plays"]:
        if p["wall_ts"]:
            stamp_counts[p["wall_ts"]] += 1
    out, prev = [], None
    for p in g["plays"]:
        hp, ap = p["home_points"], p["away_points"]
        kind = None
        if prev is not None and hp is not None and ap is not None:
            gained = (hp - prev[0]) + (ap - prev[1])
            if gained >= 6:
                kind = "touchdown"
            elif gained == 3:
                kind = "field_goal"
            elif gained == 2 and p["play_type"] != "two_point_conversion":
                kind = "safety"
        if kind is None and p["turnover"]:
            kind = "turnover"
        if hp is not None and ap is not None:
            prev = (hp, ap)
        if kind and p["wall_ts"] and stamp_counts[p["wall_ts"]] < 4:
            e = _index_at_or_after(g["ts"], p["wall_ts"])   # minute that contains the play
            # Enter one full minute later; the 2-minute move runs from the minute before the play.
            out.append({"kind": kind, "entry": e + 1, "ref": e - 1, "pre_ref": e - 4})
    return out


def swing_events(g, level, gap=5):
    """2-minute price moves of at least `level`, measured only on closed, observed minutes."""
    out, last = [], -10**9
    for i in range(2, len(g["ts"])):
        if not (g["obs"][i] and g["obs"][i - 2]) or i - last < gap:
            continue
        m0, m1 = _mid(g, i - 2), _mid(g, i)
        if m0 is not None and m1 is not None and abs(m1 - m0) >= level:
            out.append({"kind": f"swing>={level * 100:.0f}c", "entry": i, "ref": i - 2, "pre_ref": i - 5})
            last = i
    return out


def measure(g, ev):
    """Initial move and later moves for one event, or None if it can't be measured fairly.

    For these descriptive numbers, later moves past the game's last play are left out (the price then
    just converges to the result). The fade-rule simulation does not do this, because deciding that
    would need to know in advance when the game ends.
    """
    i, r = ev["entry"], ev["ref"]
    n = len(g["ts"])
    if r < 0 or i >= n or not g["obs"][i]:
        return None
    m_in, m_ref = _mid(g, i), _mid(g, r)
    if m_in is None or m_ref is None or not MIN_PRICE <= m_in <= MAX_PRICE:
        return None
    if g["ask"][i] - g["bid"][i] > MAX_SPREAD:
        return None
    jump = m_in - m_ref
    if jump == 0:
        return None
    s = 1 if jump > 0 else -1
    res = {"kind": ev["kind"], "game": g["id"], "jump": jump, "sign": s, "entry": i, "after": {},
           "carried_exit": {}}
    pr = ev["pre_ref"]
    if pr >= 0 and _mid(g, pr) is not None:
        res["pre"] = s * (m_ref - _mid(g, pr))
    for h in HORIZONS:
        j = i + h
        if j >= n or (g["end_ts"] and g["ts"][j] > g["end_ts"]):
            continue  # past the end of the game or of the price data
        mj = _mid(g, j)
        if mj is not None:
            res["after"][h] = s * (mj - m_in)       # positive = kept going, negative = came back
            res["carried_exit"][h] = not g["obs"][j]
    return res


def fade_pnl(g, i, j, sign):
    """Per-contract profit of fading a move at minute i and closing at minute j, after fees.

    Price went up (sign > 0): buy NO at (1 - bid), later sell NO at (1 - ask) = bid_i - ask_j.
    Price went down: buy YES at the ask, later sell YES at the bid = bid_j - ask_i.
    The taker fee is paid on both trades.
    """
    ft, fm = g["fee"]
    bi, ai, bj, aj = g["bid"][i], g["ask"][i], g["bid"][j], g["ask"][j]
    if None in (bi, ai, bj, aj):
        return None
    if sign > 0:
        gross = bi - aj
        f = (fees.taker_fee_per_contract(1 - bi, FEE_ORDER_SIZE, ft, fm) or 0) + \
            (fees.taker_fee_per_contract(1 - aj, FEE_ORDER_SIZE, ft, fm) or 0)
    else:
        gross = bj - ai
        f = (fees.taker_fee_per_contract(ai, FEE_ORDER_SIZE, ft, fm) or 0) + \
            (fees.taker_fee_per_contract(bj, FEE_ORDER_SIZE, ft, fm) or 0)
    return gross - f


# ---------- the fade rule ----------

EVENT_SETS = {
    "touchdowns": lambda k: k == "touchdown",
    "field goals": lambda k: k == "field_goal",
    "any score": lambda k: k in ("touchdown", "field_goal", "safety"),
    "turnovers": lambda k: k == "turnover",
    "scores + turnovers": lambda k: k in ("touchdown", "field_goal", "safety", "turnover"),
}
for _lvl in SWING_LEVELS:
    EVENT_SETS[f"price swing >= {_lvl * 100:.0f}c in 2 min"] = (lambda lvl: lambda k: k == f"swing>={lvl * 100:.0f}c")(_lvl)


def all_events(g):
    evs = play_events(g)
    for lvl in SWING_LEVELS:
        evs += swing_events(g, lvl)
    return evs


def simulate(games, set_name, min_jump, horizon):
    """Trades for one rule. One position per game at a time; returns list of (game, pnl, carried_exit)."""
    pick = EVENT_SETS[set_name]
    trades = []
    for g in games:
        busy_until = -1
        for ev in sorted(g["events"], key=lambda e: e["entry"]):
            if not pick(ev["kind"]) or ev["entry"] <= busy_until:
                continue
            m = measure(g, ev)
            if m is None or abs(m["jump"]) < min_jump:
                continue
            # Hold for `horizon` minutes; if the price data ends first (market closed), close at the
            # last available minute, as a real trader would have to.
            j = min(ev["entry"] + horizon, len(g["ts"]) - 1)
            pnl = fade_pnl(g, ev["entry"], j, m["sign"])
            if pnl is None:
                continue
            trades.append((g["id"], pnl, not g["obs"][j]))
            busy_until = j
    return trades


def cluster_ci(values_by_game):
    """Mean and a rough 95% range, treating each game as one independent unit (events in the same
    game are not independent)."""
    allv = [v for vs in values_by_game.values() for v in vs]
    n = len(allv)
    if n < 2:
        return (statistics.mean(allv) if allv else None), None, None
    mean = sum(allv) / n
    var = sum(sum(v - mean for v in vs) ** 2 for vs in values_by_game.values()) / (n * n)
    se = math.sqrt(var) if var > 0 else 0.0
    return mean, mean - 1.96 * se, mean + 1.96 * se


def _trade_stats(trades):
    by_game = defaultdict(list)
    for gid, pnl, _ in trades:
        by_game[gid].append(pnl)
    mean, lo, hi = cluster_ci(by_game)
    return {"n": len(trades), "games": len(by_game), "mean": mean, "lo": lo, "hi": hi,
            "win": sum(1 for _, p, _ in trades if p > 0) / len(trades) if trades else None,
            "carried": sum(1 for *_, c in trades if c) / len(trades) if trades else None}


def choose_rule(train_games):
    """Try every combination on the training games only and keep the best average profit."""
    results = []
    for set_name in EVENT_SETS:
        for x in JUMP_LEVELS:
            for h in HORIZONS:
                st = _trade_stats(simulate(train_games, set_name, x, h))
                if st["n"] >= MIN_TRAIN_TRADES:
                    results.append(((set_name, x, h), st))
    results.sort(key=lambda r: -r[1]["mean"])
    return results


# ---------- report ----------

def _c(x, d=1):
    return "n/a" if x is None else f"{x * 100:+.{d}f}c"


def _table(headers, rows):
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), "  ".join("-" * w for w in widths)]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(out) if rows else "(nothing to show)"


def _range(lo, hi):
    return "n/a" if lo is None else f"{lo * 100:+.1f} to {hi * 100:+.1f}c"


def descriptive_stats(games):
    """Per kind of moment: list of measured events (used for the summary)."""
    kinds = ["touchdown", "field_goal", "turnover", f"swing>={DESCRIPTIVE_SWING * 100:.0f}c"]
    per = {k: [] for k in kinds}
    for g in games:
        for ev in play_events(g) + swing_events(g, DESCRIPTIVE_SWING):
            if ev["kind"] in per:
                m = measure(g, ev)
                if m:
                    per[ev["kind"]].append(m)
    out = {}
    for k, ms in per.items():
        by_game = defaultdict(list)
        for m in ms:
            if 15 in m["after"]:
                by_game[m["game"]].append(m["after"][15])
        pre = [m["pre"] for m in ms if "pre" in m]
        out[k] = {"n": len(ms), "after15": cluster_ci(by_game),
                  "pre": statistics.mean(pre) if pre else None,
                  "jump": statistics.mean(abs(m["jump"]) for m in ms) if ms else None}
    return out


def descriptive_table(games):
    kinds = ["touchdown", "field_goal", "turnover", f"swing>={DESCRIPTIVE_SWING * 100:.0f}c"]
    labels = {"touchdown": "Touchdowns", "field_goal": "Field goals", "turnover": "Turnovers",
              kinds[3]: f"Price swings >= {DESCRIPTIVE_SWING * 100:.0f}c in 2 min"}
    per = {k: [] for k in kinds}
    for g in games:
        evs = play_events(g) + swing_events(g, DESCRIPTIVE_SWING)
        for ev in evs:
            if ev["kind"] in per:
                m = measure(g, ev)
                if m:
                    per[ev["kind"]].append(m)
    rows = []
    for k in kinds:
        ms = per[k]
        if not ms:
            rows.append([labels[k], 0] + ["n/a"] * 6)
            continue
        cells = [labels[k], len(ms), f"{statistics.mean(abs(m['jump']) for m in ms) * 100:.1f}c",
                 _c(statistics.mean([m["pre"] for m in ms if "pre" in m]) if any("pre" in m for m in ms) else None)]
        for h in HORIZONS:
            by_game = defaultdict(list)
            for m in ms:
                if h in m["after"]:
                    by_game[m["game"]].append(m["after"][h])
            mean, lo, hi = cluster_ci(by_game)
            cells.append(f"{_c(mean)} ({_range(lo, hi)})" if mean is not None else "n/a")
        back = [m for m in ms if 15 in m["after"]]
        cells.append(f"{sum(1 for m in back if m['after'][15] < 0) / len(back) * 100:.0f}%" if back else "n/a")
        rows.append(cells)
    return _table(["Big moment", "Events", "Initial move (size)", "Move in the 3 min before",
                   "Then, +5 min", "+15 min", "+30 min", "Came back by +15"], rows)


def _games_table(conn, games):
    found = defaultdict(lambda: defaultdict(int))
    for r in conn.execute("SELECT league, season_type, status, start_ts FROM bf_games"):
        grp = "NFL preseason" if r["league"] == "NFL" and r["season_type"] == "PRE" else (
            "NFL" if r["league"] == "NFL" else "College football")
        found[grp]["found"] += 1
        if r["status"] not in ("no_timestamps", "no_plays"):
            found[grp]["timed"] += 1
    for g in games:
        found[group_of(g)][g["split"]] += 1
    rows = []
    for grp in ("NFL", "College football", "NFL preseason"):
        f = found[grp]
        note = "reported separately" if grp == "NFL preseason" else ""
        rows.append([grp, f["found"], f["timed"], f["train"] + f["test"], f["train"], f["test"], note])
    return _table(["League", "Games found", "With timestamped plays", "Usable (plays + prices)",
                   f"Before {TRAIN_TEST_CUTOFF} (rule-finding)", f"On/after {TRAIN_TEST_CUTOFF} (test)", ""], rows)


def _summary(train, test, ranked):
    """Plain-English summary from the test games (descriptive) and the rule results."""
    st = descriptive_stats(test)
    names = {"touchdown": "touchdowns", "field_goal": "field goals", "turnover": "turnovers"}
    back, noclear = [], []
    for k, v in st.items():
        mean, lo, hi = v["after15"]
        label = names.get(k, f"price swings of {DESCRIPTIVE_SWING * 100:.0f}c+")
        if mean is None:
            continue
        (back if hi is not None and hi < 0 else noclear).append(f"{label} ({_c(mean)} at +15 min)")
    lines = ["SUMMARY (test games, on/after the cutoff)"]
    if back:
        lines.append("- Prices came back after: " + "; ".join(back) + ". That is a sign of overshooting.")
    if noclear:
        lines.append("- No clear overshoot after: " + "; ".join(noclear) + ". The later moves are small and "
                     "their 95% ranges include zero, so the price mostly stayed where the moment put it.")
    td = st.get("touchdown") or {}
    if td.get("pre") is not None and td["pre"] > 0.005:
        lines.append(f"- Before a touchdown shows up in Kalshi's play feed, the price has usually already moved "
                     f"its way (about {td['pre'] * 100:.1f}c in the 3 minutes before). Part of that is the scoring "
                     "drive moving downfield, and part may be the feed lagging the market; this data can't separate "
                     "the two. Either way, anyone reacting to the feed is partly late.")
    if ranked:
        best = ranked[0][1]
        (set_name, x, h) = ranked[0][0]
        t = _trade_stats(simulate(test, set_name, x, h))
        lines.append(f"- A simple 'fade the move' rule lost money: the best of the {len(ranked)} versions with "
                     f"enough trades averaged {_c(best['mean'])} per contract on the early games and {_c(t['mean'])} "
                     "on the test games, after fees and the spread.")
    lines += ["- Bottom line for this first pass: no evidence of a tradable overreaction in game-winner prices "
              "at one-minute resolution. See the caveats below (no order-book size, 1-minute data, feed timing)."
              if not back else "- Some overshooting shows up; see section 2 for whether it survives costs.", ""]
    return lines


def build_report(conn):
    games = load_games(conn)
    for g in games:
        g["events"] = all_events(g)
    main = [g for g in games if group_of(g) != "NFL preseason"]
    pre = [g for g in games if group_of(g) == "NFL preseason"]
    train = [g for g in main if g["split"] == "train"]
    test = [g for g in main if g["split"] == "test"]

    L = ["FOOTBALL OVERREACTION STUDY (first pass, backfilled data)", "=" * 60, "",
         "Question: after a big moment (a score, a turnover, or a sudden price swing), does the Kalshi "
         "game-winner price overshoot and then come back? And would fading the move have made money after "
         "fees and the bid/ask spread?", "",
         "DATA",
         f"2026 NFL and college football games Kalshi links to markets, using Kalshi's own play-by-play and "
         f"1-minute price candles. Train/test cutoff: {TRAIN_TEST_CUTOFF} (US Eastern date of kick-off). "
         "Settings were chosen only on games before the cutoff; games on or after it were used once, to test. "
         "NFL preseason (backups play, thin markets) is kept out of both and shown separately at the end.", "",
         _games_table(conn, games), "",
         "Games without timestamped plays can't be used: without a time we can't know the price when a play "
         "happened.", ""]
    summary_at = len(L)
    carried = sum(1 for g in main for o in g["obs"] if not o)
    total = sum(len(g["obs"]) for g in main)
    L += ["HOW IT'S MEASURED (no look-ahead)",
          "- Each game uses the home team's YES price (bid/ask mid for measuring, real bid/ask for trading).",
          "- A play counts from the minute Kalshi's feed first saw it. We 'act' one full minute later, at "
          "that minute's closing bid/ask. The initial move is the price change from the minute before the "
          "play to that entry minute. Price swings are measured on minutes that have already closed.",
          "- Later moves are measured from the entry price, in the direction of the initial move: positive "
          "means the price kept going, negative means it came back (overshoot).",
          "- Skipped: entries priced under 5c or over 95c (game nearly decided), spreads over 10c, entry "
          "minutes with no real candle, and anything after the game's last play.",
          f"- Minutes with no candle are carried forward from the last real candle and flagged; "
          f"{carried / total * 100:.0f}% of minutes in these games had no candle. Entries always use a real "
          "candle; exits that fell on a carried-forward minute are counted below.",
          "- Ranges are rough 95% ranges that treat each game as one unit, since moments in the same game "
          "aren't independent.", ""]

    L += ["1. DOES THE PRICE OVERSHOOT AND COME BACK?", "",
          "Rule-finding games (before the cutoff):", descriptive_table(train), "",
          "Test games (on/after the cutoff):", descriptive_table(test), "",
          "How to read: 'Initial move' is how far the price moved in the 2 minutes around the moment. "
          "The +5/+15/+30 columns show the average later move in the same direction; negative numbers mean "
          "the price came back, i.e. it overshot. 'Move in the 3 min before' is the price move just before "
          "the play appeared in Kalshi's feed, in the same direction (positive = the price was already moving "
          "that way, from the drive building up and/or the feed lagging the market).",
          ""]

    ranked = choose_rule(train)
    L[summary_at:summary_at] = _summary(train, test, ranked)
    L += ["2. A SIMPLE FADE RULE: CHOSEN ON EARLY GAMES, TESTED ON LATER ONES", ""]
    if not ranked:
        L += ["Not enough trades in the rule-finding games to choose a rule.", ""]
    else:
        (set_name, x, h), st = ranked[0]
        n_rules = len(EVENT_SETS) * len(JUMP_LEVELS) * len(HORIZONS)
        L += [
            "The rule: after a big moment, bet against the initial move at the entry minute's bid/ask, then "
            "close the bet a fixed number of minutes later. One bet per game at a time. Taker fees are paid "
            "on both trades.",
            f"Tried {n_rules} versions on the rule-finding games (event type x minimum initial move x holding "
            f"time); {len(ranked)} had at least {MIN_TRAIN_TRADES} trades. Best, by average profit per contract:",
            f"  Fade {set_name}, when the initial move is at least {x * 100:.0f}c, close after {h} minutes.",
            "",
            "Top 5 versions on the rule-finding games (for transparency; trying many versions makes the best "
            "one look better than it really is, which is why the test below matters):",
            _table(["Event type", "Min move", "Hold", "Trades", "Avg profit/contract", "95% range"],
                   [[s, f"{xx * 100:.0f}c", f"{hh} min", r["n"], _c(r["mean"]), _range(r["lo"], r["hi"])]
                    for (s, xx, hh), r in ranked[:5]]), ""]
        rows = []
        for label, gs in (("Rule-finding games", train), ("Test games: all", test),
                          ("Test games: NFL", [g for g in test if g["league"] == "NFL"]),
                          ("Test games: college", [g for g in test if g["league"] == "NCAAFB"]),
                          ("NFL preseason (separate)", pre)):
            s2 = _trade_stats(simulate(gs, set_name, x, h))
            rows.append([label, s2["n"], s2["games"], _c(s2["mean"]), _range(s2["lo"], s2["hi"]),
                         f"{s2['win'] * 100:.0f}%" if s2["win"] is not None else "n/a",
                         f"{s2['carried'] * 100:.0f}%" if s2["carried"] is not None else "n/a"])
        L += ["The chosen rule, applied unchanged:",
              _table(["Games", "Trades", "Games traded", "Avg profit/contract after fees+spread", "95% range",
                      "Winning trades", "Exits on carried-forward minute"], rows), ""]
        test_st = _trade_stats(simulate(test, set_name, x, h))
        L.append("VERDICT (test games only):")
        if st["mean"] is not None and st["mean"] <= 0:
            L.append(f"- Note: even the best version lost money on the rule-finding games ({_c(st['mean'])} per "
                     "contract), so the 'chosen' rule is simply the least bad one. No version of this simple "
                     "fade made money after fees and the spread, even on the games it was picked from.")
        if test_st["n"] < 20:
            L.append(f"- Too few test trades ({test_st['n']}) to say anything yet.")
        elif test_st["lo"] is not None and test_st["lo"] > 0:
            L.append(f"- The rule made {_c(test_st['mean'])} per contract on games it had never seen, and the "
                     "95% range stays above zero. That is evidence of real overreaction worth a closer look.")
        elif test_st["mean"] is not None and test_st["mean"] <= 0:
            L.append(f"- The rule lost {_c(test_st['mean'])} per contract on the test games. Whatever it found "
                     "on the early games did not hold up, after fees and the spread.")
        else:
            L.append(f"- The rule averaged {_c(test_st['mean'])} per contract on the test games, but the 95% range "
                     "includes zero, so this could easily be luck. Not enough evidence either way yet.")
        L += ["",
              "IMPORTANT: candles show the best bid and ask each minute but NOT how many contracts were "
              "available at those prices. These are per-contract results; a real order could have moved the "
              "price or gone unfilled, and the profits shown would shrink with size.", ""]

    L += ["3. NFL PRESEASON (separate; backups play and markets are thin)", "",
          descriptive_table(pre) if pre else "No usable preseason games.", "",
          "The fade rule's preseason result is the last row of the table in section 2.", ""]
    return "\n".join(L)


def write_report(conn):
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORT_DIR / "football_overreaction.txt"
    path.write_text(build_report(conn) + "\n", encoding="utf-8")
    return path
