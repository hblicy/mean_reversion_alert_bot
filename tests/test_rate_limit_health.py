import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from test_configured_pairs import CFG, NOW
from test_v2_source import listing


class RateLimitTests(unittest.TestCase):
    def test_repeated_short_429_uses_capped_exponential_backoff(self):
        session = Mock()
        session.get.return_value.status_code = 429
        session.get.return_value.json.return_value = {"retry_after_seconds": 1}
        source = bot.VariationalV2Source(session)
        for now, delay in ((100, 30), (130, 60), (190, 120), (310, 240), (550, 300), (850, 300)):
            with self.subTest(now=now), patch("bot.time.monotonic", return_value=now):
                source.begin_cycle()
                with self.assertRaises(bot.MarketDataUnavailable):
                    source.get_leg("US30-SWAP", "size_1k")
                self.assertEqual(source.retry_at, now + delay)
            with patch("bot.time.monotonic", return_value=now + delay - 1):
                source.begin_cycle()
                with self.assertRaises(bot.MarketDataUnavailable):
                    source.get_leg("US30-SWAP", "size_1k")
        self.assertEqual(session.get.call_count, 6)

    def test_server_delay_longer_than_backoff_cap_is_respected(self):
        session = Mock()
        session.get.return_value.status_code = 429
        session.get.return_value.json.return_value = {"retry_after_seconds": 900}
        source = bot.VariationalV2Source(session)
        with patch("bot.time.monotonic", return_value=100):
            with self.assertRaises(bot.MarketDataUnavailable):
                source.get_leg("US30-SWAP", "size_1k")
        with patch("bot.time.monotonic", return_value=999):
            source.begin_cycle()
            with self.assertRaises(bot.MarketDataUnavailable):
                source.get_leg("US30-SWAP", "size_1k")
        self.assertEqual(source.retry_at, 1000)
        session.get.assert_called_once()

    def test_success_resets_429_backoff_and_logs_recovery(self):
        limited = Mock(status_code=429)
        limited.json.return_value = {"retry_after_seconds": 0}
        ready = Mock(status_code=200)
        ready.json.return_value = {"listings": [listing()]}
        session = Mock()
        session.get.side_effect = [limited, limited, ready, limited]
        source = bot.VariationalV2Source(session)
        with self.assertLogs(bot.logger, level="INFO") as logs:
            for now, delay in ((100, 30), (130, 60), (190, 0), (220, 30)):
                with patch("bot.time.monotonic", return_value=now):
                    source.begin_cycle()
                    if delay:
                        with self.assertRaises(bot.MarketDataUnavailable):
                            source.get_leg("US30-SWAP", "size_1k")
                        self.assertEqual(source.retry_at, now + delay)
                    else:
                        self.assertEqual(source.get_leg("US30-SWAP", "size_1k").status, "OPEN")
                        self.assertEqual(source.failures, 0)
        self.assertTrue(any("429" in line for line in logs.output))
        self.assertTrue(any("recovered" in line for line in logs.output))

    def test_invalid_server_delay_still_backs_off(self):
        for hint in (None, "bad", -1, float("nan"), float("inf")):
            with self.subTest(hint=hint):
                session = Mock()
                session.get.return_value.status_code = 429
                session.get.return_value.json.return_value = {"retry_after_seconds": hint}
                source = bot.VariationalV2Source(session)
                with patch("bot.time.monotonic", return_value=100):
                    with self.assertRaises(bot.MarketDataUnavailable):
                        source.get_leg("US30-SWAP", "size_1k")
                self.assertEqual(source.retry_at, 130)


class RateLimitHealthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = bot.AlertState(Path(tmp.name) / "state.json")
        self.state.data["health"] = {"heartbeat_day": NOW.astimezone(bot.BEIJING).date().isoformat()}
        self.health = bot.BotHealth(self.state)
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.session = Mock()
        self.limited = Mock(status_code=429)
        self.limited.json.return_value = {"retry_after_seconds": 1}
        self.session.get.return_value = self.limited
        self.source = bot.VariationalV2Source(self.session)
        self.cfg = copy.deepcopy(CFG)
        self.make_ready(NOW)
        with patch.dict(bot.SOURCES, {"variational_v2": lambda: self.source}):
            self.cfg = bot.init_monitors({"pairs": [self.cfg]}, self.state)[0]
        self.session.get.reset_mock()
        self.session.get.return_value = self.limited

    def tick(self, seconds, *, start=NOW):
        now = start + timedelta(seconds=seconds)
        with patch("bot.utc_now", return_value=now), patch("bot.time.monotonic", return_value=100 + seconds):
            bot.run_once([self.cfg], self.state, self.notifier)
            self.health.tick([self.cfg], self.notifier)

    def make_ready(self, now):
        ready = Mock(status_code=200)
        rows = [listing(leg["ticker"]) for leg in self.cfg["legs"].values()]
        for row in rows:
            row["quotes"]["updated_at"] = now.isoformat()
        ready.json.return_value = {"listings": rows}
        self.session.get.return_value = ready

    def fail_five_requests(self):
        for seconds in range(0, 451, 30):
            self.tick(seconds)

    def test_backoff_polls_do_not_count_as_additional_failed_attempts(self):
        self.limited.json.return_value = {"retry_after_seconds": 300}
        for seconds in range(0, 151, 30):
            self.tick(seconds)
        self.session.get.assert_called_once()
        self.assertEqual(self.health.failures, 1)
        self.notifier.send.assert_not_called()

    def test_five_actual_429_attempts_alert_with_cause(self):
        self.fail_five_requests()
        self.assertEqual(self.session.get.call_count, 5)
        self.assertEqual(self.health.failures, 5)
        self.notifier.send.assert_called_once()
        text = self.notifier.send.call_args.args[0]
        self.assertIn("bot degraded", text)
        self.assertIn("429", text)
        self.assertIn("A/B", text)

    def test_scheduled_closed_market_errors_do_not_raise_degraded(self):
        closed = NOW.replace(hour=21, minute=10)
        for seconds in range(0, 451, 30):
            self.tick(seconds, start=closed)
        self.assertEqual(self.session.get.call_count, 5)
        self.assertEqual(self.health.failures, 0)
        self.assertEqual(self.health.open_without_quote, 0)
        self.notifier.send.assert_not_called()

    def test_runtime_failures_still_alert_during_scheduled_close(self):
        closed = NOW.replace(hour=21, minute=10)
        self.source.snapshot = Mock(side_effect=RuntimeError("monitor bug"))
        for seconds in range(0, 150, 30):
            with patch("bot.utc_now", return_value=closed + timedelta(seconds=seconds)):
                with self.assertLogs(bot.logger, level="ERROR"), self.assertRaises(RuntimeError):
                    bot.run_once([self.cfg], self.state, self.notifier)
                self.health.tick([self.cfg], self.notifier)
        self.notifier.send.assert_called_once()
        self.assertIn("RuntimeError", self.notifier.send.call_args.args[0])

    def test_open_quote_outage_still_alerts_during_long_backoff(self):
        self.limited.json.return_value = {"retry_after_seconds": 3600}
        self.tick(0)
        self.tick(1800)
        self.session.get.assert_called_once()
        self.assertEqual(self.health.failures, 1)
        self.notifier.send.assert_called_once()
        text = self.notifier.send.call_args.args[0]
        self.assertIn("429", text)
        self.assertIn("退避", text)

    def test_recovery_is_sent_once_after_valid_quotes_return(self):
        self.fail_five_requests()
        self.notifier.send.reset_mock()
        self.make_ready(NOW + timedelta(seconds=750))
        self.tick(750)
        self.tick(780)
        self.notifier.send.assert_called_once()
        self.assertIn("恢复", self.notifier.send.call_args.args[0])

    def test_successful_http_with_rejected_quotes_does_not_report_recovery(self):
        self.fail_five_requests()
        self.notifier.send.reset_mock()
        self.make_ready(NOW + timedelta(seconds=750))
        rows = self.session.get.return_value.json.return_value["listings"]
        rows[1]["quotes"]["updated_at"] = (NOW + timedelta(seconds=690)).isoformat()
        self.tick(750)
        self.notifier.send.assert_not_called()
        self.assertIn("REJECTED", self.cfg["_status"])

    def test_failed_recovery_delivery_is_retried(self):
        self.fail_five_requests()
        self.notifier.send.reset_mock()
        self.notifier.send.side_effect = [False, True]
        self.make_ready(NOW + timedelta(seconds=750))
        self.tick(750)
        self.tick(780)
        self.tick(810)
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertTrue(all("恢复" in call.args[0] for call in self.notifier.send.call_args_list))

    def test_regular_quotes_do_not_emit_unsolicited_recovery(self):
        self.make_ready(NOW)
        self.tick(0)
        self.tick(30)
        self.notifier.send.assert_not_called()

    def test_wall_timeout_still_closes_while_source_is_backing_off(self):
        self.cfg["max_holding_hours"] = 72
        self.state.set_shadow_position(self.cfg["id"], {
            "direction": "short_a_long_b", "ratio_side": "short_ratio", "method": "beta_spread", "beta": .5,
            "entry_time": (NOW - timedelta(hours=72) + timedelta(seconds=30)).isoformat(),
            "clock_time": NOW.isoformat(), "trading_seconds": 0})
        self.limited.json.return_value = {"retry_after_seconds": 300}
        self.tick(0)
        self.tick(30)
        self.session.get.assert_called_once()
        self.assertIsNone(self.state.shadow_position(self.cfg["id"]))
        self.notifier.send.assert_called_once()
        self.assertIn("超时结束跟踪", self.notifier.send.call_args.args[0])

    def test_main_does_not_promote_backoff_to_internal_cycle_failure(self):
        self.limited.json.return_value = {"retry_after_seconds": 300}
        elapsed = [0]

        def wait(seconds):
            elapsed[0] += seconds
            if elapsed[0] >= 180:
                raise KeyboardInterrupt

        with patch("sys.argv", ["bot.py"]), patch("bot.load_dotenv"), patch("bot.configure_logging"), \
             patch("bot.load_config", return_value={"pairs": [CFG]}), \
             patch("bot.AlertState", return_value=self.state), patch("bot.init_monitors", return_value=[self.cfg]), \
             patch("bot.TelegramNotifier", return_value=self.notifier), patch("bot.MarketHistory"), \
             patch("bot.BotHealth", return_value=self.health), \
             patch("bot.utc_now", side_effect=lambda: NOW + timedelta(seconds=elapsed[0])), \
             patch("bot.time.monotonic", side_effect=lambda: 100 + elapsed[0]), patch("bot.time.sleep", side_effect=wait):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.session.get.assert_called_once()
        self.assertEqual(self.health.failures, 1)
        self.notifier.send.assert_not_called()

    def test_archive_failure_still_alerts_with_fresh_quotes(self):
        with patch("sys.argv", ["bot.py"]), patch("bot.load_dotenv"), patch("bot.configure_logging"), \
             patch("bot.load_config", return_value={"pairs": [CFG]}), \
             patch("bot.AlertState", return_value=self.state), patch("bot.init_monitors", return_value=[self.cfg]), \
             patch("bot.TelegramNotifier", return_value=self.notifier), patch("bot.MarketHistory") as history, \
             patch("bot.BotHealth", return_value=self.health), patch("bot.run_once"), \
             patch("bot.utc_now", return_value=NOW), \
             patch("bot.time.sleep", side_effect=[None, None, None, None, KeyboardInterrupt]):
            self.cfg.update(_status="OPEN", _last_valid_quote=NOW.timestamp())
            history.return_value.flush.side_effect = OSError("disk full")
            with self.assertLogs(bot.logger, level="ERROR"), self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.notifier.send.assert_called_once()
        self.assertIn("历史存储", self.notifier.send.call_args.args[0])
        self.assertNotIn("A/B：OPEN", self.notifier.send.call_args.args[0])
        self.assertNotIn("恢复", self.notifier.send.call_args.args[0])

    def test_recovery_waits_until_an_open_session_has_valid_quotes(self):
        self.fail_five_requests()
        self.notifier.send.reset_mock()
        self.make_ready(NOW + timedelta(hours=7, minutes=10))
        self.tick(7 * 3600 + 600)
        self.notifier.send.assert_not_called()
        self.make_ready(NOW + timedelta(hours=8))
        self.tick(8 * 3600)
        self.notifier.send.assert_called_once()
        self.assertIn("恢复", self.notifier.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
