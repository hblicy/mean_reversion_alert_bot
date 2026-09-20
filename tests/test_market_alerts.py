import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
CFG = {"id": "test", "label": "BZ/CL", "pair": "BZ_CL",
       "source": "variational_metadata", "strategy": "two_way", "window_size": 20,
       "z_open": 1.8, "z_close": 0.35, "entry_require_cross": True,
       "entry_max_z": 4.2, "z_vol_max": 0, "shadow_position_enabled": True,
       "max_holding_hours": 72, "sample_interval_sec": 600,
       "position_size_usd": 1000, "close_profit_ratio_pct": 0.2,
       "market_slippage_tolerance_pct": 0.2}


def snapshot(ratio=1.03, spread=0.0):
    return bot.PriceSnapshot("BZ", "CL", ratio * 100, 100, ratio,
        "variational_metadata", NOW, {"assets": {
            ticker: {"bid": price * (1 - spread), "ask": price * (1 + spread),
                     "price_source": "quote", "quote_updated_at": NOW.isoformat()}
            for ticker, price in (("BZ", ratio * 100), ("CL", 100))}})


def tracked_signal(direction="SHORT_BZ_LONG_CL", entry_ratio=1.03):
    return {"direction": direction, "ratio_side": bot.ratio_side(direction),
            "entry_ratio": entry_ratio, "entry_z": 3.0,
            "entry_time": NOW.isoformat(), "reference_mean": 1.0,
            "reference_std": 0.01, "reference_z_close": 0.35}


class MarketAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = bot.AlertState(Path(self.tmp.name) / "state.json")
        self.cfg = bot.init_monitors({"pairs": [CFG]}, self.state)[0]
        self.source = Mock()
        self.cfg["_source"] = self.source
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.clock = patch("bot.utc_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def prepare_entry(self, scale=1.0, spread=0.0):
        history = [1 - 0.0001 * scale, 1 + 0.0001 * scale] * 9 + [1 + 0.04 * scale, 1 + 0.03 * scale]
        self.cfg["_calc"] = bot.ZScoreCalculator(20, history=history)
        self.cfg["_prev_z"] = 3.0
        self.source.snapshot.return_value = snapshot(1 + 0.029 * scale, spread)

    def test_entry_works_without_funding_or_cost_fields(self):
        self.prepare_entry()
        for name in ("position_size_usd", "close_profit_ratio_pct", "market_slippage_tolerance_pct"):
            self.cfg.pop(name)
        bot.run_once([self.cfg], self.state, self.notifier)
        self.notifier.send.assert_called_once()
        pos = self.state.shadow_position("test")
        self.assertAlmostEqual(pos["reference_mean"], 1.0035)
        self.assertGreater(pos["reference_std"], 0)
        self.assertNotIn("legs", pos)
        self.assertNotIn("funding_cost_usd_estimate", pos)

    def test_tiny_move_and_large_spread_do_not_apply_profit_filter(self):
        self.prepare_entry(scale=0.001, spread=0.01)
        bot.run_once([self.cfg], self.state, self.notifier)
        self.notifier.send.assert_called_once()
        self.assertIsNotNone(self.state.shadow_position("test"))

    def test_close_on_frozen_mean_band_without_funding(self):
        sig = bot.build_close_signal(CFG, snapshot(1.003), z=2.5, position=tracked_signal())
        self.assertIsNotNone(sig)
        self.assertIn("回归", sig.reason)
        self.assertNotIn("net_profit_pct", sig.details)

    def test_mean_catching_up_does_not_close_without_actual_reversion(self):
        self.assertIsNone(bot.build_close_signal(CFG, snapshot(1.03), z=0.0, position=tracked_signal()))

    def test_crossing_past_frozen_mean_still_closes(self):
        for direction, entry, current in (("SHORT_BZ_LONG_CL", 1.03, 0.98),
                                           ("LONG_BZ_SHORT_CL", 0.97, 1.02)):
            with self.subTest(direction=direction):
                self.assertIsNotNone(bot.build_close_signal(CFG, snapshot(current), None,
                                                           tracked_signal(direction, entry)))

    def test_reference_survives_restart_and_closes_during_warmup(self):
        self.state.set_shadow_position("test", tracked_signal())
        self.state.save()
        restored = bot.AlertState(self.state.path)
        self.source.snapshot.return_value = snapshot(1.003)
        bot.run_once([self.cfg], restored, self.notifier)
        self.notifier.send.assert_called_once()
        self.assertIsNone(restored.shadow_position("test"))

    def test_source_costs_do_not_change_close_decision(self):
        snap = snapshot(1.003, spread=0.1)
        for asset in snap.metadata["assets"].values():
            asset.update(funding_rate=10.0, funding_interval_s=1)
        self.assertIsNotNone(bot.build_close_signal(CFG, snap, None, tracked_signal()))

    def test_entry_message_is_source_labelled_and_has_no_execution_advice(self):
        self.prepare_entry()
        bot.run_once([self.cfg], self.state, self.notifier)
        self.notifier.send.assert_called_once()
        message = self.notifier.send.call_args.args[0]
        self.assertIn("行情源：Variational", message)
        self.assertIn("自行核对", message)
        for forbidden in ("预估净利", "市价", "-PERP", "建议操作", "资金费预算"):
            self.assertNotIn(forbidden, message)

    def test_close_message_is_not_a_profit_or_fill_claim(self):
        sig = bot.build_close_signal(CFG, snapshot(1.003), None, tracked_signal())
        self.assertIsNotNone(sig)
        message = bot.format_close_message(CFG, snapshot(1.003), None, sig)
        self.assertIn("行情源：Variational", message)
        self.assertIn("不代表", message)
        self.assertNotIn("预估净利", message)

    def test_checked_in_config_has_no_execution_cost_parameters(self):
        config = bot.load_config(bot.DEFAULT_CONFIG)
        for pair in config["pairs"]:
            self.assertEqual(pair["max_holding_hours"], 72)
            for field in ("position_size_usd", "market_slippage_tolerance_pct", "close_profit_ratio_pct", "close_on_z_reversion"):
                self.assertNotIn(field, pair)

    def test_funding_garbage_in_api_response_is_ignored(self):
        source = bot.VariationalMetadataSource()
        source._listing = lambda ticker: {
            "quotes": {"updated_at": NOW.isoformat(), "size_1k": {"bid": "99", "ask": "101"}},
            "funding_rate": {"invalid": True}, "funding_interval_s": "NaN",
        }
        snap = source.snapshot("BZ_CL", {})
        self.assertEqual(snap.ratio, 1.0)
        self.assertNotIn("funding_rate", snap.metadata["assets"]["BZ"])

    def test_reference_exit_threshold_does_not_change_after_config_edit(self):
        pos = tracked_signal()
        self.assertIsNone(bot.build_close_signal(dict(CFG, z_close=3.0), snapshot(1.02), 0, pos))
        self.assertIsNotNone(bot.build_close_signal(dict(CFG, z_close=0.0), snapshot(1.003), 5, pos))

    def test_legacy_signal_is_not_given_a_new_reference_or_entry_time(self):
        pos = tracked_signal()
        for field in ("reference_mean", "reference_std", "reference_z_close"):
            pos.pop(field)
        self.state.set_shadow_position("test", pos)
        self.source.snapshot.return_value = snapshot(1.0)
        with self.assertLogs("alert_bot", level="WARNING"):
            bot.run_once([self.cfg], self.state, self.notifier)
        self.assertEqual(self.state.shadow_position("test"), pos)
        self.notifier.send.assert_not_called()

    def test_exact_reference_band_boundary_triggers_for_both_sides(self):
        for direction, entry, current in (("SHORT_BZ_LONG_CL", 1.03, 1.0035),
                                           ("LONG_BZ_SHORT_CL", 0.97, 0.9965)):
            with self.subTest(direction=direction):
                self.assertIsNotNone(bot.build_close_signal(CFG, snapshot(current), None,
                                                           tracked_signal(direction, entry)))


if __name__ == "__main__":
    unittest.main()
