"""New York schedules and the intersection of two legs' trading sessions."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
BEIJING = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
DEFAULT_SESSIONS = {"us_index_swap": [["00:00", "17:00"], ["18:00", "24:00"]], "holidays": {}}


def _minutes(value):
    hour, minute = map(int, value.split(":"))
    if not (0 <= hour <= 24 and 0 <= minute < 60 and (hour < 24 or minute == 0)):
        raise ValueError(f"Invalid session time: {value}")
    return hour * 60 + minute


class SessionCalendar:
    def __init__(self, config=None):
        self.config = DEFAULT_SESSIONS if config is None else config
        self.holidays = self.config.get("holidays", {})
        self.windows = {}
        for name, windows in self.config.items():
            if name == "holidays":
                continue
            parsed = []
            for start, end in windows:
                lo, hi = _minutes(start), _minutes(end)
                if lo >= hi or (parsed and lo < parsed[-1][1]):
                    raise ValueError(f"{name}: session windows must be ordered and non-overlapping")
                parsed.append((lo, hi))
            self.windows[name] = parsed
        for name, days in self.holidays.items():
            if name not in self.windows or not isinstance(days, list):
                raise ValueError(f"Invalid holiday session: {name}")
            for day in days:
                date.fromisoformat(day)

    def intervals(self, session_name, day):
        midnight = datetime.combine(day, time(), ET)
        if session_name == "always":
            return [(midnight.astimezone(UTC), (midnight + timedelta(days=1)).astimezone(UTC))]
        if session_name not in self.windows:
            raise ValueError(f"Unknown session: {session_name}")
        if day.weekday() >= 5 or day.isoformat() in self.holidays.get(session_name, []):
            return []
        return [((midnight + timedelta(minutes=start)).astimezone(UTC),
                 (midnight + timedelta(minutes=end)).astimezone(UTC))
                for start, end in self.windows[session_name]
                if not (session_name == "us_index_swap" and day.weekday() == 4 and start >= 17 * 60)]

    def is_open(self, session_name, utc_dt):
        if utc_dt.tzinfo is None:
            raise ValueError("Session clock requires a timezone")
        return any(start <= utc_dt < end for start, end in self.intervals(session_name, utc_dt.astimezone(ET).date()))

    def pair_is_open(self, legs, utc_dt):
        return all(self.is_open(leg["session"], utc_dt) for leg in legs.values())

    def pair_intervals(self, legs, day):
        a, b = (self.intervals(legs[key]["session"], day) for key in ("a", "b"))
        return [(max(x, u), min(y, v)) for x, y in a for u, v in b if max(x, u) < min(y, v)]

    def open_seconds(self, legs, start, end):
        if start >= end:
            return 0.0
        day, last = start.astimezone(ET).date(), end.astimezone(ET).date()
        seconds = 0.0
        while day <= last:
            for lo, hi in self.pair_intervals(legs, day):
                seconds += max(0, (min(end, hi) - max(start, lo)).total_seconds())
            day += timedelta(days=1)
        return seconds

    def gap(self, legs, previous_bucket, bucket, interval):
        count, closed = 0, False
        for number in range(previous_bucket + 1, bucket):
            dt = datetime.fromtimestamp(number * interval, UTC)
            if self.pair_is_open(legs, dt):
                count += 1
            else:
                closed = True
        return count, closed

    def next_close(self, legs, utc_dt):
        if all(leg["session"] == "always" for leg in legs.values()):
            return None
        day = utc_dt.astimezone(ET).date()
        for lo, hi in self.pair_intervals(legs, day):
            if lo <= utc_dt < hi:
                # Adjacent midnight windows continue trading on the next day.
                if self.pair_is_open(legs, hi):
                    return self.next_close(legs, hi)
                return hi
        return None


def is_open(session_name, utc_dt):
    return SessionCalendar().is_open(session_name, utc_dt)
