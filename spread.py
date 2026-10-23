"""Spread statistics computed from observations preceding the current quote."""

from collections import deque
from math import isfinite, log
from statistics import mean, stdev


def spread_value(a: float, b: float, method: str, beta: float = 1.0) -> float:
    if not all(isfinite(v) and v > 0 for v in (a, b)):
        raise ValueError("Spread requires positive finite leg prices")
    if method == "ratio":
        return a / b
    if method == "log_ratio":
        return log(a) - log(b)
    if method == "beta_spread":
        return log(a) - beta * log(b)
    raise ValueError(f"Unknown spread method: {method}")


class SpreadCalculator:
    def __init__(self, window_size, z_vol_window=20, history=None, *, method="ratio", beta=None,
                 z_history=None):
        self.window_size = window_size
        self.method = method
        self.beta = beta or {"mode": "fixed", "value": 1.0}
        capacity = max(window_size, int(self.beta.get("rolling_buckets", 0)) + 1)
        self.history = deque((history or [])[-capacity:], maxlen=capacity)
        self.z_history = deque((z_history or [])[-z_vol_window:], maxlen=z_vol_window)

    @property
    def count(self):
        return min(len(self.history), self.window_size)

    @property
    def z_volatility(self):
        return stdev(self.z_history) if len(self.z_history) >= 10 else None

    def current_beta(self):
        if self.method != "beta_spread":
            return 1.0
        fallback = float(self.beta["value"])
        if self.beta["mode"] == "fixed":
            return fallback
        size = int(self.beta["rolling_buckets"])
        rows = list(self.history)[-(size + 1):]
        if len(rows) < size + 1 or any(not isinstance(row, dict) for row in rows):
            return fallback
        returns_a = [log(y["a_mid"] / x["a_mid"]) for x, y in zip(rows, rows[1:])]
        returns_b = [log(y["b_mid"] / x["b_mid"]) for x, y in zip(rows, rows[1:])]
        a_mean, b_mean = mean(returns_a), mean(returns_b)
        variance = sum((x - b_mean) ** 2 for x in returns_b)
        if variance == 0:
            return fallback
        slope = sum((a - a_mean) * (b - b_mean) for a, b in zip(returns_a, returns_b)) / variance
        return max(float(self.beta["min"]), min(float(self.beta["max"]), slope))

    def values(self, beta):
        return [spread_value(row["a_mid"], row["b_mid"], self.method, beta)
                if isinstance(row, dict) else float(row)
                for row in list(self.history)[-self.window_size:]]

    def evaluate(self, a, b, beta=None):
        beta = self.current_beta() if beta is None else beta
        value = spread_value(a, b, self.method, beta)
        values = self.values(beta)
        reference = (mean(values), stdev(values)) if len(values) >= self.window_size else None
        z = None
        if reference:
            mu, sigma = reference
            z = (value - mu) / sigma if sigma else (0.0 if value == mu else None)
        return z, reference, beta

    def add(self, row):
        result = self.evaluate(row["a_mid"], row["b_mid"])
        self.history.append(row)
        if result[0] is not None:
            self.z_history.append(result[0])
        return result

    def dump_history(self):
        return list(self.history)
