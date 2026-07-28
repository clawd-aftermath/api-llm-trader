import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _symbols import _PRICE_SCALE, scale_price, unscale_price


class NativePriceScalingTests(unittest.TestCase):
    def test_v2_price_scale_matches_market_tick_metadata(self):
        ccxt_price_precision = Decimal("0.0001")
        native_tick_size = Decimal("100000")
        derived_price_unit = ccxt_price_precision / native_tick_size
        self.assertEqual(derived_price_unit, Decimal(1) / _PRICE_SCALE)

    def test_bid_and_ask_use_v2_b9_price_units(self):
        self.assertEqual(
            scale_price("63824.371139", "100000n", side=0),
            "63824371100000n",
        )
        self.assertEqual(
            scale_price("63824.371139", "100000n", side=1),
            "63824371200000n",
        )

    def test_unscale_price_round_trips_b9_value(self):
        self.assertEqual(unscale_price("65000000000000n", "100000n"), 65000.0)


if __name__ == "__main__":
    unittest.main()
