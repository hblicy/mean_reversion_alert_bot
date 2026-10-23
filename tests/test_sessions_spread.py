import math
import unittest
from datetime import datetime, timedelta, timezone

from sessions import SessionCalendar
from spread import SpreadCalculator, spread_value


UTC = timezone.utc
LEGS = {key: {"session": "us_index_swap"} for key in ("a", "b")}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.calendar = SessionCalendar({"us_index_swap": [["00:00", "17:00"], ["18:00", "24:00"]],
                                         "holidays": {"us_index_swap": ["2026-12-25"]}})

    def test_daily_break_and_dst(self):
        for day, hour in (("2026-10-29", 21), ("2026-11-02", 22)):
            closed = datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)
            self.assertTrue(self.calendar.is_open("us_index_swap", closed - timedelta(seconds=1)))
            self.assertFalse(self.calendar.is_open("us_index_swap", closed))
            self.assertTrue(self.calendar.is_open("us_index_swap", closed + timedelta(hours=1)))
            self.assertEqual((closed + timedelta(hours=8)).hour, 5 if hour == 21 else 6)

    def test_weekends_holidays_and_always(self):
        for day in ("2026-10-24", "2026-10-25", "2026-12-25"):
            dt = datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC)
            self.assertFalse(self.calendar.is_open("us_index_swap", dt))
            self.assertTrue(self.calendar.is_open("always", dt))

    def test_only_open_missing_buckets_count(self):
        start = datetime(2026, 10, 23, 20, 50, tzinfo=UTC)
        end = datetime(2026, 10, 26, 4, tzinfo=UTC)
        self.assertEqual(self.calendar.gap(LEGS, int(start.timestamp() // 600), int(end.timestamp() // 600), 600), (0, True))
        end = start + timedelta(minutes=40)
        self.assertEqual(self.calendar.gap(LEGS, int(start.timestamp() // 600), int(end.timestamp() // 600), 600), (0, True))

    def test_trading_time_excludes_break_and_weekend(self):
        start = datetime(2026, 10, 23, 20, tzinfo=UTC)
        end = datetime(2026, 10, 26, 5, tzinfo=UTC)
        self.assertEqual(self.calendar.open_seconds(LEGS, start, end), 2 * 3600)

    def test_pair_session_intersection(self):
        cal = SessionCalendar({"early": [["09:00", "12:00"]], "late": [["11:00", "14:00"]]})
        legs = {"a": {"session": "early"}, "b": {"session": "late"}}
        start = datetime(2026, 10, 23, 12, tzinfo=UTC)
        self.assertEqual(cal.open_seconds(legs, start, start + timedelta(hours=8)), 3600)


class SpreadTests(unittest.TestCase):
    def test_log_ratio_is_symmetric(self):
        self.assertAlmostEqual(spread_value(15, 9, "log_ratio"), -spread_value(9, 15, "log_ratio"))

    def test_z_excludes_current_point(self):
        calc = SpreadCalculator(3, method="ratio")
        for i, a in enumerate((1, 2, 3)):
            self.assertIsNone(calc.add({"bucket": i, "ts": i, "a_mid": a, "b_mid": 1})[0])
        z, reference, beta = calc.add({"bucket": 3, "ts": 3, "a_mid": 5, "b_mid": 1})
        self.assertAlmostEqual(z, 3)
        self.assertEqual(reference, (2, 1))
        self.assertEqual(beta, 1)

    def test_beta_uses_previous_returns_and_clamps(self):
        beta = {"mode": "rolling_return_ols", "value": .5, "rolling_buckets": 3, "min": .3, "max": 1}
        calc = SpreadCalculator(3, method="beta_spread", beta=beta)
        b_log = [1, 1.1, 1.3, 1.6]
        for i, b in enumerate(b_log):
            self.assertEqual(calc.current_beta(), .5)
            calc.add({"bucket": i, "ts": i, "a_mid": math.exp(2 * b), "b_mid": math.exp(b)})
        self.assertAlmostEqual(calc.current_beta(), 1)
        _, _, used = calc.add({"bucket": 4, "ts": 4, "a_mid": 1e20, "b_mid": 1.1})
        self.assertAlmostEqual(used, 1)

    def test_negative_and_zero_spreads_are_valid(self):
        self.assertEqual(spread_value(1, 1, "log_ratio"), 0)
        self.assertLess(spread_value(1, 2, "beta_spread", .5), 0)

    def test_legacy_ratio_history_survives(self):
        calc = SpreadCalculator(3, method="ratio", history=[1., 2., 3.])
        z, reference, _ = calc.add({"bucket": 4, "ts": 4, "a_mid": 5, "b_mid": 1})
        self.assertEqual(reference, (2, 1))
        self.assertEqual(z, 3)


if __name__ == "__main__":
    unittest.main()
