import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import PriceSnapshot, Signal, VariationalMetadataSource, action_label, build_signal, format_message, ratio_side


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

    def test_bz_cl_signal_contains_both_reference_directions(self):
        signal = build_signal(
            {"strategy": "two_way", "pair": "BZ_CL", "z_open": 1.8},
            z=-2.0,
            prev_z=None,
            z_vol=None,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "LONG_BZ_SHORT_CL")

        signal.details.update(reference_mean=1.04, reference_std=0.01)
        snapshot = PriceSnapshot("BZ", "CL", 91.75, 89.28, 91.75 / 89.28, "variational_metadata", None)
        message = format_message({"label": "BZ/CL", "z_open": 1.8}, snapshot, -2.0, None, signal)
        self.assertIn("多 BZ / 空 CL", message)
        self.assertNotIn("市价", message)

    def test_bz_cl_close_signal_identifies_both_legs(self):
        signal = Signal("CLOSE", "CLOSE_SHORT_BZ_LONG_CL", "", "行情回归",
                        details={"move_pct": 0.1, "holding_hours": 1})
        snapshot = PriceSnapshot("BZ", "CL", 91.75, 89.28, 91.75 / 89.28, "variational_metadata", None)

        self.assertEqual(action_label(signal), "平空 BZ / 平多 CL")
        message = format_message({"label": "BZ/CL"}, snapshot, 0, None, signal)
        self.assertIn("平空 BZ / 平多 CL", message)
        self.assertNotIn("预估净利", message)


if __name__ == "__main__":
    unittest.main()
