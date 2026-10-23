import unittest
import copy
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import bot


NOW = datetime(2026, 10, 23, 14, tzinfo=timezone.utc)


def listing(ticker="US30-SWAP", contract="swap", quotes=True):
    row = {"ticker": ticker, "contract_type": contract, "mark_price": "100", "base_spread_bps": ".1",
           "funding": {"last": {"annualized_long_rate": ".05", "annualized_short_rate": ".03"}}}
    if quotes:
        row["quotes"] = {"updated_at": NOW.isoformat(), "size_1k": {"bid": "99", "ask": "101"}}
    return row


class V2SourceTests(unittest.TestCase):
    def source(self, rows=None):
        session = Mock()
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.json.return_value = {"listings": rows or [listing()]}
        return bot.VariationalV2Source(session), session

    def test_generic_leg_and_one_fetch_per_cycle(self):
        source, session = self.source([listing(), listing("US100-SWAP")])
        source.begin_cycle()
        a = source.get_leg("US30-SWAP", "size_1k")
        b = source.get_leg("US100-SWAP", "size_1k")
        self.assertEqual((a.mid, b.mid, a.status), (100, 100, "OPEN"))
        self.assertEqual(a.updated_at, NOW)
        self.assertEqual(a.funding["annualized_long_rate"], .05)
        self.assertEqual(session.get.call_count, 1)
        source.begin_cycle()
        source.get_leg("US30-SWAP", "size_1k")
        self.assertEqual(session.get.call_count, 2)

    def test_missing_quotes_or_size_never_uses_mark(self):
        source, _ = self.source([listing(quotes=False)])
        self.assertEqual(source.get_leg("US30-SWAP", "size_1k").status, "CLOSED_OR_NO_QUOTE")
        self.assertIsNone(source.get_leg("US30-SWAP", "size_1k").mid)
        source, _ = self.source()
        self.assertEqual(source.get_leg("US30-SWAP", "size_1m").status, "CLOSED_OR_NO_QUOTE")

    def test_rate_limit_honours_json_delay(self):
        source, session = self.source()
        session.get.return_value.status_code = 429
        session.get.return_value.json.return_value = {"retry_after_seconds": 75}
        with patch("bot.time.monotonic", return_value=100):
            with self.assertRaises(bot.MarketDataError):
                source.get_leg("US30-SWAP", "size_1k")
        with patch("bot.time.monotonic", return_value=174):
            source.begin_cycle()
            with self.assertRaises(bot.MarketDataError):
                source.get_leg("US30-SWAP", "size_1k")
        self.assertEqual(session.get.call_count, 1)

    def test_backoff_doubles_and_is_shared_across_legs(self):
        source, session = self.source()
        session.get.side_effect = bot.requests.Timeout("offline")
        for now, wait in ((100, 30), (130, 60), (190, 120), (310, 240), (550, 300)):
            with patch("bot.time.monotonic", return_value=now):
                source.begin_cycle()
                for ticker in ("US30-SWAP", "US100-SWAP"):
                    with self.assertRaises(bot.MarketDataError):
                        source.get_leg(ticker, "size_1k")
                self.assertEqual(source.retry_at, now + wait)
        self.assertEqual(session.get.call_count, 5)

    def test_perpetual_and_garbage_funding_are_display_only(self):
        row = listing("BTC-PERP", "perpetual")
        row["funding"] = {"last": {"annualized_rate": ".1"}}
        source, _ = self.source([row])
        self.assertEqual(source.get_leg("BTC-PERP", "size_1k").funding["annualized_rate"], .1)
        row["funding"] = {"last": {"annualized_rate": "n/a"}}
        source, _ = self.source([row])
        self.assertIsNone(source.get_leg("BTC-PERP", "size_1k").funding)

    def test_unknown_ticker_and_contract_mismatch_are_clear(self):
        source, _ = self.source()
        with self.assertRaisesRegex(ValueError, "not found"):
            source.validate_legs({"a": {"ticker": "UNKNOWN", "contract_type": "swap"}})
        with self.assertRaisesRegex(ValueError, "contract_type"):
            source.validate_legs({"a": {"ticker": "US30-SWAP", "contract_type": "perpetual"}})

    def test_config_only_new_pair_runs_through_real_adapter(self):
        from test_configured_pairs import CFG
        cfg = copy.deepcopy(CFG)
        cfg["window_buckets"] = 2
        rows = [listing("NEW-A-SWAP"), listing("NEW-B-SWAP")]
        source, session = self.source(rows)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(bot.SOURCES, {"variational_v2": lambda: source}):
            state = bot.AlertState(Path(tmp) / "state.json")
            monitors = bot.init_monitors({"pairs": [cfg]}, state)
            notifier = Mock()
            for i, price in enumerate((100, 101, 110)):
                from datetime import timedelta
                now = NOW + timedelta(minutes=i * 10)
                for row in rows:
                    row["quotes"]["updated_at"] = now.isoformat()
                rows[0]["quotes"]["size_1k"] = {"bid": price - .01, "ask": price + .01}
                with patch("bot.utc_now", return_value=now):
                    bot.run_once(monitors, state, notifier)
            self.assertEqual(state.shadow_position("custom")["direction"], "short_a_long_b")
            self.assertEqual(session.get.call_count, 4)  # one startup fetch and one per cycle

    def test_startup_checks_actual_listing_contract_and_presence(self):
        from test_configured_pairs import CFG
        for rows, expected in (([listing("NEW-A-SWAP", "perpetual"), listing("NEW-B-SWAP")], "contract_type"),
                               ([listing("NEW-A-SWAP")], "not found")):
            source, _ = self.source(rows)
            with tempfile.TemporaryDirectory() as tmp, patch.dict(bot.SOURCES, {"variational_v2": lambda: source}):
                state = bot.AlertState(Path(tmp) / "state.json")
                with self.assertRaisesRegex(ValueError, expected):
                    bot.init_monitors({"pairs": [copy.deepcopy(CFG)]}, state)

    def test_request_failures_and_backoff_remain_distinct_from_invalid_quotes(self):
        for kind in ("timeout", "429", "503"):
            with self.subTest(kind=kind):
                source, session = self.source()
                if kind == "timeout":
                    session.get.side_effect = bot.requests.Timeout("offline")
                else:
                    session.get.return_value.status_code = int(kind)
                    if kind == "429":
                        session.get.return_value.json.return_value = {"retry_after_seconds": 30}
                with patch("bot.time.monotonic", return_value=100):
                    with self.assertRaises(bot.MarketDataError) as failure:
                        source.get_leg("US30-SWAP", "size_1k")
                    self.assertIsInstance(failure.exception, bot.MarketDataUnavailable)
                    with self.assertRaises(bot.MarketDataUnavailable):
                        source.get_leg("US30-SWAP", "size_1k")
                with patch("bot.time.monotonic", return_value=110):
                    source.begin_cycle()
                    with self.assertRaises(bot.MarketDataUnavailable):
                        source.get_leg("US30-SWAP", "size_1k")
                self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
