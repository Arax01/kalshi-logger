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


if __name__ == "__main__":
    unittest.main()
