import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from market_history import HistoryWriteError, MarketHistory
from test_configured_pairs import CFG, NOW, quote
from test_v2_source import listing


class MarketHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "market_history.sqlite3"
        self.history = MarketHistory(self.path)

    def capture(self, now=NOW, sampled=True, cfg=None, snap=None):
        record = self.history.begin(cfg or CFG, now)
        self.history.attach_snapshot(record, snap or quote(now=now))
        record.update(status="valid", bucket=int(now.timestamp() // 600))
        if sampled:
            record["sample"] = {"bucket": record["bucket"], "ts": now.timestamp(), "a_mid": 100., "b_mid": 100.}
        return record

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            return [json.loads(row[0]) for row in connection.execute(f"SELECT payload FROM {table} ORDER BY started_at")]

    def test_quotes_funding_and_receipt_times_survive_restart(self):
        snap = quote()
        snap.metadata["assets"]["A"].update(funding={"annualized_long_rate": .05, "annualized_short_rate": .03},
                                             funding_raw={"last": {"annualized_long_rate": ".04"},
                                                          "next": {"annualized_long_rate": ".05"},
                                                          "next_timestamp": 1790000000}, mark_price=100.1)
        self.capture(snap=snap)
        self.assertFalse(self.path.exists())
        self.history.flush()
        self.history = MarketHistory(self.path)
        self.capture(now=NOW + timedelta(seconds=30), sampled=False)
        self.history.flush()
        rows = self.rows("observations")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.rows("samples")), 1)
        first = rows[0]
        self.assertEqual(first["poll_started_at"], NOW.isoformat())
        self.assertEqual(first["snapshot"]["received_at"], NOW.isoformat())
        asset = first["snapshot"]["assets"]["A"]
        self.assertEqual((asset["bid"], asset["ask"], asset["mark_price"]), (99.99, 100.01, 100.1))
        self.assertEqual(asset["quote_updated_at"], NOW.isoformat())
        self.assertEqual(asset["funding"]["annualized_short_rate"], .03)
        self.assertEqual(asset["funding_raw"]["next_timestamp"], 1790000000)
        self.assertEqual(first["settings"]["beta"], CFG["beta"])

    def test_duplicate_retries_do_not_overwrite_quotes_or_samples(self):
        self.capture()
        self.history.flush()
        self.capture(snap=quote(999))
        self.history.flush()
        self.capture(now=NOW + timedelta(seconds=30), snap=quote(102))
        self.history.flush()
        self.assertEqual(len(self.rows("observations")), 2)
        samples = self.rows("samples")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["snapshot"]["base_price"], 100)
        self.assertEqual(self.rows("observations")[0]["snapshot"]["base_price"], 100)

    def test_later_dates_append_without_pruning_earlier_history(self):
        self.capture()
        self.history.flush()
        self.capture(now=NOW + timedelta(days=40))
        self.history.flush()
        self.assertEqual(len(self.rows("samples")), 2)
        self.assertEqual(self.rows("samples")[0]["poll_started_at"], NOW.isoformat())

    def test_different_market_series_do_not_mix_buckets(self):
        self.capture()
        for key, value in (("quote_size", "size_10k"), ("sample_interval_sec", 1200)):
            self.capture(cfg=dict(CFG, **{key: value}))
        cfg = copy.deepcopy(CFG)
        cfg["legs"]["a"]["ticker"] = "OTHER-SWAP"
        self.capture(cfg=cfg)
        self.history.flush()
        self.assertEqual(len(self.rows("samples")), 4)

    def test_only_market_settings_are_archived(self):
        cfg = copy.deepcopy(CFG)
        cfg.update(api_key="private-setting", _source=Mock())
        snap = quote()
        snap.metadata["assets"]["A"]["access_token"] = "private-metadata"
        self.capture(cfg=cfg, snap=snap)
        cfg["beta"]["value"] = .7
        self.history.flush()
        payload = json.dumps(self.rows("observations"))
        self.assertNotIn("private-setting", payload)
        self.assertNotIn("private-metadata", payload)
        self.assertNotIn("_source", self.rows("observations")[0]["settings"])
        self.assertEqual(self.rows("observations")[0]["settings"]["beta"]["value"], .5)

    def test_write_failure_retains_batch_and_context_for_retry(self):
        blocker = Path(self.tmp.name) / "blocked"
        blocker.write_text("occupied", encoding="utf-8")
        self.path = blocker / "market_history.sqlite3"
        self.history = MarketHistory(self.path)
        self.capture()
        with self.assertRaises(HistoryWriteError) as failed:
            self.history.flush()
        self.assertIsInstance(failed.exception.__cause__, OSError)
        self.assertIn(str(self.path), str(failed.exception))
        self.assertEqual(self.history.pending_count, 1)
        blocker.unlink()
        self.history.flush()
        self.assertEqual(self.history.pending_count, 0)
        self.assertEqual(len(self.rows("samples")), 1)

    def test_partial_transaction_rolls_back_and_retries_without_duplicates(self):
        self.capture()
        self.history.flush()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TRIGGER stop_sample BEFORE INSERT ON samples BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        self.capture(now=NOW + timedelta(minutes=10))
        with self.assertRaises(HistoryWriteError) as failed:
            self.history.flush()
        self.assertIsInstance(failed.exception.__cause__, sqlite3.Error)
        self.assertEqual(len(self.rows("observations")), 1)
        self.assertEqual(self.history.pending_count, 1)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER stop_sample")
        self.history.flush()
        self.assertEqual(len(self.rows("observations")), 2)
        self.assertEqual(len(self.rows("samples")), 2)

    def test_corrupt_archive_is_not_reset(self):
        original = b"not a SQLite database"
        self.path.write_bytes(original)
        self.capture()
        with self.assertRaises(HistoryWriteError):
            self.history.flush()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.history.pending_count, 1)


class MarketHistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = bot.AlertState(Path(self.tmp.name) / "state.json")
        self.history = MarketHistory(Path(self.tmp.name) / "market_history.sqlite3")
        self.source = Mock()
        self.source.snapshot.return_value = quote()
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.clock = patch("bot.utc_now", return_value=NOW).start()
        patch("bot.time.time", side_effect=lambda: self.clock.return_value.timestamp()).start()
        patch.dict(bot.SOURCES, {"variational_v2": lambda: self.source}).start()
        self.addCleanup(patch.stopall)
        self.cfg = bot.init_monitors({"pairs": [copy.deepcopy(CFG)]}, self.state)[0]
        self.cfg["_market_history"] = self.history

    def cycle(self, a=100, now=NOW):
        self.clock.return_value = now
        self.source.snapshot.return_value = quote(a, now=now)
        try:
            bot.run_once([self.cfg], self.state, self.notifier)
        finally:
            self.history.flush()

    def rows(self, table):
        with closing(sqlite3.connect(self.history.path)) as connection:
            return [json.loads(r[0]) for r in connection.execute(f"SELECT payload FROM {table} ORDER BY started_at")]

    def test_archive_outlives_window_eviction_and_restart(self):
        self.cfg["shadow_mode"] = True
        for i in range(16):
            self.cycle(100+i, NOW + timedelta(minutes=10*i))
        self.assertLess(len(self.state.history("custom")), 16)
        self.assertEqual(len(self.rows("samples")), 16)
        self.state = bot.AlertState(self.state.path)
        self.cfg = bot.init_monitors({"pairs": [copy.deepcopy(CFG)]}, self.state)[0]
        self.cfg.update(_market_history=self.history, shadow_mode=True)
        self.cycle(116, NOW + timedelta(minutes=160))
        self.assertEqual(len(self.rows("samples")), 17)
        self.assertEqual(self.rows("samples")[0]["sample"]["a_mid"], 100)

    def test_intrabucket_quotes_and_rejections_are_distinct_from_samples(self):
        self.cycle()
        self.cycle(101, NOW + timedelta(seconds=30))
        self.clock.return_value = NOW + timedelta(seconds=121)
        self.source.snapshot.return_value = quote(now=NOW)
        bot.run_once([self.cfg], self.state, self.notifier)
        self.history.flush()
        rows = self.rows("observations")
        self.assertEqual([r["status"] for r in rows], ["valid", "valid", "rejected"])
        self.assertIn("stale/future", rows[-1]["reason"])
        self.assertEqual(len(self.rows("samples")), 1)
        self.assertEqual(rows[1]["snapshot"]["base_price"], 101)
        self.clock.return_value = NOW + timedelta(seconds=122)
        self.source.snapshot.return_value = quote(now=NOW + timedelta(seconds=29))
        bot.run_once([self.cfg], self.state, self.notifier)
        self.history.flush()
        self.assertEqual(self.rows("observations")[-1]["reason"], "timestamp regression")
        self.assertEqual(len(self.rows("samples")), 1)

    def test_flat_quotes_remain_observations_without_entering_samples(self):
        self.cfg["flat_buckets"] = 3
        for i in range(3):
            self.cycle(100, NOW + timedelta(minutes=i*10))
        self.assertEqual([r["status"] for r in self.rows("observations")], ["valid", "valid", "stale"])
        self.assertEqual(len(self.rows("samples")), 2)

    def test_outages_are_stored_without_fabricating_prices(self):
        for i, error in enumerate((bot.MarketDataError("CLOSED_OR_NO_QUOTE"),
                                   bot.MarketDataUnavailable("retry in 30s"))):
            self.clock.return_value = NOW + timedelta(seconds=i*30)
            self.source.snapshot.side_effect = error
            bot.run_once([self.cfg], self.state, self.notifier)
        self.history.flush()
        rows = self.rows("observations")
        self.assertEqual([r["status"] for r in rows], ["closed_or_no_quote", "unavailable"])
        self.assertTrue(all(r["snapshot"] is None for r in rows))
        self.assertEqual(self.rows("samples"), [])

    def test_no_quote_timer_exit_is_not_recorded_as_a_market_outage(self):
        self.cfg["max_holding_hours"] = 72
        self.state.set_shadow_position("custom", {"direction": "short_a_long_b", "ratio_side": "short_ratio",
            "beta": .5, "entry_time": (NOW-timedelta(hours=73)).isoformat(),
            "clock_time": NOW.isoformat(), "trading_seconds": 0})
        self.cycle()
        self.source.snapshot.assert_not_called()
        record = self.rows("observations")[0]
        self.assertEqual(record["status"], "not_requested")
        self.assertEqual(record["reason"], "natural_timeout")
        self.assertIsNone(record["snapshot"])

    def test_archiving_does_not_change_entry_close_messages_or_state(self):
        outcomes = []
        for archive in (False, True):
            self.state = bot.AlertState(Path(self.tmp.name) / f"state-{archive}.json")
            self.cfg = bot.init_monitors({"pairs": [copy.deepcopy(CFG)]}, self.state)[0]
            if archive:
                self.cfg["_market_history"] = self.history
            self.notifier.send.reset_mock()
            for i, a in enumerate((100, 102, 101, 110, 100.5)):
                self.cycle(a, NOW + timedelta(minutes=i*10))
            outcomes.append((copy.deepcopy(self.state.data), [c.args[0] for c in self.notifier.send.call_args_list]))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(len(outcomes[0][1]), 2)
        self.assertIn("[ENTRY]", outcomes[0][1][0])
        self.assertIn("[CLOSE]", outcomes[0][1][1])

    def test_main_flushes_captured_data_even_if_monitor_cycle_fails(self):
        def fail_after_capture(monitors, *_args, **_kwargs):
            record = monitors[0]["_market_history"].begin(CFG, NOW)
            record.update(status="unavailable", reason="test outage")
            raise RuntimeError("cycle failed")
        with patch("sys.argv", ["bot.py", "--once"]), patch("bot.load_dotenv"), \
             patch("bot.configure_logging"), patch("bot.load_config", return_value={"pairs": [CFG], "state_file": str(self.state.path)}), \
             patch("bot.AlertState", return_value=self.state), patch("bot.init_monitors", return_value=[self.cfg]), \
             patch("bot.TelegramNotifier", return_value=self.notifier), patch("bot.run_once", side_effect=fail_after_capture), \
             self.assertLogs(bot.logger, level="ERROR"):
            self.assertEqual(bot.main(), 1)
        self.assertEqual(self.rows("observations")[0]["reason"], "test outage")

    def test_normal_startup_automatically_appends_across_process_runs(self):
        config = {"pairs": [copy.deepcopy(CFG)], "state_file": str(self.state.path)}
        with patch("sys.argv", ["bot.py", "--once"]), patch("bot.load_dotenv"), \
             patch("bot.configure_logging"), patch("bot.load_config", return_value=config), \
             patch("bot.TelegramNotifier", return_value=self.notifier):
            for i, seconds in enumerate((0, 30, 600)):
                now = NOW + timedelta(seconds=seconds)
                self.clock.return_value = now
                self.source.snapshot.return_value = quote(100+i, now=now)
                self.assertEqual(bot.main(), 0)
        self.assertEqual(self.source.snapshot.call_count, 3)
        self.assertEqual(len(self.rows("observations")), 3)
        self.assertEqual(len(self.rows("samples")), 2)
        self.notifier.send.assert_not_called()

    def test_archive_failure_is_reported_after_signal_processing(self):
        events = []
        def process_signals(*_args, **_kwargs):
            events.append("signals")
        def fail_flush(*_args):
            events.append("archive")
            raise HistoryWriteError("archive unavailable")
        with patch("sys.argv", ["bot.py", "--once"]), patch("bot.load_dotenv"), \
             patch("bot.configure_logging"), patch("bot.load_config", return_value={"pairs": [CFG], "state_file": str(self.state.path)}), \
             patch("bot.AlertState", return_value=self.state), patch("bot.init_monitors", return_value=[self.cfg]), \
             patch("bot.TelegramNotifier", return_value=self.notifier), patch("bot.run_once", side_effect=process_signals), \
             patch.object(MarketHistory, "flush", side_effect=fail_flush), self.assertLogs(bot.logger, level="ERROR") as logs:
            self.assertEqual(bot.main(), 1)
        self.assertEqual(events, ["signals", "archive"])
        self.assertTrue(any("archive unavailable" in line for line in logs.output))


class MarketHistorySourceTests(unittest.TestCase):
    def test_snapshot_keeps_funding_envelope_and_mark_without_extra_requests(self):
        raw = {"last": {"annualized_long_rate": ".03", "annualized_short_rate": ".01"},
               "next": {"annualized_long_rate": ".05", "annualized_short_rate": ".03"},
               "next_timestamp": 1790000000}
        rows = [listing("NEW-A-SWAP"), listing("NEW-B-SWAP")]
        rows[0]["funding"] = raw
        session = Mock()
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.json.return_value = {"listings": rows}
        snap = bot.VariationalV2Source(session).snapshot("custom", CFG)
        self.assertEqual(snap.metadata["assets"]["A"]["funding_raw"], raw)
        self.assertEqual(snap.metadata["assets"]["A"]["mark_price"], 100)
        self.assertEqual(snap.metadata["assets"]["A"]["funding"]["annualized_long_rate"], .05)
        self.assertEqual(session.get.call_count, 1)
