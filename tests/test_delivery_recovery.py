import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_market_alerts import bot, CFG, NOW, snapshot


class StateLoadTests(unittest.TestCase):
    def test_missing_state_is_a_valid_first_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = bot.AlertState(Path(tmp) / "missing.json")
            self.assertEqual(state.data, {"alerts": {}, "history": {}})

    def test_unreadable_state_stops_startup_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            original = json.dumps({"shadow_positions": {"test": {"entry_time": NOW.isoformat()}}})
            path.write_text(original, encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=PermissionError("read denied")):
                with self.assertRaises(PermissionError):
                    bot.AlertState(path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_corrupt_state_stops_startup_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"shadow_positions":', encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                bot.AlertState(path)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"shadow_positions":')


class EntryRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = bot.AlertState(Path(tmp.name) / "state.json")
        self.cfg = bot.init_monitors({"pairs": [CFG]}, self.state)[0]
        self.source = Mock()
        self.cfg["_source"] = self.source
        self.cfg["_calc"] = bot.ZScoreCalculator(20, history=[.9999, 1.0001] * 9 + [1.04, 1.03])
        self.cfg["_prev_z"] = 3.0
        self.source.snapshot.return_value = snapshot(1.029)
        self.notifier = Mock()
        self.notifier.send.side_effect = [False, True]
        clock = patch("bot.utc_now", return_value=NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def run_cycle(self):
        bot.run_once([self.cfg], self.state, self.notifier)

    def test_failed_entry_retries_same_quote_without_resampling(self):
        self.run_cycle()
        self.assertIsNone(self.state.shadow_position("test"))
        self.assertTrue(self.state.should_send("test:SHORT_BZ_LONG_CL:ENTRY", 1800))
        history = self.cfg["_calc"].dump_history()
        z_count = len(self.cfg["_calc"].z_history)
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(self.cfg["_calc"].dump_history(), history)
        self.assertEqual(len(self.cfg["_calc"].z_history), z_count)
        pos = self.state.shadow_position("test")
        self.assertAlmostEqual(pos["reference_mean"], 1.0035)
        self.assertEqual(pos["entry_time"], self.clock.return_value.isoformat())
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)

    def test_network_exception_also_retries(self):
        self.notifier.send.side_effect = [bot.requests.Timeout("offline"), True]
        self.run_cycle()
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNotNone(self.state.shadow_position("test"))

    def test_pending_entry_survives_restart(self):
        self.run_cycle()
        self.state = bot.AlertState(self.state.path)
        self.cfg = bot.init_monitors({"pairs": [CFG]}, self.state)[0]
        self.cfg["_source"] = self.source
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertAlmostEqual(self.state.shadow_position("test")["reference_mean"], 1.0035)

    def test_retry_uses_current_valid_quote(self):
        self.run_cycle()
        self.source.snapshot.return_value = snapshot(1.028)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(self.state.shadow_position("test")["entry_ratio"], 1.028)

    def test_pending_entry_survives_restart_with_btc_volatility_filter(self):
        self.cfg["z_vol_max"] = 1.7
        self.cfg["_calc"].z_history.extend([2.0] * 10)
        self.run_cycle()
        self.state = bot.AlertState(self.state.path)
        self.cfg = bot.init_monitors({"pairs": [{**CFG, "z_vol_max": 1.7}]}, self.state)[0]
        self.cfg["_source"] = self.source
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNotNone(self.state.shadow_position("test"))
        self.assertEqual(len(self.cfg["_calc"].z_history), 0)

    def test_recovered_price_cancels_retry_permanently(self):
        self.run_cycle()
        self.source.snapshot.return_value = snapshot(1.004)
        self.run_cycle()
        self.source.snapshot.return_value = snapshot(1.029)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_adverse_move_cancels_retry(self):
        self.run_cycle()
        self.source.snapshot.return_value = snapshot(1.05)
        self.run_cycle()
        self.source.snapshot.return_value = snapshot(1.029)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_retry_cannot_change_direction_when_cross_filter_disabled(self):
        self.cfg["entry_require_cross"] = False
        self.run_cycle()
        self.source.snapshot.return_value = snapshot(.97)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIsNone(self.state.shadow_position("test"))

    def test_new_sample_does_not_reuse_old_entry_confirmation(self):
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=600)
        fresh = snapshot(1.029)
        for asset in fresh.metadata["assets"].values():
            asset["quote_updated_at"] = self.clock.return_value.isoformat()
        self.source.snapshot.return_value = fresh
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIsNone(self.state.shadow_position("test"))

    def test_expired_signal_is_not_revived_by_new_quote(self):
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=121)
        fresh = snapshot(1.029)
        for asset in fresh.metadata["assets"].values():
            asset["quote_updated_at"] = self.clock.return_value.isoformat()
        self.source.snapshot.return_value = fresh
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIsNone(self.state.shadow_position("test"))

    def test_stale_quote_does_not_retry(self):
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=121)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)

    def set_quote(self, ratio, base_seconds, quote_seconds=None):
        snap = snapshot(ratio)
        for ticker, offset in (("BZ", base_seconds), ("CL", base_seconds if quote_seconds is None else quote_seconds)):
            snap.metadata["assets"][ticker]["quote_updated_at"] = (NOW + timedelta(seconds=offset)).isoformat()
        self.source.snapshot.return_value = snap

    def test_quote_older_than_entry_cannot_close_tracking(self):
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = True
        self.run_cycle()
        position = self.state.shadow_position("test")
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.set_quote(1.0, -30)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.state.shadow_position("test"), position)

    def test_one_leg_time_regression_cannot_close_tracking(self):
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = True
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.set_quote(1.0, -10, 10)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIsNotNone(self.state.shadow_position("test"))

    def test_existing_state_uses_saved_sample_time_until_next_valid_quote(self):
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = True
        self.run_cycle()
        self.state.data.pop("latest_quote_timestamps", None)
        self.state.save()
        self.state = bot.AlertState(self.state.path)
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.set_quote(1.0, -30)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIsNotNone(self.state.shadow_position("test"))

    def test_retry_entry_quote_time_survives_restart(self):
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.set_quote(1.028, 30)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.state = bot.AlertState(self.state.path)
        self.cfg = bot.init_monitors({"pairs": [CFG]}, self.state)[0]
        self.cfg["_source"] = self.source
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = True
        self.clock.return_value = NOW + timedelta(seconds=60)
        self.set_quote(1.0, 15)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNotNone(self.state.shadow_position("test"))

    def test_later_intrabucket_quote_prevents_subsequent_regression(self):
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = True
        self.run_cycle()
        history = self.cfg["_calc"].dump_history()
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.set_quote(1.028, 30)
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=60)
        self.set_quote(1.0, 15)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.cfg["_calc"].dump_history(), history)
        self.assertIsNotNone(self.state.shadow_position("test"))
        self.set_quote(1.0, 60)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNone(self.state.shadow_position("test"))

    def test_regressed_quote_cannot_retry_entry_but_same_quote_can(self):
        self.run_cycle()
        self.clock.return_value = NOW + timedelta(seconds=30)
        self.set_quote(1.028, -30)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIsNone(self.state.shadow_position("test"))
        self.set_quote(1.029, 0)
        self.run_cycle()
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNotNone(self.state.shadow_position("test"))

    def test_successful_send_starts_timeout_clock_after_delivery(self):
        delivered_at = NOW + timedelta(seconds=14)

        def delayed_delivery(message):
            self.clock.return_value = delivered_at
            return True

        self.notifier.send.side_effect = delayed_delivery
        self.run_cycle()
        position = bot.AlertState(self.state.path).shadow_position("test")
        self.assertEqual(position["entry_time"], delivered_at.isoformat())
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = True
        self.clock.return_value = delivered_at + timedelta(hours=72, seconds=-1)
        self.assertFalse(bot.send_timeout(self.cfg, position, self.state, self.notifier))
        self.assertEqual(self.notifier.send.call_count, 1)
        self.clock.return_value = delivered_at + timedelta(hours=72)
        self.assertTrue(bot.send_timeout(self.cfg, position, self.state, self.notifier))
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNone(self.state.shadow_position("test"))


class NotifySelfTestTests(unittest.TestCase):
    def run_self_test(self, result):
        with patch("sys.argv", ["bot.py", "--test-notify"]), \
             patch("bot.load_dotenv"), patch("bot.load_config", return_value={}), \
             patch("bot.configure_logging"), patch("bot.AlertState") as state, \
             patch("bot.TelegramNotifier") as notifier:
            if isinstance(result, Exception):
                notifier.return_value.send.side_effect = result
            else:
                notifier.return_value.send.return_value = result
            code = bot.main()
            state.assert_not_called()
            return code

    def test_failed_delivery_returns_failure(self):
        self.assertEqual(self.run_self_test(False), 1)

    def test_successful_delivery_returns_success(self):
        self.assertEqual(self.run_self_test(True), 0)

    def test_network_failure_returns_failure(self):
        self.assertEqual(self.run_self_test(bot.requests.Timeout("offline")), 1)


if __name__ == "__main__":
    unittest.main()
