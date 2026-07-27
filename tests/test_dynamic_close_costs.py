import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import PriceSnapshot, Signal, build_close_signal, make_shadow_position


CFG = {
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
                "BZ": {"bid": base_price * 0.9998, "ask": base_price * 1.0002},
                "CL": {"bid": quote_price * 0.9998, "ask": quote_price * 1.0002},
            }
        },
    )


class DynamicCloseCostTests(unittest.TestCase):
    def test_shadow_position_records_one_way_entry_bbo_cost(self):
        position = make_shadow_position(
            CFG,
            Signal("ENTRY", "SHORT_BZ_LONG_CL", "", "test"),
            bz_cl_snapshot(1.0),
            z=2.0,
        )

        self.assertIn("entry_bbo_cost_pct", position)
        self.assertAlmostEqual(position["entry_bbo_cost_pct"], 0.04, places=6)

    def test_close_waits_until_net_profit_covers_costs_and_target(self):
        position = {
            "direction": "SHORT_BZ_LONG_CL",
            "ratio_side": "short_ratio",
            "entry_ratio": 1.0,
            "entry_bbo_cost_pct": 0.04,
        }

        signal = build_close_signal(CFG, bz_cl_snapshot(0.9975), z=0.8, position=position)

        self.assertIsNone(signal)

    def test_close_reports_net_profit_after_bbo_costs_and_slippage(self):
        position = {
            "direction": "SHORT_BZ_LONG_CL",
            "ratio_side": "short_ratio",
            "entry_ratio": 1.0,
            "entry_bbo_cost_pct": 0.04,
        }

        signal = build_close_signal(CFG, bz_cl_snapshot(0.996), z=0.8, position=position)

        self.assertIsNotNone(signal)
        self.assertIn("entry_bbo_cost_pct", signal.details)
        self.assertIn("exit_bbo_cost_pct", signal.details)
        self.assertIn("slippage_tolerance_pct", signal.details)
        self.assertIn("net_profit_pct", signal.details)
        self.assertAlmostEqual(signal.details["entry_bbo_cost_pct"], 0.04, places=6)
        self.assertAlmostEqual(signal.details["exit_bbo_cost_pct"], 0.04, places=6)
        self.assertAlmostEqual(signal.details["slippage_tolerance_pct"], 0.20, places=6)
        self.assertAlmostEqual(signal.details["net_profit_pct"], 0.12, places=6)


if __name__ == "__main__":
    unittest.main()
