import unittest

import numpy as np
import pandas as pd

import ftse_trade_scanner as sc
import ftse_zones as zb


def candles(days=30, seed=3):
    rng = np.random.default_rng(seed)
    idx = []
    for day in pd.bdate_range("2026-09-01", periods=days):
        idx += list(pd.date_range(day + pd.Timedelta(hours=8), periods=34, freq="15min"))
    idx = pd.DatetimeIndex(idx)
    wave = 100 + 1.5 * np.sin(np.arange(len(idx)) / 9) + rng.normal(0, 0.15, len(idx)).cumsum() * 0.2
    o = wave + rng.normal(0, 0.05, len(idx))
    h = np.maximum(o, wave) + 0.1
    l = np.minimum(o, wave) - 0.1
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": wave}, index=idx)


CFG = sc.INDEX_RULES["FTSE 100"]


class ZoneTests(unittest.TestCase):
    def test_no_rules_matches_the_live_scanner(self):
        df = candles()
        live = sc.qualify_zones(sc.cluster_zones(sc.find_swing_points(df, CFG.swing_lookback), CFG.zone_tolerance_pct), df, CFG)
        mine = zb.build_zones(df, CFG)
        self.assertEqual([(z.low, z.high, z.touch_count) for z in live], [(z.low, z.high, z.touch_count) for z in mine])

    def test_stronger_rules_give_fewer_zones(self):
        df = candles()
        base = len(zb.build_zones(df, CFG))
        self.assertGreater(base, 0)
        for rules in ({"recent_days": 10}, {"merge_atr": 1.0}, {"move_atr": 2.0}):
            self.assertLessEqual(len(zb.build_zones(df, CFG, rules)), base, rules)
        self.assertLess(len(zb.build_zones(df, CFG, {"recent_days": 10, "merge_atr": 1.0, "move_atr": 2.0})), base)

    def test_recent_only_uses_recent_touches(self):
        df = candles()
        first = sorted({s.normalize() for s in df.index})[-10]
        for zone in zb.build_zones(df, CFG, {"recent_days": 10}):
            self.assertTrue(all(p.ts >= first for p in zone.touches))

    def test_merge_close(self):
        a = sc.Zone(100.0, 100.2, [sc.SwingPoint(pd.Timestamp("2026-09-01 09:00"), 100.0, "low")])
        b = sc.Zone(100.3, 100.4, [sc.SwingPoint(pd.Timestamp("2026-09-01 10:00"), 100.3, "low")])
        c = sc.Zone(102.0, 102.1, [sc.SwingPoint(pd.Timestamp("2026-09-01 11:00"), 102.0, "high")])
        merged = zb.merge_close([c, a, b], gap=0.2)
        self.assertEqual([(z.low, z.high, z.touch_count) for z in merged], [(100.0, 100.4, 2), (102.0, 102.1, 1)])

    def test_zones_near(self):
        zones = [sc.Zone(99.0, 99.5), sc.Zone(105.0, 105.5)]
        self.assertEqual(zb.zones_near(zones, 100.0), 1)


if __name__ == "__main__":
    unittest.main()
