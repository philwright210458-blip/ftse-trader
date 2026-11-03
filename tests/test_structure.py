import unittest

import pandas as pd

import ftse_structure as st
from tests.structure_fixture import candles, textbook


class StructureSignalTests(unittest.TestCase):
    def test_textbook_impulse_pullback_breakout(self):
        frame = textbook()
        found = st.signals(frame)
        self.assertEqual(len(found), 1)
        s = found[0]
        self.assertTrue(st.FIB_LOW <= s["depth"] <= st.FIB_HIGH)
        self.assertAlmostEqual(s["target"], s["P"] + s["H"] - s["L"])
        self.assertLess(s["stop"], s["P"])

    def test_uses_only_past_prices(self):
        frame = textbook()
        s = st.signals(frame)[0]
        cut = st.signals(frame.iloc[: s["t"] + 1])
        self.assertEqual([x["t"] for x in cut], [s["t"]])

    def test_too_deep_pullback_cancels(self):
        frame = textbook()
        s = st.signals(frame)[0]
        deep = frame.copy()
        deep.iloc[s["t"] - 1, deep.columns.get_loc("low")] = s["L"] + 0.5      # pullback nearly to the impulse start
        self.assertEqual([x for x in st.signals(deep) if x["t"] == s["t"]], [])


if __name__ == "__main__":
    unittest.main()
