import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import PriceSnapshot, Signal, build_close_signal, make_shadow_position


CFG = {
    "label": "BZ/CL",
    "shadow_position_enabled": True,
    "close_on_z_reversion": False,
    "close_profit_ratio_pct": 0.10,
    "market_slippage_tolerance_pct": 0.20,
    "position_size_usd": 1000,
}


def bz_cl_snapshot(ratio: float) -> PriceSnapshot:
    base_price = ratio * 100
    quote_price = 100.0
    return PriceSnapshot(
        "BZ",
        "CL",
        base_price,
        quote_price,
        ratio,
        "variational_metadata",
        None,
        {
            "assets": {
                "BZ": {"bid": base_price * 0.9998, "ask": base_price * 1.0002, "funding_rate": 0, "funding_interval_s": 3600},
                "CL": {"bid": quote_price * 0.9998, "ask": quote_price * 1.0002, "funding_rate": 0, "funding_interval_s": 3600},
            }
        },
    )


class MarketOnlyCloseTests(unittest.TestCase):
    def test_signal_tracking_does_not_record_execution_costs(self):
        position = make_shadow_position(
            CFG, Signal("ENTRY", "SHORT_BZ_LONG_CL", "", "test"),
            bz_cl_snapshot(1.03), 2.0, reference_mean=1.0, reference_std=0.01,
        )
        self.assertNotIn("entry_bbo_cost_pct", position)
        self.assertNotIn("legs", position)
        self.assertEqual(position["reference_mean"], 1.0)

    def test_partial_reversion_does_not_close_just_because_it_would_be_profitable(self):
        position = make_shadow_position(
            CFG, Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""),
            bz_cl_snapshot(1.03), 2, reference_mean=1.0, reference_std=0.01,
        )
        self.assertIsNone(build_close_signal(CFG, bz_cl_snapshot(1.02), 0, position))

    def test_legacy_cost_settings_do_not_override_market_reversion(self):
        position = make_shadow_position(
            CFG, Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""),
            bz_cl_snapshot(1.03), 2, reference_mean=1.0, reference_std=0.01,
        )
        config = dict(CFG, close_profit_ratio_pct=100, market_slippage_tolerance_pct=100)
        signal = build_close_signal(config, bz_cl_snapshot(1.003), 2, position)
        self.assertIsNotNone(signal)
        self.assertNotIn("net_profit_pct", signal.details)
        self.assertIn("reference_z", signal.details)


if __name__ == "__main__":
    unittest.main()
