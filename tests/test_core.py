"""Offline checks of the pure calculations. Run: python -m unittest discover tests"""
import math
import unittest

from kalshi_logger import classify, fees, volsurface


class FeeTests(unittest.TestCase):
    def test_matches_kalshi_worked_example(self):
        # docs.kalshi.com Fee Rounding: 1 contract at $0.055 -> model fee $0.00363825, charged $0.01
        self.assertAlmostEqual(fees.TAKER_RATE * 0.055 * 0.945, 0.00363825, places=10)
        self.assertEqual(fees.taker_fee_total(0.055, 1), 0.01)

    def test_peak_fee_at_50c(self):
        self.assertEqual(fees.taker_fee_total(0.50, 100), 1.75)
        self.assertAlmostEqual(fees.taker_fee_per_contract(0.50, 100), 0.0175)

    def test_symmetric_and_multiplier(self):
        self.assertEqual(fees.taker_fee_total(0.2, 50), fees.taker_fee_total(0.8, 50))
        self.assertEqual(fees.taker_fee_total(0.5, 100, "quadratic", 0), 0.0)
        self.assertIsNone(fees.taker_fee_total(0.5, 100, "flat", 1))


class VolTests(unittest.TestCase):
    def setUp(self):
        now = 1_000_000_000
        self.now = now
        flat = lambda v: [(-0.2, v), (0.0, v), (0.2, v)]
        self.surface = volsurface.Surface("BTC", [
            volsurface.Smile(now + 86400, 100.0, flat(0.40)),
            volsurface.Smile(now + 7 * 86400, 100.0, flat(0.60)),
        ], now)

    def test_total_variance_interpolation(self):
        target = self.now + 4 * 86400
        vol, method, lo, hi, v_lo, v_hi = self.surface.vol(100.0, target, self.now)
        w = 0.4 ** 2 * 1 + (0.6 ** 2 * 7 - 0.4 ** 2 * 1) * (4 - 1) / (7 - 1)
        self.assertEqual(method, "interpolated")
        self.assertAlmostEqual(vol, math.sqrt(w / 4), places=9)
        self.assertEqual((v_lo, v_hi), (0.40, 0.60))

    def test_before_first_expiry_uses_first_vol(self):
        vol, method, *_ = self.surface.vol(100.0, self.now + 6 * 3600, self.now)
        self.assertEqual(method, "before_first_expiry")
        self.assertAlmostEqual(vol, 0.40)

    def test_prob_above(self):
        self.assertAlmostEqual(volsurface.prob_above(100, 100, 0.5, 1.0), volsurface.norm_cdf(-0.25), places=12)
        self.assertGreater(volsurface.prob_above(110, 100, 0.3, 0.01), 0.9)

    def test_expiry_parse(self):
        self.assertEqual(volsurface.parse_expiry("4OCT26"), 1791100800)


class ClassifyTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(classify.classify("KXNFLGAME", "Sports", ["Football"])["market_group"], "game_winner")
        c = classify.classify("KXNBAPTS", "Sports", ["Basketball"])
        self.assertEqual((c["market_group"], c["league"], c["stat_type"]), ("player_prop", "NBA", "points"))
        c = classify.classify("KXNFLPASSYDS", "Sports", ["Football"])
        self.assertEqual(c["stat_type"], "passing_yards")
        self.assertEqual(classify.classify("KXNCAAF2HSPREAD", "Sports", [])["market_group"], "game_line")
        self.assertEqual(classify.classify("KXEPLGAME", "Sports", ["Soccer"])["market_group"], "game_winner")
        self.assertEqual(classify.classify("KXBTCD", "Crypto", [])["market_group"], "crypto")
        self.assertEqual(classify.classify("X", "Combo", [], is_combo=True)["market_group"], "combo")



class ReportLogicTests(unittest.TestCase):
    def test_combo_types(self):
        from kalshi_logger.reports import _combo_type
        cross = [{"market": "KXNFLGAME-26OCT05ATLNO-NO"}, {"market": "KXNFLGAME-26OCT05DETCAR-DET"}]
        same = [{"market": "KXNFLGAME-26OCT05ATLNO-NO"}, {"market": "KXNFLPASSYDS-26OCT05ATLNO-NOTSHOUGH6-250"}]
        self.assertEqual(_combo_type(cross), "cross-game")
        self.assertEqual(_combo_type(same), "same-game")
        self.assertEqual(_combo_type(same + cross[1:]), "mixed")

    def test_implied_vol_round_trip(self):
        from kalshi_logger.reports import _implied_vol
        for spot, strike in ((85000, 84800), (85000, 85300)):
            secs = 3600
            p = volsurface.prob_above(spot, strike, 0.30, (secs - 40) / volsurface.YEAR_SEC)
            row = {"strike_type": "greater", "floor_strike": strike, "yes_bid": p - 0.001, "yes_ask": p + 0.001,
                   "seconds_to_close": secs, "spot": spot}
            self.assertAlmostEqual(_implied_vol(row), 0.30, places=3)

    def test_wilson_range(self):
        from kalshi_logger.reports import _wilson
        lo, hi = _wilson(2, 100)
        self.assertTrue(0.0 < lo < 0.02 < hi < 0.08)
        self.assertEqual(_wilson(0, 0), (None, None))

    def test_far_strike_logging_every_30_min(self):
        from unittest import mock
        from kalshi_logger import crypto

        class FakeConn:
            def execute(self, sql, params=()):
                pass

        t0 = 1_791_000_000
        far = lambda ticker, fair, bid, ask, now: crypto._log_far_strike(
            FakeConn(), {"ticker": ticker}, fair, 0.3, 85000, bid, ask, 100, 100, 3600, now)
        with mock.patch.object(crypto.catalog, "ensure_market", return_value=(1, None)):
            crypto._far_last_logged.clear()
            self.assertTrue(far("A", 0.005, None, 0.01, t0))
            self.assertFalse(far("A", 0.005, None, 0.01, t0 + 600))     # too soon
            self.assertTrue(far("A", 0.005, None, 0.01, t0 + 1800))     # 30 minutes later
            self.assertFalse(far("B", 0.50, 0.40, 0.60, t0))            # not a far strike
            self.assertFalse(far("C", 0.001, None, None, t0))           # no quotes to learn from


class WeeklyReportTests(unittest.TestCase):
    def test_sections_present_without_scanner_data(self):
        import sqlite3
        import tempfile
        from kalshi_logger import db, reports
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(f"{tmp}/t.db")
            conn.row_factory = sqlite3.Row
            conn.executescript(db.SCHEMA)
            text = reports.scanner_weekly(conn, 0, 2_000_000_000, "test")
            conn.close()
        self.assertIn("THE SCANNER COLLECTED NO DATA THIS WEEK", text)
        for heading in ("3. COMBOS", "4. CRYPTO", "5. LONGSHOTS", "6. IN-GAME"):
            self.assertIn(heading, text)


def _synthetic_game(prices, plays=(), obs=None):
    ts = [1_790_000_000 + 60 * i for i in range(len(prices))]
    return {"id": "g1", "league": "NFL", "season": "REG", "ts": ts,
            "obs": list(obs) if obs else [True] * len(prices),
            "bid": [p - 0.01 for p in prices], "ask": [p + 0.01 for p in prices],
            "plays": list(plays), "end_ts": ts[-1], "fee": ("quadratic", 1.0)}


class OverreactionTests(unittest.TestCase):
    def test_minute_grid_flags_missing_minutes(self):
        from kalshi_logger.backfill import minute_grid
        c = lambda ts, b, a: {"end_period_ts": ts, "yes_bid": {"close_dollars": b}, "yes_ask": {"close_dollars": a},
                              "price": {"close_dollars": a}, "volume_fp": "5"}
        rows = minute_grid([c(60, "0.40", "0.42"), c(240, "0.50", "0.52")])
        self.assertEqual([r[0] for r in rows], [60, 120, 180, 240])
        self.assertEqual([r[1] for r in rows], [1, 0, 0, 1])          # observed flags
        self.assertEqual(rows[1][2:], (0.40, 0.42, None, 0.0))        # carried forward, no trade, no volume

    def test_play_placed_at_minute_first_seen(self):
        from kalshi_logger import overreaction as o
        g = _synthetic_game([0.5] * 20)
        play = {"home_points": 7, "away_points": 0, "turnover": None, "play_type": "pass",
                "wall_ts": g["ts"][5] - 30}
        g["plays"] = [{"home_points": 0, "away_points": 0, "turnover": None, "play_type": "kickoff",
                       "wall_ts": g["ts"][1]}, play]
        ev = [e for e in o.play_events(g) if e["kind"] == "touchdown"][0]
        self.assertEqual((ev["ref"], ev["entry"]), (4, 6))   # minute before the play, one minute after

    def test_fade_pnl_includes_spread_and_fees(self):
        from kalshi_logger import overreaction as o
        g = _synthetic_game([0.50, 0.60, 0.55])
        # Price rose, so fade = buy NO at 1-bid(0.59)=0.41, sell NO later at 1-ask(0.56)=0.44.
        pnl = o.fade_pnl(g, 1, 2, +1)
        fee = 2 * 0.0172  # about 1.72c each side at these prices, 100-contract orders
        self.assertAlmostEqual(pnl, (0.59 - 0.56) - fee, places=3)

    def test_no_lookahead_in_trade_decisions(self):
        from kalshi_logger import overreaction as o
        prices = [0.50] * 10 + [0.65] * 5 + [0.60] * 45
        g = _synthetic_game(prices)
        g["events"] = o.swing_events(g, 0.12)
        before = [t for t in o.simulate([g], "price swing >= 12c in 2 min", 0.05, 15)]
        g2 = _synthetic_game(prices[:13] + [0.99] * (len(prices) - 13))   # change only the future
        g2["events"] = o.swing_events(g2, 0.12)
        after = o.simulate([g2], "price swing >= 12c in 2 min", 0.05, 15)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)   # same trade taken; only its profit differs
        self.assertNotEqual(before[0][1], after[0][1])

    def test_one_position_per_game_and_forced_exit(self):
        from kalshi_logger import overreaction as o
        prices = [0.50, 0.50, 0.70, 0.70, 0.50, 0.50, 0.70, 0.70]
        g = _synthetic_game(prices)
        g["events"] = o.swing_events(g, 0.12, gap=1)
        trades = o.simulate([g], "price swing >= 12c in 2 min", 0.0, 30)
        self.assertEqual(len(trades), 1)   # held to the end of the data, so no overlapping second trade


class CalibrationTests(unittest.TestCase):
    def test_buckets(self):
        from kalshi_logger.calib_report import _bucket, LABELS
        self.assertEqual(LABELS[_bucket(0.01)], "1-5c")
        self.assertEqual(LABELS[_bucket(0.05)], "5-10c")
        self.assertEqual(LABELS[_bucket(0.50)], "50-60c")
        self.assertEqual(LABELS[_bucket(0.99)], "95-99c")

    def test_fees_taker_and_maker(self):
        from kalshi_logger.calib_report import trade_fees
        self.assertEqual(trade_fees("quadratic", 1.0, 0.5, 100), (1.75, 0.0))          # makers free
        t, m = trade_fees("quadratic_with_maker_fees", 1.0, 0.5, 100)
        self.assertEqual((t, m), (1.75, 0.44))                                           # 0.25x, rounded up
        self.assertEqual(trade_fees("quadratic_with_combo_maker_fees", 1.0, 0.5, 100)[1], 0.88)

    def test_fee_in_effect_at_trade_time(self):
        import sqlite3
        from kalshi_logger import db
        from kalshi_logger.calib_report import FeeBook
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA.replace("PRAGMA journal_mode=WAL;", ""))
        conn.execute("INSERT INTO calib_fee_changes VALUES('S', 1000, 'quadratic_with_maker_fees', 1)")
        conn.execute("INSERT INTO series(series_ticker, fee_type, fee_multiplier) VALUES('T', 'quadratic', 0)")
        book = FeeBook(conn)
        self.assertEqual(book.at("S", 999), ("quadratic", 1.0))
        self.assertEqual(book.at("S", 1001), ("quadratic_with_maker_fees", 1))
        self.assertEqual(book.at("T", 5), ("quadratic", 0))

    def test_taker_and_maker_sides(self):
        from kalshi_logger.calib_report import Acc
        a = Acc()
        # Taker bought YES at 30c, YES won; maker (resting NO buyer) lost. No fees for simplicity.
        a.add("E1", "M1", 0.30, 10, 1.0, True, 0.0, 0.0)
        self.assertAlmostEqual(a.ratio("yes_t")[0], 1 / 0.30 - 1)
        self.assertAlmostEqual(a.ratio("no_m")[0], -1.0)
        self.assertIsNone(a.ratio("yes_m")[0])          # nobody bought YES with a resting order
        self.assertAlmostEqual(a.ratio("gap")[0], 0.70)  # won (1.0) minus price (0.30)


class RerunTests(unittest.TestCase):
    def test_results_hidden_before_first_look(self):
        import calendar
        import sqlite3
        from kalshi_logger import calib_rerun, db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA.replace("PRAGMA journal_mode=WAL;", ""))
        before = calib_rerun.build_report(conn, now=calendar.timegm((2027, 1, 14, 0, 0, 0)))
        self.assertIn("results hidden until the first look", before)
        after = calib_rerun.build_report(conn, now=calendar.timegm((2027, 1, 16, 0, 0, 0)))
        self.assertNotIn("results hidden", after)
        self.assertIn("NOT YET TESTABLE", after)

    def test_fresh_months_are_complete_months_from_oct_6(self):
        import calendar
        from kalshi_logger import calib_rerun
        months = calib_rerun._complete_months(calendar.timegm((2027, 1, 15, 0, 0, 0)))
        self.assertEqual([m for m, _, _ in months], ["2026-10", "2026-11", "2026-12"])
        self.assertEqual(months[0][1], calib_rerun.CLEAN_START)


class BooksTests(unittest.TestCase):
    def test_top_levels_reads_best_prices_from_the_end(self):
        from kalshi_logger.books import top_levels
        side = [["0.0100", "1300.00"], ["0.0200", "158.00"], ["0.0300", "7.00"], ["0.0400", "1.00"]]
        self.assertEqual(top_levels(side, 3), [400, 1.0, 300, 7.0, 200, 158.0])
        self.assertEqual(top_levels([["0.5000", "5.00"]], 3), [5000, 5.0, None, None, None, None])
        self.assertEqual(top_levels([], 2), [None] * 4)


if __name__ == "__main__":
    unittest.main()
