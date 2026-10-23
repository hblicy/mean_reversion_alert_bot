"""
Mean-reversion signal alert bot.

This program never places orders. It reads public market data, computes the
same style of rolling z-score used by the old pair bots, and pushes Telegram
alerts for manual execution.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from math import isfinite
from statistics import mean, stdev
from typing import Any

import requests
from dotenv import load_dotenv
from spread import SpreadCalculator, spread_value
from sessions import SessionCalendar, BEIJING, ET
from configuration import validate_config


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

logger = logging.getLogger("alert_bot")


class MarketDataError(ValueError):
    pass


class MarketDataUnavailable(MarketDataError):
    pass


@dataclass
class LegQuote:
    ticker: str
    contract_type: str
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    updated_at: datetime | None = None
    mark_price: float | None = None
    base_spread_bps: float | None = None
    funding: dict | None = None
    status: str = "CLOSED_OR_NO_QUOTE"


@dataclass
class PriceSnapshot:
    base: str
    quote: str
    base_price: float
    quote_price: float
    ratio: float
    source: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    level: str
    direction: str
    action: str
    reason: str
    tradeable: bool = True
    caution: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ZScoreCalculator:
    def __init__(self, window_size: int, z_vol_window: int = 20, history: list[float] | None = None):
        self.window_size = window_size
        self.ratios: deque[float] = deque(maxlen=window_size)
        self.z_history: deque[float] = deque(maxlen=z_vol_window)
        if history:
            for value in history[-window_size:]:
                self.ratios.append(float(value))

    def add(self, ratio: float) -> float | None:
        self.ratios.append(ratio)
        if len(self.ratios) < 20:
            return None
        sigma = stdev(self.ratios)
        if sigma == 0:
            z = 0.0
        else:
            z = (ratio - mean(self.ratios)) / sigma
        self.z_history.append(z)
        return z

    @property
    def count(self) -> int:
        return len(self.ratios)

    @property
    def z_volatility(self) -> float | None:
        if len(self.z_history) < 10:
            return None
        return stdev(self.z_history)

    def dump_history(self) -> list[float]:
        return list(self.ratios)


class AlertState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"alerts": {}, "history": {}}
        self.load()

    def load(self) -> None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Cannot read state file %s; refusing to reset tracking state", self.path)
            raise
        try:
            self.data = json.loads(content)
        except json.JSONDecodeError:
            logger.exception("Invalid state file %s; refusing to reset tracking state", self.path)
            raise

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.path)

    def history(self, monitor_id: str) -> list[float]:
        return list(self.data.setdefault("history", {}).get(monitor_id, []))

    def set_history(self, monitor_id: str, values: list[float]) -> None:
        self.data.setdefault("history", {})[monitor_id] = values

    def should_send(self, key: str, cooldown_sec: int) -> bool:
        last_ts = self.data.setdefault("alerts", {}).get(key)
        now = time.time()
        if last_ts is None or now - float(last_ts) >= cooldown_sec:
            return True
        return False

    def mark_sent(self, key: str) -> None:
        self.data.setdefault("alerts", {})[key] = time.time()

    def shadow_position(self, monitor_id: str) -> dict[str, Any] | None:
        pos = self.data.setdefault("shadow_positions", {}).get(monitor_id)
        return dict(pos) if pos else None

    def set_shadow_position(self, monitor_id: str, position: dict[str, Any]) -> None:
        self.data.setdefault("shadow_positions", {})[monitor_id] = position

    def clear_shadow_position(self, monitor_id: str) -> None:
        self.data.setdefault("shadow_positions", {}).pop(monitor_id, None)


class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
        self.print_messages = os.getenv("PRINT_MESSAGES", "1").lower() not in ("0", "false", "no")

    def send(self, message: str) -> bool:
        if self.print_messages:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(message.encode(encoding, errors="backslashreplace").decode(encoding))
            print("-" * 60)
        if not self.token or not self.chat_id:
            logger.info("Telegram env is not configured; message printed locally only.")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "disable_web_page_preview": True}
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code >= 400:
            logger.warning("Telegram delivery rejected: HTTP %s; will retry", response.status_code)
            return False
        try:
            data = response.json()
        except ValueError:
            logger.warning("Telegram delivery returned invalid JSON; will retry")
            return False
        if not isinstance(data, dict):
            logger.warning("Telegram delivery returned an invalid response; will retry")
            return False
        if not data.get("ok"):
            logger.warning("Telegram delivery rejected: error_code=%s; will retry", data.get("error_code", "unknown"))
            return False
        return True


class HttpSource:
    timeout = 12

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        headers = {"User-Agent": "mean-reversion-alert-bot/1.0"}
        response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


class VariationalMetadataSource(HttpSource):
    name = "variational_metadata"
    url = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
    _cache_data: dict[str, Any] | None = None
    _cache_ts: float = 0.0
    _cache_ttl_sec: float = 5.0

    def _stats(self) -> dict[str, Any]:
        now = time.time()
        if self.__class__._cache_data is not None and now - self.__class__._cache_ts < self.__class__._cache_ttl_sec:
            return self.__class__._cache_data
        data = self._get_json(self.url)
        if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
            raise MarketDataError("Variational response has no listings array")
        self.__class__._cache_data = data
        self.__class__._cache_ts = now
        return data

    def _listing(self, ticker: str) -> dict[str, Any]:
        wanted = ticker.upper()
        for item in self._stats().get("listings", []):
            if not isinstance(item, dict):
                raise MarketDataError("Variational listing must be an object")
            if str(item.get("ticker", "")).upper() == wanted:
                return item
        raise MarketDataError(f"Variational listing not found: {ticker}")

    def _price(self, ticker: str, quote_size: str) -> tuple[float, dict[str, Any]]:
        listing = self._listing(ticker)
        quotes = listing.get("quotes") or {}
        if not isinstance(quotes, dict):
            raise MarketDataError(f"{ticker}: quotes must be an object")
        quote = quotes.get(quote_size) or {}
        if not isinstance(quote, dict):
            raise MarketDataError(f"{ticker}: {quote_size} must be an object")
        bid = _safe_float(quote.get("bid"))
        ask = _safe_float(quote.get("ask"))
        price_source = "quote"
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2
        else:
            price = _safe_float(listing.get("mark_price"))
            price_source = "mark_price"
        if not isfinite(price) or price <= 0:
            raise MarketDataError(f"Bad Variational price for {ticker}")

        updated_at = str(quotes.get("updated_at", ""))
        return price, {
            "ticker": ticker,
            "price_source": price_source,
            "mark_price": _safe_float(listing.get("mark_price")),
            "quote_size": quote_size,
            "bid": bid,
            "ask": ask,
            "quote_updated_at": updated_at,
            "quote_age_sec": quote_age_seconds(updated_at),
            "base_spread_bps": _safe_float(listing.get("base_spread_bps")),
        }

    def snapshot(self, pair: str, cfg: dict[str, Any] | None = None) -> PriceSnapshot:
        quote_size = str((cfg or {}).get("quote_size", "size_1k"))
        if cfg and "legs" in cfg:
            legs = cfg["legs"]
            a, a_meta = self._price(legs["a"]["ticker"].removesuffix("-PERP"), quote_size)
            b, b_meta = self._price(legs["b"]["ticker"].removesuffix("-PERP"), quote_size)
            return PriceSnapshot(legs["a"]["name"], legs["b"]["name"], a, b, a / b, self.name,
                                 utc_now(), {"quote_size": quote_size, "assets": {
                                     legs["a"]["name"]: a_meta, legs["b"]["name"]: b_meta}})
        if pair == "BTC_ETH":
            btc, btc_meta = self._price("BTC", quote_size)
            eth, eth_meta = self._price("ETH", quote_size)
            return PriceSnapshot(
                "ETH",
                "BTC",
                eth,
                btc,
                eth / btc,
                self.name,
                utc_now(),
                {"quote_size": quote_size, "assets": {"BTC": btc_meta, "ETH": eth_meta}},
            )
        if pair == "XAG_BTC":
            btc, btc_meta = self._price("BTC", quote_size)
            xag, xag_meta = self._price("XAG", quote_size)
            return PriceSnapshot(
                "BTC",
                "XAG",
                btc,
                xag,
                btc / xag,
                self.name,
                utc_now(),
                {"quote_size": quote_size, "assets": {"BTC": btc_meta, "XAG": xag_meta}},
            )
        if pair == "BZ_CL":
            bz, bz_meta = self._price("BZ", quote_size)
            cl, cl_meta = self._price("CL", quote_size)
            return PriceSnapshot(
                "BZ",
                "CL",
                bz,
                cl,
                bz / cl,
                self.name,
                utc_now(),
                {"quote_size": quote_size, "assets": {"BZ": bz_meta, "CL": cl_meta}},
            )
        if pair == "XAG_XAU":
            xag, xag_meta = self._price("XAG", quote_size)
            xau, xau_meta = self._price("XAU", quote_size)
            return PriceSnapshot("XAG", "XAU", xag, xau, xag / xau,
                                 self.name, utc_now(),
                                 {"quote_size": quote_size, "assets": {"XAG": xag_meta, "XAU": xau_meta}})
        raise ValueError(f"{self.name} does not support pair {pair}")


class VariationalV2Source(HttpSource):
    name = "variational_v2"
    url = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"

    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        self.retry_at = 0.0
        self.failures = 0
        self.begin_cycle()

    def begin_cycle(self):
        self._listings = None
        self._cycle_error = None

    def _stats(self):
        if self._cycle_error:
            raise MarketDataUnavailable(self._cycle_error)
        if self._listings is not None:
            return self._listings
        now = time.monotonic()
        if now < self.retry_at:
            raise MarketDataUnavailable(f"Variational backoff: retry in {self.retry_at - now:.0f}s")
        try:
            response = self.session.get(self.url, timeout=self.timeout,
                                        headers={"User-Agent": "mean-reversion-alert-bot/2.0"})
            if response.status_code == 429:
                try:
                    delay = float(response.json()["retry_after_seconds"])
                    if not isfinite(delay) or delay < 0:
                        raise ValueError("Invalid retry delay")
                except (ValueError, KeyError, TypeError):
                    delay = 30.0
                self.retry_at = now + delay
                self._cycle_error = f"Variational rate limited; retry in {delay:g}s"
                raise MarketDataError(self._cycle_error)
            response.raise_for_status()
            if response.status_code >= 500:
                raise requests.HTTPError("Variational server error")
            data = response.json()
            rows = data.get("listings") if isinstance(data, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("ticker") for row in rows):
                raise MarketDataError("Variational v2 response has no valid listings array")
        except (requests.RequestException, ValueError) as exc:
            if not self._cycle_error:
                self.failures += 1
                delay = min(30 * 2 ** min(self.failures - 1, 4), 300)
                self.retry_at = now + delay
                self._cycle_error = f"Variational fetch failed ({type(exc).__name__}); retry in {delay}s"
            raise MarketDataUnavailable(self._cycle_error) from None
        self.failures = 0
        self._listings = {row["ticker"]: row for row in rows}
        return self._listings

    def validate_legs(self, legs):
        rows = self._stats()
        for leg in legs.values():
            ticker = leg["ticker"]
            if ticker not in rows:
                raise ValueError(f"Variational ticker not found: {ticker}")
            actual = rows[ticker].get("contract_type")
            if actual != leg["contract_type"]:
                raise ValueError(f"{ticker}: contract_type={actual}, configured {leg['contract_type']}")

    def get_leg(self, ticker, quote_size):
        row = self._stats().get(ticker)
        if row is None:
            raise MarketDataError(f"Variational listing not found: {ticker}")
        result = LegQuote(ticker, row.get("contract_type", ""))
        for key in ("mark_price", "base_spread_bps"):
            value = _safe_float(row.get(key), float("nan"))
            setattr(result, key, value if isfinite(value) else None)
        funding = row.get("funding")
        if isinstance(funding, dict):
            rates = funding.get("last") if result.contract_type == "perpetual" else funding.get("next") or funding.get("last")
            keys = ("annualized_rate",) if result.contract_type == "perpetual" else ("annualized_long_rate", "annualized_short_rate")
            if isinstance(rates, dict):
                parsed = {key: _safe_float(rates.get(key), float("nan")) for key in keys}
                if all(isfinite(value) for value in parsed.values()):
                    result.funding = parsed
                    result.funding["next_timestamp"] = funding.get("next_timestamp")
        quotes = row.get("quotes")
        if not isinstance(quotes, dict) or not quotes.get(quote_size):
            return result
        quote = quotes[quote_size]
        if not isinstance(quote, dict):
            raise MarketDataError(f"{ticker}: invalid {quote_size} quote")
        bid, ask = _safe_float(quote.get("bid")), _safe_float(quote.get("ask"))
        if not (isfinite(bid) and isfinite(ask) and 0 < bid <= ask):
            raise MarketDataError(f"{ticker}: invalid BBO")
        try:
            updated_at = parse_timestamp(quotes.get("updated_at", ""))
        except (ValueError, TypeError, AttributeError):
            raise MarketDataError(f"{ticker}: invalid quote updated_at") from None
        result.bid, result.ask, result.mid = bid, ask, (bid + ask) / 2
        result.updated_at, result.status = updated_at, "OPEN"
        return result

    def snapshot(self, pair, cfg=None):
        legs = cfg["legs"]
        quotes = [self.get_leg(legs[key]["ticker"], cfg.get("quote_size", "size_1k")) for key in ("a", "b")]
        if any(quote.status != "OPEN" for quote in quotes):
            raise MarketDataError("CLOSED_OR_NO_QUOTE")
        a, b = quotes
        assets = {legs[key]["name"]: {
            "bid": quote.bid, "ask": quote.ask, "quote_updated_at": quote.updated_at.isoformat(),
            "price_source": "quote", "funding": quote.funding, "base_spread_bps": quote.base_spread_bps,
        } for key, quote in zip(("a", "b"), quotes)}
        return PriceSnapshot(legs["a"]["name"], legs["b"]["name"], a.mid, b.mid, a.mid / b.mid,
                             self.name, utc_now(), {"assets": assets, "quote_size": cfg.get("quote_size", "size_1k")})


SOURCES = {
    VariationalV2Source.name: VariationalV2Source,
    VariationalMetadataSource.name: VariationalMetadataSource,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def quote_age_seconds(updated_at: str) -> float | None:
    if not updated_at:
        return None
    try:
        text = updated_at.replace("Z", "+00:00")
        if "." in text:
            head, tail = text.split(".", 1)
            sign = "+" if "+" in tail else "-" if "-" in tail else ""
            if sign:
                frac, zone = tail.split(sign, 1)
                text = f"{head}.{frac[:6]}{sign}{zone}"
        dt = datetime.fromisoformat(text)
        return max(0.0, (utc_now() - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        config = json.load(fh)
    validate_config(config)
    return config


def init_monitors(config: dict[str, Any], state: AlertState) -> list[dict[str, Any]]:
    validate_config(config)
    monitors = []
    sources = {}
    calendar = SessionCalendar(config.get("sessions"))
    for item in config["pairs"]:
        if "legs" in item and item["source"] == VariationalV2Source.name:
            if item["source"] not in sources:
                sources[item["source"]] = SOURCES[item["source"]]()
            sources[item["source"]].validate_legs(item["legs"])
    for item in config.get("pairs", []):
        if not item.get("enabled", True):
            continue
        source_name = item["source"]
        if source_name not in SOURCES:
            raise ValueError(f"Unknown source {source_name}. Available: {', '.join(SOURCES)}")
        item = dict(item)
        if source_name not in sources:
            sources[source_name] = SOURCES[source_name]()
        item["_source"] = sources[source_name]
        item["_calendar"] = calendar
        if "legs" in item:
            alerts = state.data.setdefault("alerts", {})
            prefix = f"{item['id']}:"
            for key, sent_at in list(alerts.items()):
                if key.startswith(prefix) and key.endswith(":ENTRY"):
                    direction = key[len(prefix):-len(":ENTRY")]
                    if ratio_side(direction):
                        normalized = entry_cooldown_key(item, direction)
                        if normalized != key:
                            alerts[normalized] = max(float(sent_at), float(alerts.get(normalized, 0)))
            saved = state.data.get("statistics", {}).get(item["id"], {})
            history = state.history(item["id"])
            signature = [item.get("sample_interval_sec", 600),
                         item["legs"]["a"]["ticker"], item["legs"]["b"]["ticker"]]
            model = [item["method"], json.dumps(item.get("beta"), sort_keys=True),
                     item["window_buckets"], item.get("z_vol_window", 20)]
            if saved.get("signature") not in (None, signature):
                history = []
                state.data.setdefault("samples", {}).pop(item["id"], None)
                state.data.setdefault("quality", {}).pop(item["id"], None)
            sample = state.data.get("samples", {}).get(item["id"], {})
            if sample and sample.get("interval") != item.get("sample_interval_sec", 600):
                history = []
                state.data["samples"].pop(item["id"], None)
                state.data.setdefault("quality", {}).pop(item["id"], None)
            if item["method"] != "ratio":
                history = [row for row in history if isinstance(row, dict)]
            item["_calc"] = SpreadCalculator(item["window_buckets"], item.get("z_vol_window", 20), history,
                                              method=item["method"], beta=item.get("beta"),
                                              z_history=saved.get("z_history", []) if saved.get("model") == model else [])
            item["_prev_z"] = saved.get("prev_z") if saved.get("signature") == signature and saved.get("model") == model else None
            item["_signature"] = signature
            item["_model"] = model
            pending = state.data.get("pending_entries", {}).get(item["id"])
            if saved.get("model") not in (None, model):
                state.data.setdefault("pending_entries", {}).pop(item["id"], None)
            elif pending and "previous_ratio" in pending:
                if item["method"] != "ratio":
                    state.data["pending_entries"].pop(item["id"])
                else:
                    pending.update(method="ratio", beta=1.0, previous_spread=pending.pop("previous_ratio"),
                                   direction="short_a_long_b" if ratio_side(pending["direction"]) == "short_ratio" else "long_a_short_b")
                    logger.info("%s migrated legacy pending ENTRY", item["label"])
            monitors.append(item)
            continue
        sample = state.data.setdefault("samples", {}).get(item["id"], {})
        # Old polling histories have no timestamps and cannot be mixed with timed samples.
        history = state.history(item["id"]) if sample.get("interval") == item.get("sample_interval_sec", 600) else []
        item["_calc"] = ZScoreCalculator(
            int(item["window_size"]),
            int(item.get("z_vol_window", 20)),
            history,
        )
        item["_prev_z"] = None
        monitors.append(item)
    return monitors




def format_message(cfg: dict[str, Any], snap: PriceSnapshot, z: float | None, z_vol: float | None, signal: Signal) -> str:
    if "legs" in cfg:
        return format_pair_message(cfg, snap, z, signal)
    if signal.level == "CLOSE":
        return format_close_message(cfg, snap, z, signal)

    lines = [
        f"🟢 [ENTRY] {cfg['label']} 行情提醒",
        f"行情源：{'Variational' if snap.source == VariationalMetadataSource.name else snap.source}",
        f"回归方向参考：{action_label(signal)}",
        f"滚动 Z：{z:+.3f}（触发阈值 {float(cfg['z_open']):.2f}）",
        f"{snap.base}/{snap.quote}：{snap.ratio:.8f}",
        f"行情源中间价：{snap.base} ${snap.base_price:,.4f}｜{snap.quote} ${snap.quote_price:,.4f}",
    ]
    reference_mean = signal.details["reference_mean"]
    width = signal.details["reference_std"] * float(cfg.get("z_close", 0.35))
    lines.append(f"本次固定均值：{reference_mean:.8f}")
    lines.append(f"回归观察区间：{reference_mean - width:.8f} ～ {reference_mean + width:.8f}")
    if snap.source == VariationalMetadataSource.name:
        lines.extend(format_variational_metadata(snap))
    lines.extend([
        f"信号最多跟踪 {float(cfg.get('max_holding_hours', 72)):g} 小时；到期提醒结束跟踪。",
        "仅供行情参考，不代表可成交价格或收益。请在实际交易平台自行核对合约、手续费、点差及隔夜成本。",
        "不指定执行平台，不下单。",
    ])
    return "\n".join(lines)


def format_close_message(cfg: dict[str, Any], snap: PriceSnapshot, z: float | None, signal: Signal) -> str:
    lines = [
        f"🔵 [CLOSE] {cfg['label']} 结束跟踪提醒",
        f"行情源：{'Variational' if snap.source == VariationalMetadataSource.name else snap.source}",
        f"原因：{signal.reason}",
        f"对应方向：{action_label(signal)}",
        f"{snap.base}/{snap.quote}：{snap.ratio:.8f}",
    ]
    if z is not None:
        lines.append(f"当前滚动 Z：{z:+.3f}（仅供观察）")
    if "reference_z" in signal.details:
        lines.append(f"相对 ENTRY 固定基准的 Z：{signal.details['reference_z']:+.3f}")
    lines.extend([
        f"信号方向上的比率变化：{signal.details['move_pct']:+.3f}%（不是持仓收益率）",
        f"ENTRY 信号已持续：{signal.details['holding_hours']:.2f} 小时",
        "这是行情跟踪提醒，不代表你已成交或已经盈利。",
        "若已跟随，请在实际交易平台自行核对两腿持仓、可成交报价及成本，再决定退出。",
    ])
    return "\n".join(lines)




def format_variational_metadata(snap: PriceSnapshot) -> list[str]:
    ages = []
    for ticker, meta in snap.metadata.get("assets", {}).items():
        age = quote_age_seconds(meta.get("quote_updated_at", ""))
        ages.append(f"{ticker} {age:.0f}s" if age is not None else f"{ticker} N/A")
    return [f"行情报价档位：{snap.metadata.get('quote_size', 'N/A')}｜age {' / '.join(ages)}"]


def validate_bbo(snap: PriceSnapshot) -> None:
    assets = snap.metadata.get("assets")
    if not assets:
        raise ValueError(f"Missing BBO metadata for {snap.base}/{snap.quote}")
    for ticker in (snap.base, snap.quote):
        asset = assets.get(ticker)
        if not asset:
            raise ValueError(f"Missing BBO metadata for {ticker}")
        bid, ask = float(asset["bid"]), float(asset["ask"])
        if not (isfinite(bid) and isfinite(ask) and 0 < bid <= ask):
            raise ValueError(f"Bad BBO for {ticker}: bid={bid}, ask={ask}")


def action_label(signal: Signal) -> str:
    closing = signal.direction.startswith("CLOSE_")
    direction = signal.direction.removeprefix("CLOSE_")
    parsed = re.fullmatch(r"(LONG|SHORT)_(.+)_(LONG|SHORT)_(.+)", direction)
    if parsed:
        a, name_a, b, name_b = parsed.groups()
        prefix = "平" if closing else ""
        return f"{prefix}{'多' if a == 'LONG' else '空'} {name_a} / {prefix}{'多' if b == 'LONG' else '空'} {name_b}"
    return signal.action


def classify_level(abs_z: float, cfg: dict[str, Any]) -> str | None:
    if abs_z >= float(cfg["z_open"]):
        return "ENTRY"
    return None


def build_signal(cfg: dict[str, Any], z: float, prev_z: float | None, z_vol: float | None) -> Signal | None:
    z_open = float(cfg["z_open"])
    level = classify_level(abs(z), cfg)
    if level != "ENTRY":
        return None

    if cfg.get("entry_require_cross", False):
        if prev_z is None or z * prev_z <= 0 or abs(z) >= abs(prev_z):
            return None
    max_z = float(cfg.get("entry_max_z", 0) or 0)
    z_vol_max = float(cfg.get("z_vol_max", 0) or 0)
    if max_z > 0 and abs(z) > max_z:
        return None
    if z_vol_max > 0 and (z_vol is None or z_vol > z_vol_max):
        return None

    if "legs" in cfg:
        side = "short_a_long_b" if z > 0 else "long_a_short_b"
        if side not in cfg["allowed_sides"]:
            return None
        a, b = (cfg["legs"][key]["name"] for key in ("a", "b"))
        label = f"空 {a} / 多 {b}" if z > 0 else f"多 {a} / 空 {b}"
        return Signal("ENTRY", side, label, f"价差偏离 z={z:+.3f}")

    strategy = cfg.get("strategy", "two_way")
    if strategy == "btc_strength_bias":
        if z < z_open:
            return None
        return Signal("ENTRY", "LONG_BTC_SHORT_ETH", "", f"ETH/BTC 高位 z={z:+.3f}", True)

    if z >= z_open:
        if cfg["pair"] == "XAG_BTC":
            direction = "SHORT_BTC_LONG_XAG"
        elif cfg["pair"] == "BZ_CL":
            direction = "SHORT_BZ_LONG_CL"
        elif cfg["pair"] == "XAG_XAU":
            direction = "SHORT_XAG_LONG_XAU"
        else:
            direction = "short_a_long_b"
        return Signal("ENTRY", direction, "", f"比率高位 z={z:+.3f}", True)
    if z <= -z_open:
        if cfg["pair"] == "XAG_BTC":
            direction = "LONG_BTC_SHORT_XAG"
        elif cfg["pair"] == "BZ_CL":
            direction = "LONG_BZ_SHORT_CL"
        elif cfg["pair"] == "XAG_XAU":
            direction = "LONG_XAG_SHORT_XAU"
        else:
            direction = "long_a_short_b"
        return Signal("ENTRY", direction, "", f"比率低位 z={z:+.3f}", True)
    return None


def ratio_side(direction: str) -> str:
    if direction == "short_a_long_b":
        return "short_ratio"
    if direction == "long_a_short_b":
        return "long_ratio"
    if direction in ("LONG_BTC_SHORT_ETH", "SHORT_BTC_LONG_XAG", "SHORT_BZ_LONG_CL", "SHORT_XAG_LONG_XAU"):
        return "short_ratio"
    if direction in ("LONG_BTC_SHORT_XAG", "SHORT_BTC_LONG_ETH", "LONG_BZ_SHORT_CL", "LONG_XAG_SHORT_XAU"):
        return "long_ratio"
    return ""


def entry_cooldown_key(cfg, direction):
    if "legs" in cfg:
        direction = {"short_ratio": "short_a_long_b", "long_ratio": "long_a_short_b"}[ratio_side(direction)]
    return f"{cfg['id']}:{direction}:ENTRY"


def favorable_ratio_move_pct(position: dict[str, Any], current_ratio: float) -> float:
    entry_ratio = float(position.get("entry_ratio", 0) or 0)
    if entry_ratio <= 0:
        return 0.0
    side = position.get("ratio_side", "")
    if side == "short_ratio":
        return (entry_ratio - current_ratio) / entry_ratio * 100
    if side == "long_ratio":
        return (current_ratio - entry_ratio) / entry_ratio * 100
    return 0.0


def build_close_signal(cfg: dict[str, Any], snap: PriceSnapshot, z: float | None, position: dict[str, Any] | None) -> Signal | None:
    if not position or not cfg.get("shadow_position_enabled", True):
        return None
    side = position["ratio_side"]
    adverse_sign = 1 if side == "short_ratio" else -1
    move_pct = favorable_ratio_move_pct(position, snap.ratio)
    details = {
        "move_pct": move_pct,
        "holding_hours": (utc_now() - parse_timestamp(position["entry_time"])).total_seconds() / 3600,
    }
    reasons = []
    if "reference_mean" in position and "reference_std" in position:
        reference_mean = float(position["reference_mean"])
        reference_std = float(position["reference_std"])
        reference_z = (snap.ratio - reference_mean) / reference_std
        details["reference_z"] = reference_z
        # Crossing the entire band also completes reversion; rolling mean drift does not.
        boundary = reference_mean + adverse_sign * reference_std * float(position["reference_z_close"])
        reached = snap.ratio <= boundary if adverse_sign == 1 else snap.ratio >= boundary
        if move_pct > 0 and reached:
            reasons.append("比率已回归至 ENTRY 固定均值区间或越过该区间")
    else:
        logger.warning("%s legacy signal lacks a frozen reference; reversion close unavailable, risk/timeout tracking retained", cfg["label"])

    max_z = float(cfg.get("entry_max_z", 0) or 0)
    if z is not None and max_z > 0 and z * adverse_sign > max_z and move_pct < 0:
        reasons.append(f"风险提醒：比率较 ENTRY 继续恶化且滚动 |Z| 超过 {max_z:g}")
    if not reasons:
        return None
    return Signal("CLOSE", "CLOSE_" + position["direction"], "", "；".join(reasons), details=details)


def make_shadow_position(cfg: dict[str, Any], signal: Signal, snap: PriceSnapshot, z: float,
                         *, reference_mean: float, reference_std: float) -> dict[str, Any]:
    side = ratio_side(signal.direction)
    if not side:
        raise ValueError(f"Unsupported signal direction: {signal.direction}")
    if not (isfinite(reference_mean) and reference_mean > 0 and isfinite(reference_std) and reference_std > 0):
        raise ValueError("Signal reference requires a positive finite mean and standard deviation")
    return {
        "direction": signal.direction,
        "ratio_side": side,
        "entry_ratio": snap.ratio,
        "entry_z": z,
        "entry_time": utc_now().isoformat(),
        "reference_mean": reference_mean,
        "reference_std": reference_std,
        "reference_z_close": float(cfg.get("z_close", 0.35)),
    }


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return timestamp.astimezone(timezone.utc)


def validate_snapshot(cfg: dict[str, Any], snap: PriceSnapshot) -> list[float]:
    validate_bbo(snap)
    if not all(isfinite(p) and p > 0 for p in (snap.base_price, snap.quote_price, snap.ratio)):
        raise ValueError("Invalid pair prices")
    timestamps = []
    for ticker in (snap.base, snap.quote):
        asset = snap.metadata["assets"][ticker]
        if asset.get("price_source") != "quote":
            raise ValueError(f"{ticker}: executable quote unavailable")
        timestamp = parse_timestamp(asset["quote_updated_at"]).timestamp()
        age = utc_now().timestamp() - timestamp
        if age < 0 or age > float(cfg.get("max_quote_age_sec", 120)):
            raise ValueError(f"{ticker}: stale/future quote, age={age:.1f}s")
        timestamps.append(timestamp)
    if max(timestamps) - min(timestamps) > float(cfg.get("max_quote_skew_sec", 30)):
        raise ValueError("Pair quote timestamps are not aligned")
    return timestamps





def accept_sample(cfg: dict[str, Any], snap: PriceSnapshot, timestamps: list[float], state: AlertState) -> bool:
    interval = int(cfg.get("sample_interval_sec", 600))
    bucket = int(min(timestamps) // interval)
    samples = state.data.setdefault("samples", {})
    previous = samples.get(cfg["id"], {})
    if previous.get("interval") == interval:
        if bucket <= previous["bucket"] or any(t <= p for t, p in zip(timestamps, previous["timestamps"])):
            return False
        if bucket - previous["bucket"] > 1:
            logger.info("%s sample gap; warming a new contiguous window", cfg["label"])
            cfg["_calc"] = ZScoreCalculator(int(cfg["window_size"]), int(cfg.get("z_vol_window", 20)))
            cfg["_prev_z"] = None
    samples[cfg["id"]] = {"interval": interval, "bucket": bucket, "timestamps": timestamps}
    return True





def send_timeout(cfg: dict[str, Any], position: dict[str, Any], state: AlertState, notifier: TelegramNotifier) -> bool:
    hours = (utc_now() - parse_timestamp(position["entry_time"])).total_seconds() / 3600
    limit = float(cfg.get("max_holding_hours", 72))
    if hours < limit:
        return False
    signal = Signal("CLOSE", "CLOSE_" + position["direction"], "", "")
    if "legs" in cfg:
        a, b = (cfg["legs"][key]["name"] for key in ("a", "b"))
        signal.action = f"空 {a} / 多 {b}" if position.get("ratio_side") == "short_ratio" else f"多 {a} / 空 {b}"
    message = (f"🔵 [CLOSE] {cfg['label']} 超时结束跟踪提醒\n"
               "行情源：Variational\n"
               f"原因：ENTRY 信号已持续 {hours:.2f} 小时，达到 {limit:g} 小时上限。\n"
               f"方向：{action_label(signal)}\n"
               "无论盈亏都结束本次信号跟踪；若已跟随，请在实际交易平台自行核对两腿持仓、可成交报价及成本，再决定退出。\n"
               "此提醒不依赖实时行情；不代表你已成交或已经盈利。\n"
               "信号时间不代表你的实际开仓时间；机器人不会下单。")
    if notifier.send(message):
        state.clear_shadow_position(cfg["id"])
        state.mark_sent(entry_cooldown_key(cfg, position["direction"]))
        state.save()
    else:
        logger.warning("%s timeout notification not delivered; retaining position for retry", cfg["label"])
    return True


def pair_trading_hours(cfg, position, now):
    start = parse_timestamp(position["entry_time"])
    if all(leg["session"] == "always" for leg in cfg["legs"].values()):
        return (now - start).total_seconds() / 3600
    if "trading_seconds" in position:
        return position["trading_seconds"] / 3600
    return cfg["_calendar"].open_seconds(cfg["legs"], start, now) / 3600


def advance_pair_clock(cfg, position, now):
    previous = parse_timestamp(position.get("clock_time", position["entry_time"]))
    seconds = position.get("trading_seconds", 0.0)
    elapsed = (now - previous).total_seconds()
    if cfg.get("_clock_open", True):
        # Quote-based session overrides expire with live observations, not across restarts or long pauses.
        if cfg.get("_clock_override") and 0 <= elapsed <= cfg.get("max_quote_age_sec", 120):
            seconds += elapsed
        else:
            seconds += cfg["_calendar"].open_seconds(cfg["legs"], previous, now)
    position.update(trading_seconds=max(0.0, seconds), clock_time=now.isoformat())
    cfg["_clock_open"] = False
    cfg["_clock_override"] = False


def pair_close_signal(cfg, snap, position, z):
    if not position or not cfg.get("shadow_position_enabled", True):
        return None
    method, beta = position.get("method", "ratio"), position.get("beta", 1.0)
    current = spread_value(snap.base_price, snap.quote_price, method, beta)
    entry = position.get("entry_spread", position.get("entry_ratio"))
    side = position.get("ratio_side") or ratio_side(position["direction"])
    sign = 1 if side == "short_ratio" else -1
    move = (entry - current) * sign
    move_bps = move / entry * 10000 if method == "ratio" else move * 10000
    hours = pair_trading_hours(cfg, position, utc_now())
    details = {"move_bps": move_bps, "holding_hours": hours, "method": method, "beta": beta,
               "spread": current, "deferred": position.get("deferred_close", False)}
    reasons = []
    limit = cfg.get("max_holding_hours", 72) if all(leg["session"] == "always" for leg in cfg["legs"].values()) else cfg.get("max_holding_trading_hours", 46)
    if hours >= limit:
        reasons.append(f"超时：达到 {limit:g} 个{'自然' if all(leg['session'] == 'always' for leg in cfg['legs'].values()) else '交易'}小时上限")
    if position.get("reference_std", 0) > 0:
        mu, sigma = position["reference_mean"], position["reference_std"]
        reference_z = (current - mu) / sigma
        details["reference_z"] = reference_z
        boundary = mu + sign * sigma * position["reference_z_close"]
        if move > 0 and ((current <= boundary) if sign == 1 else (current >= boundary)):
            reasons.append("价差已回归 ENTRY 固定均值区间或越过该区间")
    max_z = cfg.get("entry_max_z", 0)
    if z is not None and max_z > 0 and z * sign > max_z and move < 0:
        reasons.append(f"风险提醒：价差继续恶化且滚动 |Z| 超过 {max_z:g}")
    if not reasons:
        return None
    a, b = (cfg["legs"][key]["name"] for key in ("a", "b"))
    label = f"空 {a} / 多 {b}" if sign == 1 else f"多 {a} / 空 {b}"
    return Signal("CLOSE", "CLOSE_" + position["direction"], label, "；".join(reasons), details=details)


def format_pair_message(cfg, snap, z, signal):
    details = signal.details
    closing = signal.level == "CLOSE"
    tier = " ·观察级·弱相关" if cfg.get("tier") == "observe" else ""
    beta = details.get("beta", 1.0)
    method = details.get("method", cfg["method"])
    lines = [f"{'🔵' if closing else '🟢'} [{signal.level}] {cfg['label']} {'结束跟踪' if closing else '偏离'}提醒{tier}",
             f"方向参考：{signal.action}（建议名义比 1 : {beta:.2f}，按 β；仅参考）",
             f"方法：{method} β={beta:.2f}｜窗口 {cfg['window_buckets']} 桶（每桶 {cfg.get('sample_interval_sec', 600)} 秒）"]
    if z is not None:
        lines.append(f"滚动 Z：{z:+.3f}（阈值 {cfg['z_open']:.2f}）")
    if closing:
        lines.append(f"原因：{signal.reason}")
        if "reference_z" in details:
            lines.append(f"相对 ENTRY 固定基准 Z：{details['reference_z']:+.3f}")
        lines.extend([f"信号方向上的价差变化：{details['move_bps']:+.2f} bps（不是持仓收益率）",
                      f"已跟踪：{details['holding_hours']:.2f} 个{'自然' if all(leg['session'] == 'always' for leg in cfg['legs'].values()) else '交易'}小时"])
        if details.get("deferred"):
            lines.append("（休市期间到期，开市后补发）")
    else:
        mu, sigma = details["reference_mean"], details["reference_std"]
        width = sigma * cfg.get("z_close", .35)
        lines.extend([f"价差 s：{details['spread']:.8f}｜冻结均值 {mu:.8f}",
                      f"回归区间 {mu - width:.8f} ～ {mu + width:.8f}（±{cfg.get('z_close', .35):g}σ）"])
    assets = snap.metadata["assets"]
    lines.append(f"中间价：{snap.base} {snap.base_price:,.4f}｜{snap.quote} {snap.quote_price:,.4f}（Variational {cfg.get('quote_size', 'size_1k')}）")
    lines.extend(format_variational_metadata(snap))
    for name, asset in assets.items():
        spread = asset.get("base_spread_bps")
        spread_text = f"{spread:.2f} bps" if spread is not None else "N/A"
        funding = asset.get("funding")
        funding_text = "N/A"
        if funding:
            if "annualized_rate" in funding:
                rate = funding["annualized_rate"]
                funding_text = f"年化 {rate:.2%}（正数多付空收，负数反向）"
            else:
                long_rate, short_rate = funding["annualized_long_rate"], funding["annualized_short_rate"]
                funding_text = f"年化 多{'付' if long_rate >= 0 else '收'} {abs(long_rate):.2%} / 空{'收' if short_rate >= 0 else '付'} {abs(short_rate):.2%}"
                payment = funding.get("next_timestamp")
                if isinstance(payment, (int, float)) and datetime.fromtimestamp(payment, timezone.utc).astimezone(ET).weekday() == 4:
                    funding_text += "（周五含周末，按接口费率）"
        lines.append(f"{name} 点差：{spread_text}；资金费 {funding_text}（仅展示）")
    close = cfg["_calendar"].next_close(cfg["legs"], utc_now())
    if close:
        lines.append(f"时段：两腿开市；距休市 {(close - utc_now()).total_seconds() / 3600:.1f} 小时（{close.astimezone(BEIJING):%m-%d %H:%M} 北京时间）")
    else:
        lines.append("时段：两腿有有效报价；以接口为准")
    limit = cfg.get("max_holding_hours", 72) if all(leg["session"] == "always" for leg in cfg["legs"].values()) else cfg.get("max_holding_trading_hours", 46)
    lines.extend([f"时间：{utc_now().astimezone(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间；最多跟踪 {limit:g} 个{'自然' if all(leg['session'] == 'always' for leg in cfg['legs'].values()) else '交易'}小时。",
                  "仅行情提醒，机器人不会下单；请在 Variational 自行核对报价与成本。"])
    return "\n".join(lines)


def pair_summary(cfg, status, ages="N/A"):
    cfg["_status"] = status
    bucket = int(utc_now().timestamp() // cfg.get("sample_interval_sec", 600))
    if cfg.get("_summary_bucket") == bucket:
        return
    cfg["_summary_bucket"] = bucket
    tickers = "/".join(cfg["legs"][key]["ticker"] for key in ("a", "b"))
    logger.info("%s %s state=%s quote_age=%s window=%s/%s", cfg["label"], tickers, status, ages,
                cfg["_calc"].count, cfg["window_buckets"])


def run_pair(cfg, state, notifier, force=False):
    ident, now = cfg["id"], utc_now()
    pending_entries = state.data.setdefault("pending_entries", {})
    quality = state.data.setdefault("quality", {}).setdefault(ident, {})
    position = state.shadow_position(ident)
    scheduled_open = cfg["_calendar"].pair_is_open(cfg["legs"], now)
    if position:
        advance_pair_clock(cfg, position, now)
        state.set_shadow_position(ident, position)
        hours = pair_trading_hours(cfg, position, now)
        limit = cfg.get("max_holding_hours", 72) if all(leg["session"] == "always" for leg in cfg["legs"].values()) else cfg.get("max_holding_trading_hours", 46)
        if all(leg["session"] == "always" for leg in cfg["legs"].values()) and hours >= limit:
            if cfg.get("shadow_mode"):
                logger.info("SHADOW %s timeout due after %.2f wall-clock hours", cfg["label"], hours)
            else:
                send_timeout(cfg, position, state, notifier)
            return
    try:
        snap = cfg["_source"].snapshot(cfg.get("pair", ident), cfg)
    except MarketDataUnavailable:
        raise
    except MarketDataError as exc:
        pending_entries.pop(ident, None)
        if str(exc) == "CLOSED_OR_NO_QUOTE":
            quality["closed"] = True
            if position and hours >= limit:
                position["deferred_close"] = True
                state.set_shadow_position(ident, position)
            pair_summary(cfg, "CLOSED_OR_NO_QUOTE")
            return
        raise
    try:
        timestamps = validate_snapshot(cfg, snap)
    except (ValueError, KeyError, TypeError) as exc:
        pending_entries.pop(ident, None)
        pair_summary(cfg, f"REJECTED ({exc})")
        return
    previous_times = state.data.setdefault("latest_quote_timestamps", {}).get(ident, [])
    if any(t < p for t, p in zip(timestamps, previous_times)):
        pair_summary(cfg, "REJECTED timestamp regression")
        return
    state.data["latest_quote_timestamps"][ident] = timestamps
    if not scheduled_open and quality.get("mismatch_day") != now.astimezone(ET).date().isoformat():
        logger.info("%s session table mismatch: fresh quotes accepted", cfg["label"])
        quality["mismatch_day"] = now.astimezone(ET).date().isoformat()
    interval = cfg.get("sample_interval_sec", 600)
    bucket = int(min(timestamps) // interval)
    previous = state.data.setdefault("samples", {}).get(ident, {})
    candidate = previous.get("bucket", -1) < bucket and all(t > p for t, p in zip(timestamps, previous.get("timestamps", [])))
    calc = cfg["_calc"]
    prev_z = cfg.get("_prev_z")
    pending = pending_entries.pop(ident, None)
    if position:
        pending = None
    if candidate:
        anchor = quality.get("checked_bucket", previous.get("bucket"))
        missing, closed = cfg["_calendar"].gap(cfg["legs"], anchor, bucket, interval) if anchor is not None else (0, False)
        observed_closed = quality.pop("closed", False)
        if closed or observed_closed:
            quality["grace"] = cfg.get("reopen_grace_buckets", 2)
        if missing and missing + 1 > cfg.get("max_open_gap_buckets", 3):
            logger.info("%s open sample gap; reset statistics", cfg["label"])
            calc = cfg["_calc"] = SpreadCalculator(cfg["window_buckets"], cfg.get("z_vol_window", 20),
                                                   method=cfg["method"], beta=cfg.get("beta"))
            cfg["_prev_z"], prev_z, pending = None, None, None
        stale = False
        new_bucket = quality.get("checked_bucket") != bucket
        if new_bucket or quality.get("stale"):
            for key, value in (("a", snap.base_price), ("b", snap.quote_price)):
                leg = quality.setdefault(key, {})
                if leg.get("mid") != value:
                    leg["flat_count"] = 1
                elif new_bucket:
                    leg["flat_count"] = leg.get("flat_count", 0) + 1
                leg["mid"] = value
                stale |= cfg.get("flat_buckets", 3) > 0 and leg["flat_count"] >= cfg.get("flat_buckets", 3)
            quality["checked_bucket"] = bucket
            quality["stale"] = stale
        if quality.get("stale"):
            pair_summary(cfg, "STALE")
            return
    elif quality.get("stale"):
        pair_summary(cfg, "STALE")
        return
    cfg["_last_valid_quote"] = min(timestamps)
    if position:
        cfg["_clock_open"] = True
        cfg["_clock_override"] = not scheduled_open
        state.set_shadow_position(ident, position)
    if candidate:
        beta = calc.current_beta(bucket)
        values = calc.values(beta, bucket)
        previous_spread = values[-1] if values else None
        z, reference, beta = calc.add({"bucket": bucket, "ts": min(timestamps), "a_mid": snap.base_price, "b_mid": snap.quote_price})
        state.data["samples"][ident] = {"interval": interval, "bucket": bucket, "timestamps": timestamps}
        state.set_history(ident, calc.dump_history())
        cfg["_prev_z"] = z
        state.data.setdefault("statistics", {})[ident] = {
            "prev_z": z, "z_history": list(calc.z_history), "signature": cfg["_signature"], "model": cfg["_model"]}
        grace = quality.get("grace", 0) > 0
        quality["grace"] = max(0, quality.get("grace", 0) - 1)
    else:
        z, reference, beta = calc.evaluate(snap.base_price, snap.quote_price, bucket=bucket)
        previous_spread, grace = None, quality.get("grace", 0) > 0
    ages = " / ".join(f"{now.timestamp() - t:.0f}s" for t in timestamps)
    pair_summary(cfg, "OPEN", ages)
    close = pair_close_signal(cfg, snap, position, z)
    if close:
        key = f"{ident}:{close.direction}:CLOSE"
        if cfg.get("shadow_mode"):
            logger.info("SHADOW %s", format_pair_message(cfg, snap, z, close))
        elif force or state.should_send(key, cfg.get("close_cooldown_sec", 300)):
            if notifier.send(format_pair_message(cfg, snap, z, close)):
                state.mark_sent(key)
                state.mark_sent(entry_cooldown_key(cfg, position["direction"]))
                state.clear_shadow_position(ident)
        return
    if position or grace:
        return
    z_vol = calc.z_volatility
    if not candidate:
        if not pending or not 0 <= utc_now().timestamp() - pending["quote_time"] <= cfg.get("max_quote_age_sec", 120):
            return
        beta = pending["beta"]
        reference = (pending["reference_mean"], pending["reference_std"])
        current = spread_value(snap.base_price, snap.quote_price, pending["method"], beta)
        z = (current - reference[0]) / reference[1]
        prev_z, z_vol, previous_spread = pending["prev_z"], pending["z_vol"], pending["previous_spread"]
    if z is None or not reference or not reference[1]:
        return
    signal = build_signal(cfg, z, prev_z, z_vol)
    current = spread_value(snap.base_price, snap.quote_price, cfg["method"], beta)
    if not signal or (not candidate and signal.direction != pending["direction"]):
        return
    if cfg.get("entry_require_cross") and (previous_spread is None or (current - previous_spread) * (current - reference[0]) >= 0):
        return
    signal.details.update(method=cfg["method"], beta=beta, spread=current,
                          reference_mean=reference[0], reference_std=reference[1])
    if cfg.get("shadow_mode"):
        logger.info("SHADOW %s", format_pair_message(cfg, snap, z, signal))
        return
    key = entry_cooldown_key(cfg, signal.direction)
    if not force and not state.should_send(key, cfg.get("cooldown_sec", 1800)):
        return
    pending_entries[ident] = {**signal.details, "direction": signal.direction,
        "quote_time": min(timestamps) if candidate else pending["quote_time"],
        "prev_z": prev_z, "z_vol": z_vol, "previous_spread": previous_spread}
    state.save()
    if notifier.send(format_pair_message(cfg, snap, z, signal)):
        pending_entries.pop(ident, None)
        state.mark_sent(key)
        if cfg.get("shadow_position_enabled", True):
            state.set_shadow_position(ident, {
                **signal.details, "direction": signal.direction, "ratio_side": ratio_side(signal.direction),
                "entry_spread": current, "entry_ratio": snap.ratio, "entry_z": z,
                "reference_z_close": cfg.get("z_close", .35), "entry_time": utc_now().isoformat(),
                "trading_seconds": 0.0, "clock_time": utc_now().isoformat()})
            cfg["_clock_open"] = True
            cfg["_clock_override"] = not scheduled_open
        state.save()


def run_once(monitors: list[dict[str, Any]], state: AlertState, notifier: TelegramNotifier, force: bool = False) -> None:
    failures = []
    for source in {id(cfg["_source"]): cfg["_source"] for cfg in monitors}.values():
        if isinstance(source, VariationalV2Source):
            source.begin_cycle()
    for cfg in monitors:
        cfg["_cycle_error"] = False
        try:
            if "legs" in cfg:
                run_pair(cfg, state, notifier, force)
                continue
            pending_entries = state.data.setdefault("pending_entries", {})
            position = state.shadow_position(cfg["id"])
            if position:
                pending_entries.pop(cfg["id"], None)
            if position and send_timeout(cfg, position, state, notifier):
                continue
            snap = cfg["_source"].snapshot(cfg["pair"], cfg)
            try:
                timestamps = validate_snapshot(cfg, snap)
            except (ValueError, KeyError, TypeError) as exc:
                pending_entries.pop(cfg["id"], None)
                logger.warning("%s quote rejected: %s", cfg["label"], exc)
                continue
            latest_quotes = state.data.setdefault("latest_quote_timestamps", {})
            previous_timestamps = latest_quotes.get(
                cfg["id"], state.data.get("samples", {}).get(cfg["id"], {}).get("timestamps", [])
            )
            if any(current < previous for current, previous in zip(timestamps, previous_timestamps)):
                logger.warning("%s quote timestamp regression: current=%s previous=%s; ignored",
                               cfg["label"], timestamps, previous_timestamps)
                continue
            # Track valid intrabucket quotes too, independently of statistical sampling.
            latest_quotes[cfg["id"]] = timestamps
            pending = pending_entries.pop(cfg["id"], None)
            accepted = accept_sample(cfg, snap, timestamps, state)
            calc: ZScoreCalculator = cfg["_calc"]
            previous_ratio = calc.ratios[-1] if calc.ratios else None
            target_ratio = mean(calc.ratios) if calc.count >= int(cfg["window_size"]) else None
            target_std = stdev(calc.ratios) if target_ratio is not None else None
            prev_z = cfg.get("_prev_z")
            z = calc.add(snap.ratio) if accepted else None
            if accepted:
                state.set_history(cfg["id"], calc.dump_history())
                cfg["_prev_z"] = z
            if calc.count < int(cfg["window_size"]):
                z = None
            elif not accepted:
                sigma = stdev(calc.ratios)
                deviation = snap.ratio - mean(calc.ratios)
                z = deviation / sigma if sigma else (0.0 if deviation == 0 else None)
            z_vol = calc.z_volatility
            close_signal = build_close_signal(cfg, snap, z, position)
            if close_signal:
                key = f"{cfg['id']}:{close_signal.direction}:{close_signal.level}"
                if force or state.should_send(key, int(cfg.get("close_cooldown_sec", 300))):
                    if notifier.send(format_message(cfg, snap, z, z_vol, close_signal)):
                        state.mark_sent(key)
                        state.mark_sent(f"{cfg['id']}:{position['direction']}:ENTRY")
                        state.clear_shadow_position(cfg["id"])
                        state.save()
                continue
            if position:
                logger.info("%s shadow position active; ratio=%.8f z=%s", cfg["label"], snap.ratio, z)
                continue
            if not accepted and pending:
                age = utc_now().timestamp() - pending["quote_time"]
                if not 0 <= age <= float(cfg.get("max_quote_age_sec", 120)):
                    logger.info("%s pending ENTRY expired; awaiting a new signal", cfg["label"])
                    continue
                prev_z = pending["prev_z"]
                z_vol = pending["z_vol"]
                previous_ratio = pending["previous_ratio"]
                target_ratio = pending["reference_mean"]
                target_std = pending["reference_std"]
            if (not accepted and not pending) or z is None or target_ratio is None:
                logger.info("%s awaiting fresh/full samples: %s/%s", cfg["label"], calc.count, cfg["window_size"])
                continue
            signal = build_signal(cfg, z, prev_z, z_vol)
            if not signal or not signal.tradeable:
                logger.info("%s no signal: ratio=%.8f z=%+.3f", cfg["label"], snap.ratio, z)
                continue
            if not accepted and signal.direction != pending["direction"]:
                logger.info("%s pending ENTRY direction no longer valid", cfg["label"])
                continue
            if cfg.get("entry_require_cross", False) and (snap.ratio - previous_ratio) * (snap.ratio - target_ratio) >= 0:
                logger.info("%s entry filtered: ratio has not moved toward prior mean", cfg["label"])
                continue
            if not target_std:
                logger.info("%s entry filtered: reference window has no variance", cfg["label"])
                continue
            signal.details.update(reference_mean=target_ratio, reference_std=target_std)
            key = f"{cfg['id']}:{signal.direction}:{signal.level}"
            cooldown = int(cfg.get("cooldown_sec", 1800))
            if force or state.should_send(key, cooldown):
                candidate = make_shadow_position(cfg, signal, snap, z, reference_mean=target_ratio, reference_std=target_std)
                # Keep the original observation's deadline; fresh quotes cannot extend a failed signal indefinitely.
                pending_entries[cfg["id"]] = {
                    "direction": signal.direction,
                    "quote_time": min(timestamps) if accepted else pending["quote_time"],
                    "prev_z": prev_z, "z_vol": z_vol, "previous_ratio": previous_ratio,
                    "reference_mean": target_ratio, "reference_std": target_std,
                }
                state.save()
                if notifier.send(format_message(cfg, snap, z, z_vol, signal)):
                    candidate["entry_time"] = utc_now().isoformat()
                    pending_entries.pop(cfg["id"], None)
                    state.mark_sent(key)
                    if cfg.get("shadow_position_enabled", True):
                        state.set_shadow_position(cfg["id"], candidate)
                    state.save()
                else:
                    logger.warning("%s entry notification not delivered; retained for revalidation and retry", cfg["label"])
            else:
                logger.info("%s signal suppressed by cooldown: %s %s", cfg["label"], signal.direction, signal.level)
        except MarketDataError as exc:
            cfg["_cycle_error"] = True
            if "legs" in cfg:
                pair_summary(cfg, f"DATA_ERROR ({exc})")
            else:
                logger.warning("%s market data rejected: %s; will retry", cfg["label"], exc)
        except requests.RequestException as exc:
            cfg["_cycle_error"] = True
            logger.warning("%s network request failed (%s); will retry", cfg["label"], type(exc).__name__)
        except Exception as exc:
            cfg["_cycle_error"] = True
            logger.exception("Monitor failed for %s: %s", cfg.get("label", cfg.get("id")), exc)
            failures.append(exc)
    state.save()
    if failures:
        raise RuntimeError(f"{len(failures)} monitor(s) failed unexpectedly") from failures[0]


class BotHealth:
    def __init__(self, state):
        self.state = state
        self.data = state.data.setdefault("health", {})
        self.failures = 0
        self.previous_time = None
        self.last_quote = None
        self.open_without_quote = 0.0

    def _send(self, notifier, message):
        try:
            return notifier.send(message)
        except requests.RequestException as exc:
            logger.warning("Health notification failed (%s); will retry", type(exc).__name__)
            return False

    def tick(self, monitors, notifier, cycle_failed=False):
        live = [cfg for cfg in monitors if not cfg.get("shadow_mode", False)]
        if not live:
            return
        now = utc_now()
        self.failures = self.failures + 1 if cycle_failed or any(cfg.get("_cycle_error") for cfg in live) else 0
        quotes = [cfg["_last_valid_quote"] for cfg in live if cfg.get("_last_valid_quote") is not None]
        latest = max(quotes) if quotes else None
        if latest is not None and (self.last_quote is None or latest > self.last_quote):
            self.last_quote = latest
            self.open_without_quote = 0
        elif self.previous_time is not None:
            durations = [cfg["_calendar"].open_seconds(cfg["legs"], self.previous_time, now)
                         if "legs" in cfg else (now - self.previous_time).total_seconds() for cfg in live]
            self.open_without_quote += max(durations, default=0)
        self.previous_time = now
        due = self.failures >= 5 or self.open_without_quote >= 1800
        if due and now.timestamp() - self.data.get("degraded_sent", 0) >= 6 * 3600:
            message = (f"⚠️ bot degraded：连续失败 {self.failures} 轮；开市无有效报价 {self.open_without_quote / 60:.1f} 分钟。\n"
                       f"{now.astimezone(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间；请检查日志。")
            if self._send(notifier, message):
                self.data["degraded_sent"] = now.timestamp()
        local = now.astimezone(BEIJING)
        day = local.date().isoformat()
        if local.hour >= 9 and self.data.get("heartbeat_day") != day:
            lines = [f"💓 行情 bot 每日心跳｜{local:%Y-%m-%d %H:%M} 北京时间"]
            for cfg in monitors:
                age = now.timestamp() - cfg["_last_valid_quote"] if cfg.get("_last_valid_quote") is not None else None
                age_text = f"{age:.0f}s" if age is not None else "N/A"
                status = cfg.get("_status", "N/A")
                tracking = "跟踪中" if self.state.shadow_position(cfg["id"]) else "无跟踪"
                lines.append(f"{cfg['label']}：窗口 {cfg['_calc'].count}/{cfg.get('window_buckets', cfg.get('window_size'))}；"
                             f"最后报价 age {age_text}；{status}；{tracking}")
            if self._send(notifier, "\n".join(lines)):
                self.data["heartbeat_day"] = day
        self.state.save()


class SecretFormatter(logging.Formatter):
    def format(self, record):
        result = super().format(record)
        for key in ("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
            secret = os.getenv(key)
            if secret:
                result = result.replace(secret, "<redacted>")
        return re.sub(r"(api\.telegram\.org/bot)[^/\s]+", r"\1<redacted>", result)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, timezone.utc).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def configure_logging(level: str) -> None:
    directory = ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    formatter = SecretFormatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    rotating = RotatingFileHandler(directory / "alert-bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    for handler in (stream, rotating):
        handler.setFormatter(formatter)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=[stream, rotating], force=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mean-reversion Telegram alert bot")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--test-notify", action="store_true", help="Send a Telegram test message and exit")
    parser.add_argument("--force-alert", action="store_true", help="Ignore cooldown for this run")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    configure_logging("INFO")
    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
    except (OSError, ValueError):
        logger.exception("Cannot load configuration %s; refusing to start", config_path)
        return 1
    logging.getLogger().setLevel(getattr(logging, str(config.get("log_level", "INFO")).upper(), logging.INFO))

    notifier = TelegramNotifier()
    if args.test_notify:
        try:
            delivered = notifier.send("Mean reversion alert bot test: TG push is working. No orders will ever be placed.")
        except requests.RequestException as exc:
            logger.error("Telegram self-test failed (%s)", type(exc).__name__)
            return 1
        if not delivered:
            logger.error("Telegram self-test failed: message was not delivered")
            return 1
        return 0

    state_path = Path(config.get("state_file", "state/alert_state.json"))
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    state = AlertState(state_path)
    try:
        monitors = init_monitors(config, state)
    except (ValueError, requests.RequestException) as exc:
        logger.error("Startup validation failed: %s", exc)
        return 1
    if not monitors:
        logger.error("No enabled monitors in %s", config_path)
        return 1

    logger.info("Started %s monitor(s). This bot is alert-only and never places orders.", len(monitors))
    health = BotHealth(state)
    while True:
        failed = False
        try:
            run_once(monitors, state, notifier, force=args.force_alert)
            failed = any(cfg.get("_cycle_error", False) for cfg in monitors)
        except Exception:
            failed = True
            logger.exception("Polling cycle failed; keeping bot alive for the next cycle")
        try:
            if not args.once:
                health.tick(monitors, notifier, cycle_failed=failed)
        except Exception:
            logger.exception("Bot health update failed; will retry next cycle")
        if args.once:
            return 1 if failed else 0
        time.sleep(float(config.get("check_interval_sec", 30)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped.")
        raise SystemExit(130)
