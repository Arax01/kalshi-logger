"""Clean re-test of the calibration study, as pre-registered in docs/preregistration-calibration-rerun.md.

* Extends the trade sample to every complete month from October 6, 2026 (the fresh test set).
* Re-checks markets that had not settled at the last pull.
* Writes reports/calibration_rerun.txt. Before the first look date it shows only how much data has
  accumulated, never results, so nobody (including Claude) can peek at the test set early.

The rules are those in calib_report.py, frozen as of the pre-registration commit.
"""
import calendar
import random
import time
from datetime import datetime, timezone

from . import calib_pull, calib_report as cr, config, http, rest_study
from .util import parse_ts

CLEAN_START = calendar.timegm((2026, 10, 6, 0, 0, 0))
CLEAN_LABEL = "October 6, 2026"
LOOKS = [(calendar.timegm((2027, 1, 15, 0, 0, 0)), "January 15, 2027", calendar.timegm((2027, 1, 1, 0, 0, 0))),
         (calendar.timegm((2027, 7, 15, 0, 0, 0)), "July 15, 2027", calendar.timegm((2027, 7, 1, 0, 0, 0)))]
Z = 2.24   # 97.5% ranges, because there are two looks
B = {label: i for i, label in enumerate(cr.LABELS)}
HYPOTHESES = [
    ("H1", "Game winners", "95-99c", +1, "yes_m"),
    ("H2", "Crypto", "95-99c", +1, "yes_t"),
    ("H3", "Combos", "1-5c", -1, "no_m"),
    ("H4", "Combos", "90-95c", +1, "yes_t"),
]


def _complete_months(now):
    """(start, end) of each complete calendar month from the clean start, clipped to start at it."""
    out = []
    y, m = 2026, 10
    while True:
        t0 = calendar.timegm((y, m, 1, 0, 0, 0))
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        t1 = calendar.timegm((ny, nm, 1, 0, 0, 0))
        if t1 + 2 * 86400 > now:
            return out
        out.append((f"{y}-{m:02d}", max(t0, CLEAN_START), t1))
        y, m = ny, nm


def plan(conn, cut_trades, progress):
    for month, t0, t1 in _complete_months(time.time()):
        if conn.execute("SELECT 1 FROM calib_windows WHERE month=? AND source='rerun' LIMIT 1", (month,)).fetchone():
            continue
        rng = random.Random(f"kalshi-calibration-rerun-{month}")
        rate = calib_pull._rate(cut_trades, t0, t1, rng)
        length = int(min(max(calib_pull.TARGET_TRADES_PER_WINDOW / rate, 2), 4 * 3600))
        starts = sorted(rng.randint(t0, t1 - length) for _ in range(calib_pull.WINDOWS_PER_MONTH))
        if cut_trades:
            starts = [s if not (s < cut_trades < s + length) else cut_trades for s in starts]
        conn.executemany("INSERT INTO calib_windows(month,start_ts,length_sec,source) VALUES(?,?,?,'rerun')",
                         [(month, s, length) for s in starts])
        conn.commit()
        progress(f"  {month} (from {datetime.fromtimestamp(t0, timezone.utc):%b %d}): windows of {length}s")


def pull(conn, progress=print):
    cut = http.kalshi.get("/historical/cutoff")
    cut_trades, cut_markets = parse_ts(cut["trades_created_ts"]), parse_ts(cut["market_settled_ts"])
    series = calib_pull._series_map()
    calib_pull.load_fee_changes(conn)
    progress("Planning fresh test-set windows (complete months from October 6, 2026)...")
    plan(conn, cut_trades, progress)
    calib_pull.fetch_windows(conn, cut_trades, progress, source="rerun")
    # Markets that hadn't settled at the last pull get looked up again.
    n = conn.execute("UPDATE calib_markets SET looked_up=0 WHERE result IS NULL AND looked_up=1").rowcount
    conn.commit()
    progress(f"Re-checking {n:,} markets that had not settled before...")
    calib_pull.lookup_markets(conn, cut_markets, series, progress)
    # H5/H6: resting-order moments drawn from the fresh sample, replayed once their market has settled.
    rest_study.sample_fresh(conn, _complete_months(time.time()))
    rest_study.pull_rerun(conn, progress)


def _fresh_groups(conn, max_ts=None):
    old = (cr.HOLDOUT_START, cr.HOLDOUT_LABEL)
    cr.HOLDOUT_START, cr.HOLDOUT_LABEL = CLEAN_START, CLEAN_LABEL
    try:
        return cr.build(conn, max_ts)
    finally:
        cr.HOLDOUT_START, cr.HOLDOUT_LABEL = old


def build_report(conn, now=None):
    now = now or time.time()
    look = None
    for ts, label, data_end in LOOKS:
        if now >= ts:
            look = (label, data_end)
    # At a look, use exactly the pre-registered data range, however late the look is run.
    groups, vols, quarters, sources = _fresh_groups(conn, look[1] if look else None)
    fresh_trades = sum(a.trades for (sp, _, _), a in groups.items() if sp == "holdout")
    L = ["CALIBRATION STUDY: CLEAN RE-TEST (pre-registered)", "=" * 60, "",
         f"Pre-registration: docs/preregistration-calibration-rerun.md (rules frozen October 5, 2026).",
         f"Fresh test set: sampled trades from {CLEAN_LABEL}, complete months only. Ranges are 97.5% "
         "(two looks).", f"Fresh test-set trades so far (settled markets): {fresh_trades:,}.",
         "Look dates: " + "; ".join(label for _, label, _ in LOOKS) + ".", ""]
    rows = []
    for hid, cat, lab, direction, way in HYPOTHESES:
        a = groups.get(("holdout", cat, B[lab]))
        n = a.n_events() if a else 0
        eff = a.effective_events() if a else 0.0
        status = ""
        if look is None:
            status = "results hidden until the first look"
        elif n < cr.MIN_EVENTS or eff < cr.MIN_EVENTS:
            status = "NOT YET TESTABLE"
        else:
            g, glo, ghi = a.ratio("gap", Z)
            r, rlo, rhi = a.ratio(way, Z)
            gap_ok = (glo > 0) if direction > 0 else (ghi < 0)
            ret_ok = r is not None and rlo is not None and rlo > 0
            status = "SUPPORTED" if gap_ok and ret_ok else "NOT SUPPORTED"
            status += f" (gap {cr._c(g)} [{cr._c(glo)} to {cr._c(ghi)}], {cr._side_label(way)} " \
                      f"{cr._pct(r)} [{cr._pct(rlo)} to {cr._pct(rhi)}])"
        rows.append([hid, f"{cat}, {lab}", "YES cheap" if direction > 0 else "YES expensive",
                     cr._side_label(way), n, f"{eff:.0f}", status])
    L += ["PRE-REGISTERED HYPOTHESES (fresh test set only)",
          cr._table(["#", "Bucket", "Claim", "Side that must profit", "Events", "Effective events", "Result"], rows),
          ""]
    # H5/H6: resting NO orders, added October 6, 2026 (resting-order study rules, realistic sizing).
    data_end = look[1] if look else int(now) + 1
    rrows = []
    for hid, (cat, *_x) in rest_study.RERUN_GROUPS.items():
        res = rest_study.rerun_results(conn, hid, data_end)
        verdict = "results hidden until the first look" if look is None else rest_study.rerun_verdict(res)
        for r in res:
            shown = "" if look is None else (
                f"{cr._pct(r['ret'])} [{cr._pct(r['lo'])} to {cr._pct(r['hi'])}]" if r["ret"] is not None else "n/a")
            rrows.append([hid, f"{cat}, NO 30-60c resting", r["wait"], r["orders"], r["events"],
                          f"{r['effective']:.0f}", shown, verdict if r is res[0] else ""])
    L += ["RESTING-ORDER HYPOTHESES (added October 6, 2026; fresh test set only)",
          "Claim: a resting NO order at the best NO bid, when NO costs 30-60c, makes money after fees on the "
          "contracts that fill (typical queue, realistic sizing). SUPPORTED if, at any of the three wait times, "
          "there are 30+ events and 30+ effective events with fills and the return's 99.2% range (z = 2.64) is "
          "above zero.",
          cr._table(["#", "Bucket", "Wait", "Orders placed", "Events with fills", "Effective events",
                     "Return/$1 on filled contracts", "Result"], rrows), ""]
    if look is None:
        L += [f"No results are shown before {LOOKS[0][1]}, by design. The event counts above show whether each "
              "hypothesis will be testable at the first look (it needs 30 events and 30 effective events).", ""]
    else:
        L += [f"Look: {look[0]}. A hypothesis is SUPPORTED only if the gap is in the claimed direction and the named "
              "side's return after fees is above zero, both with 97.5% ranges that exclude zero.", ""]
    # Exploratory: patterns in the earlier data only (never judged here).
    pats = cr.find_patterns(groups, vols)
    L += ["EXPLORATORY (earlier data only, before October 6, 2026; not a test)",
          f"{len(pats)} buckets look mispriced in the earlier data under the frozen rules. Any of them would need "
          "its own future test set; they are listed only to suggest what to pre-register next time.", ""]
    if pats:
        L.append(cr._table(["Category", "Price", "Events", "Gap (earlier data)", "Best side", "Return/$1"],
                           [[p["cat"], cr.LABELS[p["bucket"]], p["events"], cr._rng(*p["gap"], cr._c),
                             cr._side_label(p["best"]), cr._pct(p["returns"][p["best"]])] for p in pats[:20]]))
    return "\n".join(L)


def write_report(conn):
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORT_DIR / "calibration_rerun.txt"
    path.write_text(build_report(conn) + "\n", encoding="utf-8")
    return path
