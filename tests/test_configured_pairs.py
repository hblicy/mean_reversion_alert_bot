import copy
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from sessions import SessionCalendar


NOW = datetime(2026, 10, 22, 14, tzinfo=timezone.utc)
CFG = {"id": "custom", "label": "A/B", "enabled": True, "source": "variational_v2",
       "quote_size": "size_1k", "legs": {
           "a": {"ticker": "NEW-A-SWAP", "name": "A", "contract_type": "swap", "session": "us_index_swap"},
           "b": {"ticker": "NEW-B-SWAP", "name": "B", "contract_type": "swap", "session": "us_index_swap"}},
       "method": "beta_spread", "beta": {"mode": "fixed", "value": .5, "rolling_buckets": 5, "min": .3, "max": 1},
       "sample_interval_sec": 600, "window_buckets": 3, "z_open": 2, "z_close": .5,
       "entry_max_z": 0, "entry_require_cross": False, "allowed_sides": ["long_a_short_b", "short_a_long_b"],
       "max_open_gap_buckets": 3, "reopen_grace_buckets": 2, "flat_buckets": 0,
       "max_holding_trading_hours": 46, "max_quote_age_sec": 120, "max_quote_skew_sec": 30,
       "shadow_mode": False, "cooldown_sec": 0}


def quote(a=100, b=100, now=NOW):
    return bot.PriceSnapshot("A", "B", a, b, a / b, "variational_v2", now,
        {"quote_size": "size_1k", "assets": {name: {
            "bid": value - .01, "ask": value + .01, "price_source": "quote", "quote_updated_at": now.isoformat(),
            "funding": None, "base_spread_bps": .1} for name, value in (("A", a), ("B", b))}})


class ConfiguredPairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = bot.AlertState(Path(self.tmp.name) / "state.json")
        self.source = Mock()
        self.source.snapshot.return_value = quote()
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.clock = patch("bot.utc_now", return_value=NOW).start()
        self.addCleanup(patch.stopall)
        self.registry = patch.dict(bot.SOURCES, {"variational_v2": lambda: self.source})
        self.registry.start()
        self.cfg = self.init(CFG)

    def init(self, cfg):
        return bot.init_monitors({"pairs": [copy.deepcopy(cfg)]}, self.state)[0]

    def cycle(self, a=100, b=100, now=None):
        if now:
            self.clock.return_value = now
        self.source.snapshot.return_value = quote(a, b, self.clock.return_value)
        bot.run_once([self.cfg], self.state, self.notifier)

    def warm(self):
        for i, a in enumerate((100, 102, 101)):
            self.cycle(a, now=NOW + timedelta(minutes=i * 10))

    def test_generic_pair_enters_and_direction_is_correct(self):
        self.warm()
        self.cycle(110, now=NOW + timedelta(minutes=30))
        self.assertEqual(self.state.shadow_position("custom")["direction"], "short_a_long_b")
        self.assertIn("空 A / 多 B", self.notifier.send.call_args.args[0])
        self.assertIn("北京时间", self.notifier.send.call_args.args[0])

    def test_allowed_side_and_shadow_mode(self):
        self.cfg["allowed_sides"] = ["long_a_short_b"]
        self.warm()
        self.cycle(110, now=NOW + timedelta(minutes=30))
        self.notifier.send.assert_not_called()
        self.cfg["allowed_sides"].append("short_a_long_b")
        self.cfg["shadow_mode"] = True
        with self.assertLogs(bot.logger, level="INFO") as logs:
            self.cycle(130, now=NOW + timedelta(minutes=40))
        self.assertTrue(any("SHADOW" in line for line in logs.output))
        self.notifier.send.assert_not_called()
        self.assertIsNone(self.state.shadow_position("custom"))

    def test_open_gap_40_minutes_resets(self):
        self.warm()
        self.cycle(103, now=NOW + timedelta(minutes=60))
        self.assertEqual(self.cfg["_calc"].count, 1)

    def test_daily_break_preserves_window_and_graces_two_buckets(self):
        start = datetime(2026, 10, 22, 20, 30, tzinfo=timezone.utc)
        for i in range(3):
            self.cycle(100 + i, now=start + timedelta(minutes=10 * i))
        self.clock.return_value = start + timedelta(minutes=30)
        self.source.snapshot.side_effect = bot.MarketDataError("CLOSED_OR_NO_QUOTE")
        bot.run_once([self.cfg], self.state, self.notifier)
        self.source.snapshot.side_effect = None
        self.cycle(120, now=start + timedelta(minutes=90))
        self.cycle(140, now=start + timedelta(minutes=100))
        self.notifier.send.assert_not_called()
        self.assertEqual(self.cfg["_calc"].count, 3)
        self.cycle(200, now=start + timedelta(minutes=110))
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_flat_leg_is_stale_without_window_reset(self):
        self.cfg["flat_buckets"] = 3
        for i in range(8):
            self.cycle(100, 100 + i, NOW + timedelta(minutes=10 * i))
        self.assertEqual(self.cfg["_calc"].count, 2)
        self.assertEqual(self.cfg["_status"], "STALE")
        self.notifier.send.assert_not_called()
        self.cycle(101, 110, NOW + timedelta(minutes=80))
        self.assertEqual(self.cfg["_calc"].count, 3)

    def test_frozen_method_beta_reference_survives_config_change_and_restart(self):
        self.warm()
        self.cycle(110, now=NOW + timedelta(minutes=30))
        entry = self.state.shadow_position("custom")
        self.assertEqual(entry["beta"], .5)
        self.state = bot.AlertState(self.state.path)
        changed = copy.deepcopy(CFG)
        changed["beta"]["value"] = .8
        changed["method"] = "log_ratio"
        changed["z_close"] = 0
        self.cfg = self.init(changed)
        self.cycle(100.5, now=NOW + timedelta(minutes=40))
        self.assertIsNone(self.state.shadow_position("custom"))
        self.assertIn("固定", self.notifier.send.call_args.args[0])

    def test_failed_entry_retries_and_preserves_original_reference(self):
        self.warm()
        self.notifier.send.side_effect = [False, True]
        self.cycle(110, now=NOW + timedelta(minutes=30))
        self.assertIsNone(self.state.shadow_position("custom"))
        self.state = bot.AlertState(self.state.path)
        self.cfg = self.init(CFG)
        self.cycle(109.8, now=NOW + timedelta(minutes=30, seconds=30))
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIsNotNone(self.state.shadow_position("custom"))
        self.assertEqual(len(self.state.history("custom")), 4)

    def test_timeout_due_at_close_is_deferred_once(self):
        start = datetime(2026, 10, 22, 20, tzinfo=timezone.utc)
        self.cfg["max_holding_trading_hours"] = 1
        self.state.set_shadow_position("custom", {"direction": "short_a_long_b", "ratio_side": "short_ratio",
            "entry_time": start.isoformat(), "method": "beta_spread", "beta": .5,
            "entry_spread": 3, "entry_ratio": 1, "reference_mean": 2, "reference_std": .1, "reference_z_close": .5})
        self.clock.return_value = start + timedelta(hours=1)
        self.source.snapshot.side_effect = bot.MarketDataError("CLOSED_OR_NO_QUOTE")
        bot.run_once([self.cfg], self.state, self.notifier)
        self.notifier.send.assert_not_called()
        self.source.snapshot.side_effect = None
        self.cycle(100, now=start + timedelta(hours=2))
        self.assertIn("休市期间到期，开市后补发", self.notifier.send.call_args.args[0])
        self.assertIsNone(self.state.shadow_position("custom"))
        self.cycle(101, now=start + timedelta(hours=2, seconds=30))
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_three_day_simulation_warms_and_survives_daily_break(self):
        self.cfg["window_buckets"] = 138
        self.cfg["shadow_mode"] = True
        self.cfg["_calc"] = bot.SpreadCalculator(138, method="beta_spread", beta=CFG["beta"])
        start = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
        seen_ready = False
        with self.assertLogs(bot.logger, level="INFO") as logs, patch.object(self.state, "save"):
            for i in range(3 * 144):
                now = start + timedelta(minutes=10 * i)
                self.clock.return_value = now
                if self.cfg["_calendar"].pair_is_open(self.cfg["legs"], now):
                    self.source.snapshot.side_effect = None
                    self.source.snapshot.return_value = quote(100 + math.sin(i) + i * .01 + (20 if i == 150 else 0), 100 + i * .01, now)
                else:
                    self.source.snapshot.side_effect = bot.MarketDataError("CLOSED_OR_NO_QUOTE")
                bot.run_once([self.cfg], self.state, self.notifier)
                seen_ready |= self.cfg["_calc"].count == 138
        self.assertTrue(seen_ready)
        self.assertFalse(any("reset" in line for line in logs.output))
        self.assertTrue(any("SHADOW" in line for line in logs.output))
        self.notifier.send.assert_not_called()

    def test_weekend_preserves_window_and_reopen_grace(self):
        start = datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc)
        for i in range(3):
            self.cycle(100 + i, now=start + timedelta(minutes=10 * i))
        reopen = datetime(2026, 10, 25, 22, tzinfo=timezone.utc)
        self.cycle(120, now=reopen)
        self.cycle(140, now=reopen + timedelta(minutes=10))
        self.notifier.send.assert_not_called()
        self.cycle(200, now=reopen + timedelta(minutes=20))
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.cfg["_calc"].count, 3)

    def test_new_path_rejects_stale_skew_and_regression(self):
        self.cycle()
        for seconds, skew in ((121, 0), (0, 31), (-10, 0)):
            self.clock.return_value = NOW + timedelta(seconds=seconds)
            snap = quote(now=NOW)
            snap.metadata["assets"]["B"]["quote_updated_at"] = (NOW - timedelta(seconds=skew)).isoformat()
            self.source.snapshot.return_value = snap
            bot.run_once([self.cfg], self.state, self.notifier)
        self.assertEqual(self.cfg["_calc"].count, 1)
        self.notifier.send.assert_not_called()

    def test_legacy_ratio_state_can_close_in_new_schema(self):
        legacy_cfg = copy.deepcopy(CFG)
        legacy_cfg["method"] = "ratio"
        self.state.set_history("custom", [.99, 1, 1.01])
        self.state.set_shadow_position("custom", {
            "direction": "SHORT_BZ_LONG_CL", "ratio_side": "short_ratio", "entry_ratio": 1.1,
            "entry_time": NOW.isoformat(), "reference_mean": 1, "reference_std": .01, "reference_z_close": .5})
        self.cfg = self.init(legacy_cfg)
        self.assertEqual(self.cfg["_calc"].count, 3)
        self.cycle(100)
        self.assertIsNone(self.state.shadow_position("custom"))

    def test_holding_clock_pauses_during_observed_no_quote(self):
        self.warm()
        self.cycle(110, now=NOW + timedelta(minutes=30))
        self.cfg["max_holding_trading_hours"] = 1
        self.clock.return_value = NOW + timedelta(minutes=40)
        self.source.snapshot.side_effect = bot.MarketDataError("CLOSED_OR_NO_QUOTE")
        bot.run_once([self.cfg], self.state, self.notifier)
        self.clock.return_value = NOW + timedelta(minutes=100)
        bot.run_once([self.cfg], self.state, self.notifier)
        self.source.snapshot.side_effect = None
        self.cycle(110, now=NOW + timedelta(minutes=110))
        self.assertIsNotNone(self.state.shadow_position("custom"))
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_beta_change_recomputes_history_and_discards_previous_z(self):
        self.warm()
        self.cfg["shadow_mode"] = True
        self.cycle(110, now=NOW + timedelta(minutes=30))
        self.state = bot.AlertState(self.state.path)
        cfg = copy.deepcopy(CFG)
        cfg["beta"]["value"] = .8
        self.cfg = self.init(cfg)
        self.assertEqual(len(self.cfg["_calc"].history), 4)
        self.assertIsNone(self.cfg["_prev_z"])

    def test_monitor_exception_does_not_stop_next_monitor(self):
        other_cfg = copy.deepcopy(CFG)
        other_cfg["id"] = "other"
        other = self.init(other_cfg)
        broken = Mock()
        broken.snapshot.side_effect = RuntimeError("bad monitor")
        self.cfg["_source"] = broken
        with self.assertLogs(bot.logger, level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "1 monitor"):
                bot.run_once([self.cfg, other], self.state, self.notifier)
        self.assertEqual(other["_calc"].count, 1)

    def test_statistics_restart_keeps_prior_z_and_volatility(self):
        self.cfg["shadow_mode"] = True
        self.warm()
        self.cycle(110, now=NOW + timedelta(minutes=30))
        previous = self.cfg["_prev_z"]
        z_history = list(self.cfg["_calc"].z_history)
        self.state = bot.AlertState(self.state.path)
        self.cfg = self.init(CFG)
        self.assertEqual(self.cfg["_prev_z"], previous)
        self.assertEqual(list(self.cfg["_calc"].z_history), z_history)

    def test_both_generic_labels_and_observe_banner(self):
        self.cfg["tier"] = "observe"
        for z, label in ((3, "空 A / 多 B"), (-3, "多 A / 空 B")):
            signal = bot.build_signal(self.cfg, z, z * 1.1, None)
            signal.details.update(method="beta_spread", beta=.5, spread=2,
                                  reference_mean=1.9, reference_std=.1)
            message = bot.format_pair_message(self.cfg, quote(), z, signal)
            self.assertIn(label, message)
            self.assertIn("观察级·弱相关", message)

    def test_changed_interval_restarts_sampling_with_new_bucket_scale(self):
        self.warm()
        self.state = bot.AlertState(self.state.path)
        changed = dict(CFG, sample_interval_sec=1200)
        self.cfg = self.init(changed)
        self.cycle(103, now=NOW + timedelta(minutes=40))
        self.assertEqual(self.cfg["_calc"].count, 1)

    def test_closed_quote_summary_is_throttled(self):
        self.source.snapshot.side_effect = bot.MarketDataError("CLOSED_OR_NO_QUOTE")
        with self.assertLogs(bot.logger, level="INFO") as logs:
            for i in range(20):
                self.clock.return_value = NOW + timedelta(seconds=30 * i)
                bot.run_once([self.cfg], self.state, self.notifier)
        self.assertEqual(len([line for line in logs.output if "CLOSED_OR_NO_QUOTE" in line]), 1)

    def test_enabled_legacy_pair_still_times_out_when_api_is_offline(self):
        cfg = copy.deepcopy(CFG)
        for leg in cfg["legs"].values():
            leg.update(session="always", contract_type="perpetual")
            leg["ticker"] = leg["ticker"].removesuffix("-SWAP") + "-PERP"
        cfg.update(method="ratio", max_holding_hours=72)
        self.cfg = self.init(cfg)
        self.state.set_shadow_position("custom", {"direction": "short_a_long_b", "ratio_side": "short_ratio",
            "entry_ratio": 1.1, "entry_time": (NOW - timedelta(hours=73)).isoformat()})
        self.source.snapshot.side_effect = bot.MarketDataError("offline")
        bot.run_once([self.cfg], self.state, self.notifier)
        self.assertIsNone(self.state.shadow_position("custom"))
        self.source.snapshot.assert_not_called()

    def test_same_bucket_update_closes_on_risk_using_prior_window(self):
        self.cfg = self.init(dict(CFG, method="ratio", entry_max_z=4))
        self.cfg["_calc"] = bot.SpreadCalculator(3, method="ratio", history=[.99, 1., 1.01])
        self.state.set_shadow_position("custom", {
            "direction": "short_a_long_b", "ratio_side": "short_ratio", "method": "ratio", "beta": 1.,
            "entry_ratio": 1.02, "entry_spread": 1.02, "entry_time": NOW.isoformat(),
            "reference_mean": 1., "reference_std": .01, "reference_z_close": .5})
        self.cycle(103)
        self.notifier.send.assert_not_called()
        self.cycle(105, now=NOW + timedelta(seconds=30))
        self.assertIsNone(self.state.shadow_position("custom"))
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIn("风险提醒", self.notifier.send.call_args.args[0])
        self.assertIn("+5.000", self.notifier.send.call_args.args[0])

    def test_same_bucket_risk_reference_survives_restart(self):
        cfg = dict(CFG, method="ratio", entry_max_z=4)
        self.cfg = self.init(cfg)
        self.cfg["_calc"] = bot.SpreadCalculator(3, method="ratio", history=[.99, 1., 1.01])
        self.state.set_shadow_position("custom", {
            "direction": "short_a_long_b", "ratio_side": "short_ratio", "method": "ratio", "beta": 1.,
            "entry_ratio": 1.02, "entry_spread": 1.02, "entry_time": NOW.isoformat(),
            "reference_mean": 1., "reference_std": .01, "reference_z_close": .5})
        self.cycle(103)
        self.state = bot.AlertState(self.state.path)
        self.cfg = self.init(cfg)
        self.cycle(105, now=NOW + timedelta(seconds=30))
        self.assertIsNone(self.state.shadow_position("custom"))
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_restart_discards_persisted_session_override(self):
        sunday = datetime(2026, 10, 25, 22, tzinfo=timezone.utc)
        tuesday = datetime(2026, 10, 27, 20, tzinfo=timezone.utc)
        for was_open in (True, False):
            with self.subTest(was_open=was_open):
                self.state.set_shadow_position("custom", {
                    "direction": "short_a_long_b", "ratio_side": "short_ratio", "method": "beta_spread", "beta": .5,
                    "entry_ratio": 1., "entry_spread": math.log(100) * .5, "entry_time": sunday.isoformat(),
                    "clock_time": sunday.isoformat(), "trading_seconds": 0.,
                    "clock_open": was_open, "clock_override": True})
                self.state.save()
                self.state = bot.AlertState(self.state.path)
                self.cfg = self.init(CFG)
                self.cycle(100, now=tuesday)
                position = self.state.shadow_position("custom")
                self.assertIsNotNone(position)
                self.assertEqual(position["trading_seconds"], 39 * 3600)
        self.notifier.send.assert_not_called()

    def test_live_session_override_counts_only_observed_short_intervals(self):
        sunday = datetime(2026, 10, 25, 22, tzinfo=timezone.utc)
        self.state.set_shadow_position("custom", {
            "direction": "short_a_long_b", "ratio_side": "short_ratio", "method": "beta_spread", "beta": .5,
            "entry_ratio": 1., "entry_spread": math.log(100) * .5, "entry_time": sunday.isoformat()})
        self.cycle(100, now=sunday)
        self.cycle(100, now=sunday + timedelta(seconds=30))
        self.assertEqual(self.state.shadow_position("custom")["trading_seconds"], 30)
        self.cycle(100, now=datetime(2026, 10, 27, 20, tzinfo=timezone.utc))
        self.assertIsNotNone(self.state.shadow_position("custom"))
        self.assertEqual(self.state.shadow_position("custom")["trading_seconds"], 39 * 3600 + 30)

    def test_legacy_close_cooldown_blocks_generic_entry(self):
        cfg = dict(CFG, method="ratio", cooldown_sec=1800)
        self.cfg = self.init(cfg)
        self.cfg["_calc"] = bot.SpreadCalculator(3, method="ratio", history=[.99, 1., 1.01])
        self.state.set_shadow_position("custom", {
            "direction": "SHORT_BZ_LONG_CL", "ratio_side": "short_ratio", "entry_ratio": 1.1,
            "entry_time": NOW.isoformat(), "reference_mean": 1., "reference_std": .01, "reference_z_close": .5})
        with patch("bot.time.time", side_effect=lambda: self.clock.return_value.timestamp()):
            self.cycle(100)
            self.assertIsNone(self.state.shadow_position("custom"))
            self.cycle(110, now=NOW + timedelta(minutes=10))
            self.cycle(108, now=NOW + timedelta(minutes=20))
            self.assertEqual(self.notifier.send.call_count, 1)
            self.cycle(130, now=NOW + timedelta(minutes=31))
            self.assertEqual(self.notifier.send.call_count, 2)

    def test_legacy_timeout_cooldown_blocks_generic_entry(self):
        cfg = copy.deepcopy(CFG)
        cfg.update(method="ratio", max_holding_hours=72, cooldown_sec=1800)
        for leg in cfg["legs"].values():
            leg.update(session="always", contract_type="perpetual")
            leg["ticker"] = leg["ticker"].removesuffix("-SWAP") + "-PERP"
        self.cfg = self.init(cfg)
        self.cfg["_calc"] = bot.SpreadCalculator(3, method="ratio", history=[.99, 1., 1.01])
        self.state.set_shadow_position("custom", {
            "direction": "SHORT_BZ_LONG_CL", "ratio_side": "short_ratio", "entry_ratio": 1.1,
            "entry_time": (NOW - timedelta(hours=73)).isoformat()})
        with patch("bot.time.time", side_effect=lambda: self.clock.return_value.timestamp()):
            self.cycle(100)
            self.cycle(110, now=NOW + timedelta(minutes=10))
            self.assertEqual(self.notifier.send.call_count, 1)

    def test_existing_legacy_cooldowns_migrate_without_shortening_newer_keys(self):
        old_key = "custom:SHORT_BZ_LONG_CL:ENTRY"
        new_key = "custom:short_a_long_b:ENTRY"
        self.state.data["alerts"] = {old_key: NOW.timestamp()}
        self.cfg = self.init(CFG)
        self.assertEqual(self.state.data["alerts"].get(new_key), NOW.timestamp())
        self.assertEqual(self.state.data["alerts"][old_key], NOW.timestamp())
        newer = NOW.timestamp() + 60
        self.state.data["alerts"][new_key] = newer
        self.cfg = self.init(CFG)
        self.assertEqual(self.state.data["alerts"][new_key], newer)


class ConfigValidationTests(unittest.TestCase):
    def test_wrong_contract_and_spy_swap_leg_rejected(self):
        for ticker in ("US500-PERP", "BTC-PERP"):
            cfg = copy.deepcopy(CFG)
            cfg["legs"]["a"]["ticker"] = ticker
            with self.assertRaisesRegex(ValueError, "contract_type"):
                bot.validate_config({"pairs": [cfg]})

    def test_invalid_params_rejected(self):
        for key, value in (("window_buckets", 1), ("z_open", float("nan")), ("shadow_mode", "false"),
                           ("allowed_sides", ["LONG_RATIO"])):
            cfg = dict(CFG, **{key: value})
            with self.assertRaises(ValueError, msg=key):
                bot.validate_config({"pairs": [cfg]})
