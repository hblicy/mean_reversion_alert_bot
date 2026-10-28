import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from test_configured_pairs import CFG, NOW, quote


class OperationsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = bot.AlertState(Path(tmp.name) / "state.json")
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.cfg = dict(CFG, _calendar=bot.SessionCalendar(), _calc=bot.SpreadCalculator(3),
                        _status="OPEN", _last_valid_quote=NOW.timestamp())

    def test_main_loop_survives_exception(self):
        with patch("sys.argv", ["bot.py"]), patch("bot.load_dotenv"), patch("bot.configure_logging"), \
             patch("bot.load_config", return_value={"pairs": [CFG]}), patch("bot.AlertState", return_value=self.state), \
             patch("bot.init_monitors", return_value=[self.cfg]), patch("bot.TelegramNotifier", return_value=self.notifier), \
             patch("bot.run_once", side_effect=[RuntimeError("monitor bug"), None]) as run, \
             patch("bot.time.sleep", side_effect=[None, KeyboardInterrupt]), patch("bot.utc_now", return_value=NOW):
            with self.assertLogs(bot.logger, level="ERROR"):
                with self.assertRaises(KeyboardInterrupt):
                    bot.main()
        self.assertEqual(run.call_count, 2)

    def test_degraded_after_five_failures_with_cooldown(self):
        health = bot.BotHealth(self.state)
        with patch("bot.utc_now", return_value=NOW):
            for _ in range(4):
                health.tick([self.cfg], self.notifier, cycle_failed=True)
            self.notifier.send.reset_mock()
            health.tick([self.cfg], self.notifier, cycle_failed=True)
            health.tick([self.cfg], self.notifier, cycle_failed=True)
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIn("bot degraded", self.notifier.send.call_args.args[0])

    def test_no_quote_counts_open_time_only(self):
        health = bot.BotHealth(self.state)
        with patch("bot.utc_now", return_value=NOW):
            health.tick([self.cfg], self.notifier)
        self.notifier.send.reset_mock()
        self.cfg.pop("_last_valid_quote")
        with patch("bot.utc_now", return_value=NOW + timedelta(minutes=31)):
            health.tick([self.cfg], self.notifier)
        self.assertIn("bot degraded", self.notifier.send.call_args.args[0])

    def test_heartbeat_once_per_beijing_day(self):
        health = bot.BotHealth(self.state)
        with patch("bot.utc_now", return_value=NOW.replace(hour=1)):
            health.tick([self.cfg], self.notifier)
            health.tick([self.cfg], self.notifier)
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIn("心跳", self.notifier.send.call_args.args[0])
        self.assertIn("北京时间", self.notifier.send.call_args.args[0])

    def test_shadow_never_sends_health_notifications(self):
        health = bot.BotHealth(self.state)
        self.cfg["shadow_mode"] = True
        with patch("bot.utc_now", return_value=NOW):
            for _ in range(6):
                health.tick([self.cfg], self.notifier, cycle_failed=True)
        self.notifier.send.assert_not_called()

    def test_non_json_delivery_failure_and_token_not_logged(self):
        with patch.dict("os.environ", {"TG_BOT_TOKEN": "test-secret-token", "TG_CHAT_ID": "1", "PRINT_MESSAGES": "0"}):
            notifier = bot.TelegramNotifier()
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("not JSON test-secret-token")
        with patch("bot.requests.post", return_value=response), self.assertLogs(bot.logger, level="WARNING") as logs:
            self.assertFalse(notifier.send("message"))
        self.assertNotIn("test-secret-token", " ".join(logs.output))
        response.status_code = 502
        with patch("bot.requests.post", return_value=response):
            self.assertFalse(notifier.send("message"))

    def test_state_is_fsynced_before_replace(self):
        with patch("bot.os.fsync") as sync:
            self.state.save()
        sync.assert_called_once()

    def test_log_rotation_bounds_backups_and_redacts_records(self):
        with patch("bot.ROOT", self.state.path.parent), patch("bot.logging.basicConfig") as setup:
            bot.configure_logging("INFO")
        stream, rotating = setup.call_args.kwargs["handlers"]
        self.addCleanup(stream.close)
        self.addCleanup(rotating.close)
        self.assertEqual(rotating.maxBytes, 10 * 1024 * 1024)
        self.assertEqual(rotating.backupCount, 5)
        rotating.maxBytes = 256
        with patch.dict("os.environ", {"TG_BOT_TOKEN": "test-secret-token"}):
            for number in range(30):
                record = logging.LogRecord("rotation-test", logging.INFO, __file__, 1,
                                           "record=%02d token=%s", (number, "test-secret-token"), None)
                rotating.handle(record)
        files = list((self.state.path.parent / "logs").glob("alert-bot.log*"))
        self.assertEqual(len(files), 6)
        record_bytes = len((rotating.format(record) + "\n").encode("utf-8"))
        self.assertTrue(all(path.stat().st_size <= 256 + record_bytes for path in files))
        text = "".join(path.read_text(encoding="utf-8") for path in files)
        self.assertNotIn("test-secret-token", text)
        self.assertIn("record=29", text)
        self.assertNotIn("record=00", text)

    def test_log_formatter_redacts_tokens_in_tracebacks_and_urls(self):
        formatter = bot.SecretFormatter("%(message)s")
        with patch.dict("os.environ", {"TG_BOT_TOKEN": "test-secret-token"}):
            try:
                raise RuntimeError("https://api.telegram.org/bottest-secret-token/sendMessage")
            except RuntimeError:
                import logging
                import sys
                record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
                text = formatter.format(record)
        self.assertNotIn("test-secret-token", text)
        self.assertIn("RuntimeError", text)


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = bot.AlertState.__new__(bot.AlertState)
        self.state.data = {"alerts": {}, "history": {}}
        self.state.save = Mock()
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.rows = [{"ticker": leg["ticker"], "contract_type": leg["contract_type"]}
                     for leg in CFG["legs"].values()]
        self.ready = Mock(status_code=200)
        self.ready.json.return_value = {"listings": self.rows}
        self.limited = Mock(status_code=429)
        self.limited.json.return_value = {"retry_after_seconds": 75}
        self.session = Mock()
        self.session.get.return_value = self.ready
        self.poll = Mock()
        self.monotonic = 100.0
        self.waits = []
        patches = [patch("sys.argv", ["bot.py"]), patch("bot.load_dotenv"),
                   patch("bot.configure_logging"), patch("bot.load_config", return_value={"pairs": [CFG]}),
                   patch("bot.AlertState", return_value=self.state),
                   patch("bot.TelegramNotifier", return_value=self.notifier),
                   patch("bot.requests.Session", return_value=self.session),
                   patch("bot.run_once", self.poll), patch("bot.utc_now", return_value=NOW),
                   patch("bot.time.monotonic", side_effect=lambda: self.monotonic),
                   patch("bot.time.sleep", side_effect=self.wait)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.poll.called:
            raise KeyboardInterrupt
        if len(self.waits) > 8:
            self.fail("Startup retries did not progress to polling")
        self.monotonic += seconds

    def test_startup_rate_limit_retries_then_polls_and_sends_heartbeat(self):
        position = {"direction": "long_a_short_b", "ratio_side": "long_ratio", "method": "beta_spread",
                    "beta": .5, "entry_time": NOW.isoformat(), "entry_spread": 2.3,
                    "reference_mean": 2.31, "reference_std": .01, "reference_z_close": .5,
                    "trading_seconds": 120., "clock_time": NOW.isoformat()}
        self.state.set_shadow_position("custom", position)
        self.session.get.side_effect = [self.limited, self.ready]
        with self.assertLogs(bot.logger, level="WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.assertEqual(self.waits, [75, 30])
        self.assertEqual(self.session.get.call_count, 2)
        self.poll.assert_called_once()
        self.assertIn("每日心跳", self.notifier.send.call_args.args[0])
        self.assertEqual(self.state.shadow_position("custom"), position)
        self.assertTrue(any("rate limited" in line for line in logs.output))

    def test_startup_network_failures_preserve_exponential_backoff(self):
        unavailable = Mock(status_code=503)
        unavailable.raise_for_status.side_effect = bot.requests.HTTPError("HTTP 503")
        self.session.get.side_effect = [bot.requests.Timeout("offline"), unavailable,
                                        bot.requests.Timeout("offline"), unavailable,
                                        bot.requests.Timeout("offline"), self.ready]
        with self.assertLogs(bot.logger, level="WARNING"):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.assertEqual(self.waits, [30, 60, 120, 240, 300, 30])
        self.assertEqual(self.session.get.call_count, 6)
        self.poll.assert_called_once()
        source = self.poll.call_args.args[0][0]["_source"]
        self.assertEqual(source.failures, 0)

    def test_startup_zero_retry_delay_waits_before_retrying(self):
        self.limited.json.return_value = {"retry_after_seconds": 0}
        self.session.get.side_effect = [self.limited, self.ready]
        with self.assertLogs(bot.logger, level="WARNING"):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.assertEqual(self.waits, [1, 30])
        self.assertEqual(self.session.get.call_count, 2)

    def test_startup_unknown_ticker_still_fails_without_retry(self):
        self.rows.pop()
        with self.assertLogs(bot.logger, level="ERROR") as logs:
            self.assertEqual(bot.main(), 1)
        self.assertTrue(any("not found" in line for line in logs.output))
        self.assertEqual(self.waits, [])
        self.assertEqual(self.session.get.call_count, 1)
        self.poll.assert_not_called()

    def test_startup_wrong_contract_still_fails_without_retry(self):
        self.rows[0]["contract_type"] = "perpetual"
        with self.assertLogs(bot.logger, level="ERROR") as logs:
            self.assertEqual(bot.main(), 1)
        self.assertTrue(any("contract_type" in line for line in logs.output))
        self.assertEqual(self.waits, [])
        self.assertEqual(self.session.get.call_count, 1)
        self.poll.assert_not_called()

    def test_startup_once_keeps_single_attempt_on_rate_limit(self):
        self.session.get.return_value = self.limited
        with patch("sys.argv", ["bot.py", "--once"]), self.assertLogs(bot.logger, level="ERROR"):
            self.assertEqual(bot.main(), 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.session.get.call_count, 1)
        self.poll.assert_not_called()

    def test_startup_unexpected_error_reaches_caller(self):
        self.session.get.side_effect = RuntimeError("unexpected adapter failure")
        with self.assertRaisesRegex(RuntimeError, "unexpected adapter failure"):
            bot.main()
        self.assertEqual(self.waits, [])
        self.poll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
