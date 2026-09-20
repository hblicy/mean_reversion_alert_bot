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
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from math import isfinite
from statistics import mean, stdev
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

logger = logging.getLogger("alert_bot")


class MarketDataError(ValueError):
    pass


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
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
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
        data = response.json()
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


class BinanceFuturesSource(HttpSource):
    name = "binance_futures"
    url = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

    def _mid(self, symbol: str) -> float:
        data = self._get_json(self.url, {"symbol": symbol})
        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Bad Binance BBO for {symbol}: bid={bid}, ask={ask}")
        return (bid + ask) / 2

    def snapshot(self, pair: str, cfg: dict[str, Any] | None = None) -> PriceSnapshot:
        if pair != "BTC_ETH":
            raise ValueError(f"{self.name} supports BTC_ETH only")
        btc = self._mid("BTCUSDT")
        eth = self._mid("ETHUSDT")
        return PriceSnapshot("ETH", "BTC", eth, btc, eth / btc, self.name, utc_now())


class OkxSwapSource(HttpSource):
    name = "okx_swap"
    url = "https://www.okx.com/api/v5/market/ticker"

    def _mid(self, inst_id: str) -> float:
        data = self._get_json(self.url, {"instId": inst_id})
        rows = data.get("data") or []
        if not rows:
            raise ValueError(f"Empty OKX ticker for {inst_id}: {data}")
        row = rows[0]
        bid = float(row["bidPx"])
        ask = float(row["askPx"])
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Bad OKX BBO for {inst_id}: bid={bid}, ask={ask}")
        return (bid + ask) / 2

    def snapshot(self, pair: str, cfg: dict[str, Any] | None = None) -> PriceSnapshot:
        if pair != "BTC_ETH":
            raise ValueError(f"{self.name} supports BTC_ETH only")
        btc = self._mid("BTC-USDT-SWAP")
        eth = self._mid("ETH-USDT-SWAP")
        return PriceSnapshot("ETH", "BTC", eth, btc, eth / btc, self.name, utc_now())


class YahooSilverBinanceBtcSource(HttpSource):
    name = "yahoo_silver_binance_btc"
    binance_url = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
    yahoo_url = "https://query1.finance.yahoo.com/v8/finance/chart/SI=F"

    def _btc_mid(self) -> float:
        data = self._get_json(self.binance_url, {"symbol": "BTCUSDT"})
        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Bad Binance BTC BBO: bid={bid}, ask={ask}")
        return (bid + ask) / 2

    def _silver_price(self) -> float:
        data = self._get_json(self.yahoo_url, {"range": "1d", "interval": "1m"})
        result = (data.get("chart", {}).get("result") or [None])[0]
        if not result:
            raise ValueError(f"Empty Yahoo silver response: {data}")
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            price = next((x for x in reversed(closes) if x is not None), None)
        price = float(price or 0)
        if price <= 0:
            raise ValueError("Bad Yahoo silver price")
        return price

    def snapshot(self, pair: str, cfg: dict[str, Any] | None = None) -> PriceSnapshot:
        if pair != "XAG_BTC":
            raise ValueError(f"{self.name} supports XAG_BTC only")
        btc = self._btc_mid()
        silver = self._silver_price()
        return PriceSnapshot("BTC", "XAG", btc, silver, btc / silver, self.name, utc_now())


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


SOURCES = {
    BinanceFuturesSource.name: BinanceFuturesSource,
    OkxSwapSource.name: OkxSwapSource,
    YahooSilverBinanceBtcSource.name: YahooSilverBinanceBtcSource,
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
        return json.load(fh)


def init_monitors(config: dict[str, Any], state: AlertState) -> list[dict[str, Any]]:
    monitors = []
    for item in config.get("pairs", []):
        if not item.get("enabled", True):
            continue
        source_name = item["source"]
        if source_name not in SOURCES:
            raise ValueError(f"Unknown source {source_name}. Available: {', '.join(SOURCES)}")
        item = dict(item)
        item["_source"] = SOURCES[source_name]()
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
    labels = {
        "LONG_BTC_SHORT_ETH": "多 BTC / 空 ETH",
        "SHORT_BTC_LONG_ETH": "空 BTC / 多 ETH（仅观察）",
        "SHORT_BTC_LONG_XAG": "空 BTC / 多 XAG",
        "LONG_BTC_SHORT_XAG": "多 BTC / 空 XAG",
        "SHORT_BZ_LONG_CL": "空 BZ / 多 CL",
        "LONG_BZ_SHORT_CL": "多 BZ / 空 CL",
        "SHORT_XAG_LONG_XAU": "空 XAG / 多 XAU",
        "LONG_XAG_SHORT_XAU": "多 XAG / 空 XAU",
        "CLOSE_SHORT_XAG_LONG_XAU": "平空 XAG / 平多 XAU",
        "CLOSE_LONG_XAG_SHORT_XAU": "平多 XAG / 平空 XAU",
        "CLOSE_LONG_BTC_SHORT_ETH": "平多 BTC / 平空 ETH",
        "CLOSE_SHORT_BTC_LONG_ETH": "平空 BTC / 平多 ETH",
        "CLOSE_SHORT_BTC_LONG_XAG": "平空 BTC / 平多 XAG",
        "CLOSE_LONG_BTC_SHORT_XAG": "平多 BTC / 平空 XAG",
        "CLOSE_SHORT_BZ_LONG_CL": "平空 BZ / 平多 CL",
        "CLOSE_LONG_BZ_SHORT_CL": "平多 BZ / 平空 CL",
    }
    return labels.get(signal.direction, signal.action)


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
            direction = "LONG_RATIO"
        return Signal("ENTRY", direction, "", f"比率高位 z={z:+.3f}", True)
    if z <= -z_open:
        if cfg["pair"] == "XAG_BTC":
            direction = "LONG_BTC_SHORT_XAG"
        elif cfg["pair"] == "BZ_CL":
            direction = "LONG_BZ_SHORT_CL"
        elif cfg["pair"] == "XAG_XAU":
            direction = "LONG_XAG_SHORT_XAU"
        else:
            direction = "SHORT_RATIO"
        return Signal("ENTRY", direction, "", f"比率低位 z={z:+.3f}", True)
    return None


def ratio_side(direction: str) -> str:
    if direction in ("LONG_BTC_SHORT_ETH", "SHORT_BTC_LONG_XAG", "SHORT_BZ_LONG_CL", "SHORT_XAG_LONG_XAU"):
        return "short_ratio"
    if direction in ("LONG_BTC_SHORT_XAG", "SHORT_BTC_LONG_ETH", "LONG_BZ_SHORT_CL", "LONG_XAG_SHORT_XAU"):
        return "long_ratio"
    return ""


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
    message = (f"🔵 [CLOSE] {cfg['label']} 超时结束跟踪提醒\n"
               "行情源：Variational\n"
               f"原因：ENTRY 信号已持续 {hours:.2f} 小时，达到 {limit:g} 小时上限。\n"
               f"方向：{action_label(signal)}\n"
               "无论盈亏都结束本次信号跟踪；若已跟随，请在实际交易平台自行核对两腿持仓、可成交报价及成本，再决定退出。\n"
               "此提醒不依赖实时行情；不代表你已成交或已经盈利。\n"
               "信号时间不代表你的实际开仓时间；机器人不会下单。")
    if notifier.send(message):
        state.clear_shadow_position(cfg["id"])
        state.mark_sent(f"{cfg['id']}:{position['direction']}:ENTRY")
        state.save()
    else:
        logger.warning("%s timeout notification not delivered; retaining position for retry", cfg["label"])
    return True


def run_once(monitors: list[dict[str, Any]], state: AlertState, notifier: TelegramNotifier, force: bool = False) -> None:
    failures = []
    for cfg in monitors:
        try:
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
            logger.warning("%s market data rejected: %s; will retry", cfg["label"], exc)
        except requests.RequestException as exc:
            logger.warning("%s network request failed (%s); will retry", cfg["label"], type(exc).__name__)
        except Exception as exc:
            logger.exception("Monitor failed for %s: %s", cfg.get("label", cfg.get("id")), exc)
            failures.append(exc)
    state.save()
    if failures:
        raise RuntimeError(f"{len(failures)} monitor(s) failed unexpectedly") from failures[0]


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mean-reversion Telegram alert bot")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--test-notify", action="store_true", help="Send a Telegram test message and exit")
    parser.add_argument("--force-alert", action="store_true", help="Ignore cooldown for this run")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    configure_logging(str(config.get("log_level", "INFO")))

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
    monitors = init_monitors(config, state)
    if not monitors:
        logger.error("No enabled monitors in %s", config_path)
        return 1

    logger.info("Started %s monitor(s). This bot is alert-only and never places orders.", len(monitors))
    while True:
        run_once(monitors, state, notifier, force=args.force_alert)
        if args.once:
            return 0
        time.sleep(int(config.get("check_interval_sec", 10)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped.")
        raise SystemExit(130)
