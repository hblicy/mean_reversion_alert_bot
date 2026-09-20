import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
CFG = {
    "id": "test", "label": "BZ/CL", "pair": "BZ_CL",
    "source": "variational_metadata", "strategy": "two_way", "window_size": 20,
    "z_open": 1.8, "z_close": 0.35, "entry_require_cross": True,
    "entry_max_z": 4.2, "z_vol_max": 1.7, "position_size_usd": 1000,
    "close_profit_ratio_pct": 0.2, "market_slippage_tolerance_pct": 0.2,
    "shadow_position_enabled": True, "max_holding_hours": 72,
    "max_quote_age_sec": 120, "max_quote_skew_sec": 30,
    "sample_interval_sec": 600, "cooldown_sec": 1800,
}


def snapshot(base=100.0, quote=100.0, now=NOW, spread=0.0):
    return bot.PriceSnapshot("BZ", "CL", base, quote, base / quote,
        "variational_metadata", now, {"assets": {
            ticker: {"bid": price * (1 - spread), "ask": price * (1 + spread),
                     "quote_updated_at": now.isoformat(), "price_source": "quote",
                     "funding_rate": 0.0, "funding_interval_s": 3600}
            for ticker, price in (("BZ", base), ("CL", quote))}})


def market_position(cfg, signal, snap, z):
    return bot.make_shadow_position(cfg, signal, snap, z, reference_mean=1.0, reference_std=0.01)


class SignalQualityTests(unittest.TestCase):
    def test_xag_xau_snapshot_and_directions(self):
        source = bot.VariationalMetadataSource()
        source._price = lambda ticker, size: ({"XAG": 30, "XAU": 3000}[ticker], {})
        snap = source.snapshot("XAG_XAU", {})
        self.assertEqual((snap.base, snap.quote, snap.ratio), ("XAG", "XAU", 0.01))
        for z, direction, side in ((2, "SHORT_XAG_LONG_XAU", "short_ratio"),
                                   (-2, "LONG_XAG_SHORT_XAU", "long_ratio")):
            sig = bot.build_signal(dict(CFG, pair="XAG_XAU"), z, z * 1.1, 0.1)
            self.assertEqual(sig.direction, direction)
            self.assertEqual(bot.ratio_side(direction), side)
            self.assertIn("XAG", bot.action_label(sig))
            self.assertIn("XAU", bot.action_label(sig))

    def test_outward_move_and_missing_previous_z_do_not_enter(self):
        for z, prev in ((2.1, 2.0), (-2.1, -2.0), (2.1, None), (2.1, -2.5)):
            self.assertIsNone(bot.build_signal(CFG, z, prev, 0.1))

    def test_two_way_applies_extreme_and_volatility_filters(self):
        for z, vol in ((5, 0.1), (2, 2)):
            sig = bot.build_signal(CFG, z, z + 0.1, vol)
            self.assertTrue(sig is None or not sig.tradeable)

    def test_enabled_volatility_filter_waits_for_enough_observations(self):
        self.assertIsNone(bot.build_signal(CFG, 2.0, 2.1, None))

    def test_crossed_bbo_is_rejected(self):
        snap = snapshot()
        snap.metadata["assets"]["BZ"].update(bid=101, ask=100)
        with self.assertRaises(ValueError):
            bot.validate_bbo(snap)






    def test_adverse_extreme_exit_does_not_wait_for_profit(self):
        with patch("bot.utc_now", return_value=NOW):
            pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
            sig = bot.build_close_signal(CFG, snapshot(102), 5.0, pos)
            self.assertIsNotNone(sig)
            self.assertIn("风险", sig.reason)

    def test_missing_funding_is_not_treated_as_zero_by_source(self):
        source = bot.VariationalMetadataSource()
        source._listing = lambda ticker: {"mark_price": "100", "quotes": {
            "updated_at": NOW.isoformat(), "size_1k": {"bid": "99", "ask": "101"}}}
        _, meta = source._price("BZ", "size_1k")
        self.assertNotIn("funding_rate", meta)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = bot.AlertState(Path(self.tmp.name) / "state.json")
        self.cfg = bot.init_monitors({"pairs": [CFG]}, self.state)[0]
        self.source = Mock()
        self.source.snapshot.return_value = snapshot()
        self.cfg["_source"] = self.source
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.clock = patch("bot.utc_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def run_cycle(self):
        bot.run_once([self.cfg], self.state, self.notifier)

    def test_duplicate_quote_does_not_increase_history(self):
        self.run_cycle()
        self.run_cycle()
        self.assertEqual(self.cfg["_calc"].count, 1)

    def test_stale_skewed_missing_and_future_timestamps_are_rejected(self):
        for timestamp in ((NOW - timedelta(seconds=121)).isoformat(),
                          (NOW - timedelta(seconds=31)).isoformat(), "",
                          (NOW + timedelta(seconds=10)).isoformat()):
            with self.subTest(timestamp=timestamp):
                snap = snapshot()
                snap.metadata["assets"]["BZ"]["quote_updated_at"] = timestamp
                self.source.snapshot.return_value = snap
                self.run_cycle()
                self.assertEqual(self.cfg["_calc"].count, 0)
        self.notifier.send.assert_not_called()

    def test_restart_does_not_reingest_same_quote(self):
        self.run_cycle()
        state = bot.AlertState(self.state.path)
        cfg = bot.init_monitors({"pairs": [CFG]}, state)[0]
        cfg["_source"] = self.source
        bot.run_once([cfg], state, self.notifier)
        self.assertEqual(cfg["_calc"].count, 1)

    def test_72h_exit_survives_data_outage_and_warmup(self):
        self.state.set_shadow_position("test", {"direction": "SHORT_BZ_LONG_CL",
            "entry_time": (NOW - timedelta(hours=72)).isoformat()})
        self.source.snapshot.side_effect = RuntimeError("offline")
        self.run_cycle()
        self.notifier.send.assert_called_once()
        message = self.notifier.send.call_args.args[0]
        self.assertIn("72", message)
        self.assertIn("信号", message)
        self.assertIsNone(self.state.shadow_position("test"))
        self.source.snapshot.assert_not_called()

    def test_failed_timeout_notification_preserves_position_and_retries(self):
        self.state.set_shadow_position("test", {"direction": "SHORT_BZ_LONG_CL",
            "entry_time": (NOW - timedelta(hours=73)).isoformat()})
        self.notifier.send.return_value = False
        self.run_cycle()
        self.assertIsNotNone(self.state.shadow_position("test"))
        self.notifier.send.return_value = True
        self.run_cycle()
        self.assertIsNone(self.state.shadow_position("test"))
        self.assertEqual(self.notifier.send.call_count, 2)

    def test_reversion_exit_does_not_wait_for_statistics_warmup(self):
        pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
        self.state.set_shadow_position("test", pos)
        self.source.snapshot.return_value = snapshot(99, 100)
        self.run_cycle()
        self.notifier.send.assert_called_once()
        self.assertIsNone(self.state.shadow_position("test"))

    def test_before_72h_does_not_time_out(self):
        pos = {"direction": "SHORT_BZ_LONG_CL", "entry_time": (NOW - timedelta(hours=72) + timedelta(seconds=1)).isoformat()}
        self.assertFalse(bot.send_timeout(CFG, pos, self.state, self.notifier))
        self.notifier.send.assert_not_called()

    def test_changed_quote_within_bucket_is_not_added(self):
        self.run_cycle()
        later = NOW + timedelta(seconds=30)
        self.source.snapshot.return_value = snapshot(101, now=later)
        with patch("bot.utc_now", return_value=later):
            self.run_cycle()
        self.assertEqual(self.cfg["_calc"].count, 1)

    def test_gap_rewarms_history_but_preserves_shadow_position(self):
        self.run_cycle()
        pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
        self.state.set_shadow_position("test", pos)
        later = NOW + timedelta(minutes=30)
        self.source.snapshot.return_value = snapshot(now=later)
        with patch("bot.utc_now", return_value=later), self.assertLogs("alert_bot", level="INFO"):
            self.run_cycle()
        self.assertEqual(self.cfg["_calc"].count, 1)
        self.assertIsNotNone(self.state.shadow_position("test"))


    def test_successful_timeout_is_not_sent_again(self):
        self.state.set_shadow_position("test", {"direction": "SHORT_BZ_LONG_CL",
            "entry_time": (NOW - timedelta(hours=72)).isoformat()})
        self.run_cycle()
        self.run_cycle()
        self.notifier.send.assert_called_once()

    def test_unexpected_errors_reach_caller_after_state_saved(self):
        self.source.snapshot.side_effect = RuntimeError("unexpected")
        with self.assertLogs("alert_bot", level="ERROR"), self.assertRaises(RuntimeError):
            self.run_cycle()
        self.assertTrue(self.state.path.exists())

    def prepare_entry(self, base=102.9):
        history = [0.9999, 1.0001] * 9 + [1.04, 1.03]
        self.cfg["_calc"] = bot.ZScoreCalculator(20, history=history)
        self.cfg["_calc"].z_history.extend([2.0] * 10)
        self.cfg["_prev_z"] = 3.0
        self.source.snapshot.return_value = snapshot(base)

    def test_real_entry_flow_records_reference_after_delivery(self):
        self.prepare_entry()
        self.run_cycle()
        self.notifier.send.assert_called_once()
        self.assertIn("本次固定均值", self.notifier.send.call_args.args[0])
        saved = bot.AlertState(self.state.path).shadow_position("test")
        self.assertEqual(saved["direction"], "SHORT_BZ_LONG_CL")
        self.assertAlmostEqual(saved["reference_mean"], 1.0035)
        self.assertEqual(saved["entry_time"], NOW.isoformat())

    def test_failed_entry_notification_does_not_create_position_or_consume_cooldown(self):
        self.prepare_entry()
        self.notifier.send.return_value = False
        with self.assertLogs("alert_bot", level="WARNING"):
            self.run_cycle()
        self.notifier.send.assert_called_once()
        self.assertIsNone(self.state.shadow_position("test"))
        self.assertTrue(self.state.should_send("test:SHORT_BZ_LONG_CL:ENTRY", 1800))

    def test_mean_catching_up_without_ratio_movement_does_not_enter(self):
        self.prepare_entry(103.0)
        self.run_cycle()
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.shadow_position("test"))

    def test_unchanged_quote_can_retry_failed_reversion_notification(self):
        pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
        self.state.set_shadow_position("test", pos)
        self.source.snapshot.return_value = snapshot(99)
        self.notifier.send.return_value = False
        self.run_cycle()
        self.assertIsNotNone(self.state.shadow_position("test"))
        self.notifier.send.return_value = True
        self.run_cycle()
        self.assertIsNone(self.state.shadow_position("test"))
        self.assertEqual(self.notifier.send.call_count, 2)

    def test_missing_funding_does_not_block_entry(self):
        self.prepare_entry()
        self.source.snapshot.return_value.metadata["assets"]["BZ"]["funding_rate"] = None
        self.run_cycle()
        self.notifier.send.assert_called_once()

    def test_old_history_without_timestamps_is_not_reused(self):
        self.state.set_history("test", [1.0] * 20)
        cfg = bot.init_monitors({"pairs": [CFG]}, self.state)[0]
        self.assertEqual(cfg["_calc"].count, 0)

    def test_legacy_position_is_preserved_until_timeout(self):
        self.state.set_shadow_position("test", {"direction": "SHORT_BZ_LONG_CL",
            "entry_time": NOW.isoformat(), "entry_ratio": 1.0, "ratio_side": "short_ratio",
            "entry_base_price": 100, "entry_quote_price": 100})
        with self.assertLogs("alert_bot", level="WARNING"):
            self.run_cycle()
        self.assertIsNotNone(self.state.shadow_position("test"))
        self.notifier.send.assert_not_called()

    def test_api_rejection_keeps_timeout_retryable(self):
        self.state.set_shadow_position("test", {"direction": "SHORT_BZ_LONG_CL",
            "entry_time": (NOW - timedelta(hours=72)).isoformat()})
        notifier = bot.TelegramNotifier()
        notifier.token, notifier.chat_id, notifier.print_messages = "test-token", "test-chat", False
        response = Mock(status_code=429)
        response.json.return_value = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
        with patch("bot.requests.post", return_value=response), self.assertLogs("alert_bot", level="WARNING"):
            bot.run_once([self.cfg], self.state, notifier)
        self.assertIsNotNone(self.state.shadow_position("test"))
        response.status_code = 200
        response.json.return_value = {"ok": True}
        with patch("bot.requests.post", return_value=response):
            bot.run_once([self.cfg], self.state, notifier)
        self.assertIsNone(self.state.shadow_position("test"))

    def test_missing_funding_does_not_block_adverse_risk_exit(self):
        pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
        self.state.set_shadow_position("test", pos)
        self.cfg["_calc"] = bot.ZScoreCalculator(20, history=[1.0] * 20)
        snap = snapshot(102)
        snap.metadata["assets"]["BZ"]["funding_rate"] = None
        self.source.snapshot.return_value = snap
        self.run_cycle()
        self.notifier.send.assert_called_once()
        self.assertIn("风险提醒", self.notifier.send.call_args.args[0])
        self.assertIsNone(self.state.shadow_position("test"))


    def test_missing_listing_does_not_stop_later_timeout(self):
        source = bot.VariationalMetadataSource()
        source._stats = lambda: {"listings": []}
        self.cfg["_source"] = source
        self.state.set_shadow_position("test", {"direction": "SHORT_BZ_LONG_CL",
            "entry_time": (NOW - timedelta(hours=71)).isoformat()})
        with self.assertLogs("alert_bot", level="WARNING"):
            self.run_cycle()
        with patch("bot.utc_now", return_value=NOW + timedelta(hours=1)):
            self.run_cycle()
        self.notifier.send.assert_called_once()
        self.assertIsNone(self.state.shadow_position("test"))

    def test_missing_prices_are_rejected_without_stopping_monitor(self):
        source = bot.VariationalMetadataSource()
        source._stats = lambda: {"listings": [{"ticker": "BZ"}, {"ticker": "CL"}]}
        self.cfg["_source"] = source
        with self.assertLogs("alert_bot", level="WARNING"):
            self.run_cycle()
        self.notifier.send.assert_not_called()

    def test_failed_risk_exit_retries_without_waiting_for_next_sample(self):
        pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
        self.state.set_shadow_position("test", pos)
        self.cfg["_calc"] = bot.ZScoreCalculator(20, history=[1.0] * 20)
        self.source.snapshot.return_value = snapshot(102)
        self.notifier.send.return_value = False
        self.run_cycle()
        self.assertIsNotNone(self.state.shadow_position("test"))
        self.notifier.send.return_value = True
        self.run_cycle()
        self.assertIsNone(self.state.shadow_position("test"))
        self.assertEqual(self.notifier.send.call_count, 2)

    def test_zero_variance_window_does_not_label_new_deviation_as_reversion(self):
        self.cfg["_calc"] = bot.ZScoreCalculator(20, history=[1.0] * 20)
        self.run_cycle()
        pos = market_position(CFG, bot.Signal("ENTRY", "SHORT_BZ_LONG_CL", "", ""), snapshot(), 2)
        self.state.set_shadow_position("test", pos)
        self.cfg["close_on_z_reversion"] = True
        self.source.snapshot.return_value = snapshot(102)
        self.run_cycle()
        self.notifier.send.assert_not_called()
        self.assertIsNotNone(self.state.shadow_position("test"))


if __name__ == "__main__":
    unittest.main()
