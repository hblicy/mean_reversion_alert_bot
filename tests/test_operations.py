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


if __name__ == "__main__":
    unittest.main()
