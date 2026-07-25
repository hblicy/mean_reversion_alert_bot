import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import Signal, VariationalMetadataSource, action_label, build_signal, ratio_side, suggested_operations


class BzClSupportTests(unittest.TestCase):
    def test_variational_snapshot_uses_bz_over_cl(self):
        source = VariationalMetadataSource()
        prices = {
            "BZ": (91.75, {"ticker": "BZ"}),
            "CL": (89.28, {"ticker": "CL"}),
        }
        source._price = lambda ticker, quote_size: prices[ticker]

        snapshot = source.snapshot("BZ_CL", {"quote_size": "size_1k"})

        self.assertEqual((snapshot.base, snapshot.quote), ("BZ", "CL"))
        self.assertEqual(snapshot.ratio, 91.75 / 89.28)

    def test_high_bz_cl_ratio_means_short_bz_long_cl(self):
        signal = build_signal(
            {"strategy": "two_way", "pair": "BZ_CL", "z_open": 1.8},
            z=2.0,
            prev_z=None,
            z_vol=None,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "SHORT_BZ_LONG_CL")
        self.assertEqual(ratio_side(signal.direction), "short_ratio")

    def test_bz_cl_signal_contains_both_manual_operations(self):
        signal = build_signal(
            {"strategy": "two_way", "pair": "BZ_CL", "z_open": 1.8},
            z=-2.0,
            prev_z=None,
            z_vol=None,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "LONG_BZ_SHORT_CL")

        operations = suggested_operations(
            signal,
            type("Snapshot", (), {"base_price": 91.75, "quote_price": 89.28})(),
        )
        self.assertEqual(len(operations), 2)
        self.assertIn("LONG BZ-PERP", operations[0])
        self.assertIn("SHORT CL-PERP", operations[1])

    def test_bz_cl_close_signal_contains_both_manual_operations(self):
        signal = Signal("CLOSE", "CLOSE_SHORT_BZ_LONG_CL", "", "盈利目标")
        snapshot = type("Snapshot", (), {"base_price": 91.75, "quote_price": 89.28})()

        self.assertEqual(action_label(signal), "平空 BZ / 平多 CL")
        operations = suggested_operations(signal, snapshot)
        self.assertEqual(len(operations), 2)
        self.assertIn("回补 BZ-PERP", operations[0])
        self.assertIn("平 CL-PERP 多单", operations[1])


if __name__ == "__main__":
    unittest.main()
