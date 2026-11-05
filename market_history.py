"""Append-only market observations and accepted samples for offline research."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from datetime import timezone
from pathlib import Path


SETTINGS = (
    "method", "beta", "window_buckets", "sample_interval_sec", "z_open", "z_close",
    "entry_max_z", "entry_require_cross", "allowed_sides", "z_vol_window", "z_vol_max",
    "risk_max_reference_z", "max_holding_hours", "max_holding_trading_hours",
    "max_quote_age_sec", "max_quote_skew_sec", "max_open_gap_buckets", "reopen_grace_buckets",
    "flat_buckets", "cooldown_sec", "close_cooldown_sec", "shadow_mode", "shadow_position_enabled",
)
ASSET_FIELDS = (
    "bid", "ask", "price_source", "quote_updated_at", "mark_price", "base_spread_bps",
    "funding", "funding_raw", "quote_size", "quote_age_sec",
)


class HistoryWriteError(RuntimeError):
    pass


class MarketHistory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._pending = []

    @property
    def pending_count(self):
        return len(self._pending)

    def begin(self, cfg, now):
        legs = {key: {field: cfg["legs"][key][field] for field in ("ticker", "name", "contract_type", "session")}
                for key in ("a", "b")}
        interval = cfg.get("sample_interval_sec", 600)
        quote_size = cfg.get("quote_size", "size_1k")
        series = json.dumps([cfg["source"], legs["a"]["ticker"], legs["b"]["ticker"], quote_size, interval],
                            separators=(",", ":"))
        record = {
            "schema_version": 1, "pair_id": cfg["id"], "series": series,
            "poll_started_at": now.astimezone(timezone.utc).isoformat(),
            "source": cfg["source"], "quote_size": quote_size, "sample_interval_sec": interval,
            "legs": legs, "settings": {key: copy.deepcopy(cfg[key]) for key in SETTINGS if key in cfg},
            "calendar": copy.deepcopy(cfg["_calendar"].config) if "_calendar" in cfg else None,
            "scheduled_open": None,
            "status": "not_requested", "reason": None, "bucket": None, "snapshot": None, "sample": None,
        }
        self._pending.append(record)
        return record

    def attach_snapshot(self, record, snap):
        assets = {name: {key: copy.deepcopy(meta[key]) for key in ASSET_FIELDS if key in meta}
                  for name, meta in snap.metadata.get("assets", {}).items()}
        record["snapshot"] = {
            "received_at": snap.timestamp.astimezone(timezone.utc).isoformat(), "source": snap.source,
            "base": snap.base, "quote": snap.quote, "base_price": snap.base_price,
            "quote_price": snap.quote_price, "ratio": snap.ratio, "assets": assets,
        }
        record["status"] = "received"

    def flush(self):
        if not self._pending:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("""CREATE TABLE IF NOT EXISTS observations (
                    pair_id TEXT NOT NULL, series TEXT NOT NULL, started_at TEXT NOT NULL,
                    status TEXT NOT NULL, bucket INTEGER, payload TEXT NOT NULL,
                    PRIMARY KEY (pair_id, series, started_at))""")
                connection.execute("""CREATE TABLE IF NOT EXISTS samples (
                    pair_id TEXT NOT NULL, series TEXT NOT NULL, bucket INTEGER NOT NULL,
                    started_at TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (pair_id, series, bucket))""")
                with connection:
                    for record in self._pending:
                        payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                        connection.execute("""INSERT INTO observations
                            (pair_id, series, started_at, status, bucket, payload) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (pair_id, series, started_at) DO NOTHING""",
                            (record["pair_id"], record["series"], record["poll_started_at"],
                             record["status"], record["bucket"], payload))
                        if record["sample"] is not None:
                            connection.execute("""INSERT INTO samples
                                (pair_id, series, bucket, started_at, payload) VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT (pair_id, series, bucket) DO NOTHING""",
                                (record["pair_id"], record["series"], record["sample"]["bucket"],
                                 record["poll_started_at"], payload))
        except (OSError, sqlite3.Error) as exc:
            raise HistoryWriteError(f"Market history write failed: path={self.path}; pending={len(self._pending)}") from exc
        self._pending.clear()
