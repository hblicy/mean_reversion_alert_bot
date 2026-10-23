"""Validate pair configuration before fetching quotes or touching state."""

from math import isfinite
from sessions import SessionCalendar


def validate_config(config):
    if not isinstance(config, dict) or not isinstance(config.get("pairs"), list):
        raise ValueError("Config requires a pairs array")
    calendar = SessionCalendar(config.get("sessions"))
    interval = config.get("check_interval_sec", 30)
    if type(interval) not in (int, float) or not isfinite(interval) or interval <= 0:
        raise ValueError("check_interval_sec must be positive")
    ids = set()
    for cfg in config["pairs"]:
        if not isinstance(cfg, dict) or not isinstance(cfg.get("id"), str) or not cfg["id"] or cfg["id"] in ids:
            raise ValueError("Pairs require unique nonempty ids")
        ids.add(cfg["id"])
        if "legs" not in cfg:
            if cfg.get("source") != "variational_metadata":
                raise ValueError(f"{cfg['id']}: legs are required")
            continue
        prefix = cfg["id"]
        for key in ("enabled", "entry_require_cross", "shadow_mode", "shadow_position_enabled"):
            if key in cfg and not isinstance(cfg[key], bool):
                raise ValueError(f"{prefix}: {key} must be boolean")
        if cfg.get("source") not in ("variational_v2", "variational_metadata") or not isinstance(cfg.get("label"), str):
            raise ValueError(f"{prefix}: invalid source or label")
        if cfg.get("method") not in ("ratio", "log_ratio", "beta_spread"):
            raise ValueError(f"{prefix}: invalid method")
        if cfg.get("tier", "standard") not in ("standard", "observe"):
            raise ValueError(f"{prefix}: invalid tier")
        if not isinstance(cfg.get("quote_size", "size_1k"), str):
            raise ValueError(f"{prefix}: invalid quote_size")
        if not isinstance(cfg.get("legs"), dict) or set(cfg["legs"]) != {"a", "b"}:
            raise ValueError(f"{prefix}: legs must contain a and b")
        for leg in cfg["legs"].values():
            if not isinstance(leg, dict) or any(not isinstance(leg.get(k), str) or not leg[k] for k in ("ticker", "name", "contract_type", "session")):
                raise ValueError(f"{prefix}: invalid leg")
            if leg["contract_type"] not in ("swap", "perpetual"):
                raise ValueError(f"{prefix}: invalid contract_type")
            suffix = "-SWAP" if leg["contract_type"] == "swap" else "-PERP"
            if not leg["ticker"].endswith(suffix):
                raise ValueError(f"{prefix}: {leg['ticker']} contract_type must match {suffix}")
            if leg["name"] == "US500" and leg["ticker"] == "US500-PERP":
                raise ValueError("US500-PERP is SPY ETF, not the US500 index swap")
            if leg["session"] != "always" and leg["session"] not in calendar.windows:
                raise ValueError(f"{prefix}: unknown session {leg['session']}")
            if cfg["source"] == "variational_metadata" and leg["contract_type"] != "perpetual":
                raise ValueError("Legacy v1 fallback supports perpetual legs only; swaps require variational_v2")
        if cfg["legs"]["a"]["name"] == cfg["legs"]["b"]["name"]:
            raise ValueError(f"{prefix}: leg names must be distinct")
        for key, default, minimum in (("window_buckets", None, 2), ("sample_interval_sec", 600, 1),
                                      ("z_vol_window", 20, 2), ("max_open_gap_buckets", 3, 0),
                                      ("reopen_grace_buckets", 2, 0), ("flat_buckets", 3, 0)):
            value = cfg.get(key, default)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{prefix}: invalid {key}")
        for key, default in (("z_open", None), ("z_close", .35), ("entry_max_z", 0), ("z_vol_max", 0),
                              ("cooldown_sec", 1800), ("close_cooldown_sec", 300),
                              ("max_quote_age_sec", 120), ("max_quote_skew_sec", 30),
                              ("max_holding_hours", 72), ("max_holding_trading_hours", 46)):
            value = cfg.get(key, default)
            if type(value) not in (int, float) or not isfinite(value) or value < 0 or (key in ("z_open", "max_holding_hours", "max_holding_trading_hours") and value == 0):
                raise ValueError(f"{prefix}: invalid {key}")
        sides = cfg.get("allowed_sides")
        if not isinstance(sides, list) or not sides or any(side not in ("long_a_short_b", "short_a_long_b") for side in sides):
            raise ValueError(f"{prefix}: invalid allowed_sides")
        if cfg["method"] == "beta_spread":
            beta = cfg.get("beta", {})
            if not isinstance(beta, dict) or beta.get("mode") not in ("fixed", "rolling_return_ols"):
                raise ValueError(f"{prefix}: invalid beta mode")
            for key in ("value", "min", "max"):
                if type(beta.get(key)) not in (float, int) or not isfinite(beta[key]) or beta[key] <= 0:
                    raise ValueError(f"{prefix}: invalid beta.{key}")
            if not beta["min"] <= beta["value"] <= beta["max"] or type(beta.get("rolling_buckets")) is not int or beta["rolling_buckets"] < 2:
                raise ValueError(f"{prefix}: invalid beta bounds or rolling_buckets")
