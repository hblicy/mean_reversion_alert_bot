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
from statistics import mean, stdev
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

logger = logging.getLogger("alert_bot")


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
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load state file %s: %s", self.path, exc)
            self.data = {"alerts": {}, "history": {}}

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
            self.data["alerts"][key] = now
            return True
        return False

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
            print(message)
            print("-" * 60)
        if not self.token or not self.chat_id:
            logger.info("Telegram env is not configured; message printed locally only.")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "disable_web_page_preview": True}
        response = requests.post(url, json=payload, timeout=15)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram send failed: {data}")
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
        self.__class__._cache_data = data
        self.__class__._cache_ts = now
        return data

    def _listing(self, ticker: str) -> dict[str, Any]:
        wanted = ticker.upper()
        for item in self._stats().get("listings", []):
            if str(item.get("ticker", "")).upper() == wanted:
                return item
        raise ValueError(f"Variational listing not found: {ticker}")

    def _price(self, ticker: str, quote_size: str) -> tuple[float, dict[str, Any]]:
        listing = self._listing(ticker)
        quotes = listing.get("quotes") or {}
        quote = quotes.get(quote_size) or {}
        bid = _safe_float(quote.get("bid"))
        ask = _safe_float(quote.get("ask"))
        price_source = "quote"
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2
        else:
            price = _safe_float(listing.get("mark_price"))
            price_source = "mark_price"
        if price <= 0:
            raise ValueError(f"Bad Variational price for {ticker}")

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
            "funding_rate": _safe_float(listing.get("funding_rate")),
            "funding_interval_s": int(_safe_float(listing.get("funding_interval_s"))),
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
        item["_calc"] = ZScoreCalculator(
            int(item["window_size"]),
            int(item.get("z_vol_window", 20)),
            state.history(item["id"]),
        )
        item["_prev_z"] = None
        monitors.append(item)
    return monitors


def signal_emoji(signal: Signal) -> str:
    if signal.level == "CLOSE":
        return "🔵"
    if signal.level == "ENTRY":
        return "🟢"
    return "🟡"


def format_message(cfg: dict[str, Any], snap: PriceSnapshot, z: float, z_vol: float | None, signal: Signal) -> str:
    if signal.level == "CLOSE":
        return format_close_message(cfg, snap, z, signal)

    status = "平仓提醒" if signal.level == "CLOSE" else "可评估" if signal.tradeable else "观察/过滤"
    lines = [
        f"{signal_emoji(signal)} [{signal.level}] {cfg['label']} {status}",
        f"方向：{action_label(signal)}",
        f"Z：{z:+.3f}（开仓 {float(cfg['z_open']):+.2f}，预警 {float(cfg.get('z_watch', cfg['z_open'])):+.2f}）",
        f"{snap.base}/{snap.quote}：{snap.ratio:.8f}",
        f"价格：{snap.base} ${snap.base_price:,.4f}｜{snap.quote} ${snap.quote_price:,.4f}",
    ]
    if signal.level == "CLOSE" and signal.reason:
        lines.append(f"触发：{signal.reason}")

    ops = suggested_operations(signal, snap)
    if ops:
        lines.extend(["", "建议操作：", *ops])

    estimate = format_entry_estimate(cfg, snap)
    if estimate:
        lines.extend(["", *estimate])

    if snap.source == VariationalMetadataSource.name:
        lines.extend(["", *format_variational_metadata(snap)])

    if signal.caution:
        lines.extend(["", f"注意：{signal.caution}"])

    lines.append("")
    lines.append("只提醒不下单；手动确认 Variational 盘口。")
    return "\n".join(lines)


def format_close_message(cfg: dict[str, Any], snap: PriceSnapshot, z: float, signal: Signal) -> str:
    lines = [
        f"🔵 [CLOSE] {cfg['label']} 平仓信号",
        "",
        f"原因：{signal.reason or '达到盈利目标'}",
        f"{snap.base}/{snap.quote} z-score：{z:+.3f}",
        f"{snap.base}/{snap.quote}：{snap.ratio:.8f}",
        "",
        "建议：市价平仓全部对冲头寸",
        "⚠️ 提醒：两边都要平，别漏单边。",
    ]
    estimate = format_close_estimate(cfg, signal)
    if estimate:
        lines.extend(["", *estimate])
    return "\n".join(lines)


def suggested_operations(signal: Signal, snap: PriceSnapshot) -> list[str]:
    if signal.direction == "CLOSE_LONG_BTC_SHORT_ETH":
        return [
            f"• 平 BTC 多单 ≈ ${snap.quote_price:,.2f}",
            f"• 回补 ETH 空单 ≈ ${snap.base_price:,.2f}",
        ]
    if signal.direction == "CLOSE_SHORT_BTC_LONG_ETH":
        return [
            f"• 回补 BTC 空单 ≈ ${snap.quote_price:,.2f}",
            f"• 平 ETH 多单 ≈ ${snap.base_price:,.2f}",
        ]
    if signal.direction == "CLOSE_SHORT_BTC_LONG_XAG":
        return [
            f"• 回补 BTC 空单 ≈ ${snap.base_price:,.2f}",
            f"• 平 XAG 多单 ≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "CLOSE_LONG_BTC_SHORT_XAG":
        return [
            f"• 平 BTC 多单 ≈ ${snap.base_price:,.2f}",
            f"• 回补 XAG 空单 ≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "CLOSE_SHORT_BZ_LONG_CL":
        return [
            f"• 回补 BZ-PERP ≈ ${snap.base_price:,.4f}",
            f"• 平 CL-PERP 多单 ≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "CLOSE_LONG_BZ_SHORT_CL":
        return [
            f"• 平 BZ-PERP 多单 ≈ ${snap.base_price:,.4f}",
            f"• 回补 CL-PERP ≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "LONG_BTC_SHORT_ETH":
        return [
            f"• LONG BTC-PERP（市价）≈ ${snap.quote_price:,.2f}",
            f"• SHORT ETH-PERP（市价）≈ ${snap.base_price:,.2f}",
        ]
    if signal.direction == "SHORT_BTC_LONG_ETH":
        return [
            f"• SHORT BTC-PERP（仅观察）≈ ${snap.quote_price:,.2f}",
            f"• LONG ETH-PERP（仅观察）≈ ${snap.base_price:,.2f}",
        ]
    if signal.direction == "SHORT_BTC_LONG_XAG":
        return [
            f"• SHORT BTC-PERP（市价）≈ ${snap.base_price:,.2f}",
            f"• LONG XAG-PERP（市价）≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "LONG_BTC_SHORT_XAG":
        return [
            f"• LONG BTC-PERP（市价）≈ ${snap.base_price:,.2f}",
            f"• SHORT XAG-PERP（市价）≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "SHORT_BZ_LONG_CL":
        return [
            f"• SHORT BZ-PERP（市价）≈ ${snap.base_price:,.4f}",
            f"• LONG CL-PERP（市价）≈ ${snap.quote_price:,.4f}",
        ]
    if signal.direction == "LONG_BZ_SHORT_CL":
        return [
            f"• LONG BZ-PERP（市价）≈ ${snap.base_price:,.4f}",
            f"• SHORT CL-PERP（市价）≈ ${snap.quote_price:,.4f}",
        ]
    return []


def format_variational_metadata(snap: PriceSnapshot) -> list[str]:
    assets = snap.metadata.get("assets", {})
    if not assets:
        return []

    quote_size = snap.metadata.get("quote_size", "N/A")
    ages = []
    fundings = []
    for ticker, meta in assets.items():
        age = meta.get("quote_age_sec")
        age_text = f"{age:.0f}s" if age is not None else "N/A"
        ages.append(f"{ticker} {age_text}")
        fundings.append(f"{ticker} {meta.get('funding_rate', 0.0):.6f}")

    return [
        f"报价：{quote_size}｜age {' / '.join(ages)}",
        f"资金费率：{'｜'.join(fundings)}",
    ]


def position_size_usd(cfg: dict[str, Any]) -> float:
    return float(cfg.get("position_size_usd", 1000))


def one_way_bbo_cost_pct(snap: PriceSnapshot) -> float:
    assets = snap.metadata.get("assets")
    if not assets:
        raise ValueError(f"Missing BBO metadata for {snap.base}/{snap.quote}")

    cost_pct = 0.0
    for ticker in (snap.base, snap.quote):
        asset = assets.get(ticker)
        if not asset:
            raise ValueError(f"Missing BBO metadata for {ticker}")
        bid = float(asset["bid"])
        ask = float(asset["ask"])
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Bad BBO for {ticker}: bid={bid}, ask={ask}")
        cost_pct += (ask - bid) / (ask + bid) * 100
    return cost_pct


def market_slippage_tolerance_pct(cfg: dict[str, Any]) -> float:
    return float(cfg["market_slippage_tolerance_pct"])


def format_entry_estimate(cfg: dict[str, Any], snap: PriceSnapshot) -> list[str]:
    size = position_size_usd(cfg)
    target_pct = float(cfg.get("close_profit_ratio_pct", 0.5))
    entry_bbo_cost_pct = one_way_bbo_cost_pct(snap)
    estimated_round_trip_bbo_pct = entry_bbo_cost_pct * 2
    slippage_pct = market_slippage_tolerance_pct(cfg)
    required_move_pct = target_pct + estimated_round_trip_bbo_pct + slippage_pct
    lines = [
        f"预估（每腿 ${size:,.0f}）：",
        f"• 净利润目标：{target_pct:.2f}% ≈ ${size * target_pct / 100:,.2f}",
        f"• 当前开平点差估算：{estimated_round_trip_bbo_pct:.4f}% ≈ ${size * estimated_round_trip_bbo_pct / 100:,.2f}",
        f"• 整笔开平滑点缓冲：{slippage_pct:.2f}% ≈ ${size * slippage_pct / 100:,.2f}",
        f"• 预计平仓所需有利移动：{required_move_pct:.4f}%",
    ]
    return lines


def format_close_estimate(cfg: dict[str, Any], signal: Signal) -> list[str]:
    move_pct = signal.details.get("move_pct")
    if move_pct is None:
        return []
    size = position_size_usd(cfg)
    gross = size * float(move_pct) / 100
    entry_bbo_cost_pct = float(signal.details["entry_bbo_cost_pct"])
    exit_bbo_cost_pct = float(signal.details["exit_bbo_cost_pct"])
    slippage_pct = float(signal.details["slippage_tolerance_pct"])
    net_profit_pct = float(signal.details["net_profit_pct"])
    lines = [
        f"预估（每腿 ${size:,.0f}）：",
        f"• 当前有利移动：{float(move_pct):.3f}% ≈ ${gross:,.2f}",
        f"• 开仓点差：{entry_bbo_cost_pct:.4f}% ≈ ${size * entry_bbo_cost_pct / 100:,.2f}",
        f"• 当前平仓点差：{exit_bbo_cost_pct:.4f}% ≈ ${size * exit_bbo_cost_pct / 100:,.2f}",
        f"• 整笔开平滑点缓冲：{slippage_pct:.2f}% ≈ ${size * slippage_pct / 100:,.2f}",
    ]
    lines.append(f"• 预估净利：{net_profit_pct:.3f}% ≈ ${size * net_profit_pct / 100:,.2f}")
    return lines


def action_label(signal: Signal) -> str:
    labels = {
        "LONG_BTC_SHORT_ETH": "多 BTC / 空 ETH",
        "SHORT_BTC_LONG_ETH": "空 BTC / 多 ETH（仅观察）",
        "SHORT_BTC_LONG_XAG": "空 BTC / 多 XAG",
        "LONG_BTC_SHORT_XAG": "多 BTC / 空 XAG",
        "SHORT_BZ_LONG_CL": "空 BZ / 多 CL",
        "LONG_BZ_SHORT_CL": "多 BZ / 空 CL",
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

    strategy = cfg.get("strategy", "two_way")
    if strategy == "btc_strength_bias":
        if z < z_open:
            return None
        caution = ""
        tradeable = True
        max_z = float(cfg.get("entry_max_z", 0) or 0)
        if max_z > 0 and z > max_z:
            tradeable = False
            caution = f"Z>{max_z:.2f}，偏离过大，谨慎追单。"
        z_vol_max = float(cfg.get("z_vol_max", 0) or 0)
        if z_vol_max > 0 and z_vol is not None and z_vol > z_vol_max:
            tradeable = False
            caution = f"Z 波动 {z_vol:.2f}>{z_vol_max:.2f}，过滤。"
        return Signal("ENTRY", "LONG_BTC_SHORT_ETH", "", f"ETH/BTC 高位 z={z:+.3f}", tradeable, caution)

    if z >= z_open:
        if cfg["pair"] == "XAG_BTC":
            direction = "SHORT_BTC_LONG_XAG"
        elif cfg["pair"] == "BZ_CL":
            direction = "SHORT_BZ_LONG_CL"
        else:
            direction = "LONG_RATIO"
        return Signal("ENTRY", direction, "", f"比率高位 z={z:+.3f}", True)
    if z <= -z_open:
        if cfg["pair"] == "XAG_BTC":
            direction = "LONG_BTC_SHORT_XAG"
        elif cfg["pair"] == "BZ_CL":
            direction = "LONG_BZ_SHORT_CL"
        else:
            direction = "SHORT_RATIO"
        return Signal("ENTRY", direction, "", f"比率低位 z={z:+.3f}", True)
    return None


def ratio_side(direction: str) -> str:
    if direction in ("LONG_BTC_SHORT_ETH", "SHORT_BTC_LONG_XAG", "SHORT_BZ_LONG_CL"):
        return "short_ratio"
    if direction in ("LONG_BTC_SHORT_XAG", "SHORT_BTC_LONG_ETH", "LONG_BZ_SHORT_CL"):
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


def build_close_signal(cfg: dict[str, Any], snap: PriceSnapshot, z: float, position: dict[str, Any] | None) -> Signal | None:
    if not position or not cfg.get("shadow_position_enabled", True):
        return None
    z_close = float(cfg.get("z_close", 0.35))
    profit_pct = float(cfg.get("close_profit_ratio_pct", 0.10))
    move_pct = favorable_ratio_move_pct(position, snap.ratio)
    if "entry_bbo_cost_pct" not in position:
        raise ValueError(f"Shadow position for {cfg['label']} lacks entry BBO cost; clear the legacy position before using dynamic close costs")
    entry_bbo_cost_pct = float(position["entry_bbo_cost_pct"])
    exit_bbo_cost_pct = one_way_bbo_cost_pct(snap)
    slippage_pct = market_slippage_tolerance_pct(cfg)
    net_profit_pct = move_pct - entry_bbo_cost_pct - exit_bbo_cost_pct - slippage_pct

    if net_profit_pct < profit_pct:
        return None

    reasons = [f"预估净收益 {net_profit_pct:.3f}%"]
    if cfg.get("close_on_z_reversion", False) and abs(z) <= z_close:
        reasons.append(f"Z 回归到 {z:+.3f}")

    entry_direction = str(position.get("direction", ""))
    direction = f"CLOSE_{entry_direction}"
    return Signal(
        "CLOSE",
        direction,
        "",
        "；".join(reasons),
        True,
        details={
            "move_pct": move_pct,
            "entry_bbo_cost_pct": entry_bbo_cost_pct,
            "exit_bbo_cost_pct": exit_bbo_cost_pct,
            "slippage_tolerance_pct": slippage_pct,
            "net_profit_pct": net_profit_pct,
        },
    )


def make_shadow_position(cfg: dict[str, Any], signal: Signal, snap: PriceSnapshot, z: float) -> dict[str, Any]:
    return {
        "direction": signal.direction,
        "ratio_side": ratio_side(signal.direction),
        "entry_ratio": snap.ratio,
        "entry_z": z,
        "entry_base_price": snap.base_price,
        "entry_quote_price": snap.quote_price,
        "entry_bbo_cost_pct": one_way_bbo_cost_pct(snap),
        "position_size_usd": position_size_usd(cfg),
        "entry_time": utc_now().isoformat(),
    }


def run_once(monitors: list[dict[str, Any]], state: AlertState, notifier: TelegramNotifier, force: bool = False) -> None:
    for cfg in monitors:
        try:
            snap = cfg["_source"].snapshot(cfg["pair"], cfg)
            calc: ZScoreCalculator = cfg["_calc"]
            z = calc.add(snap.ratio)
            state.set_history(cfg["id"], calc.dump_history())
            if z is None:
                logger.info("%s warming up: %s/%s", cfg["label"], calc.count, cfg["window_size"])
                continue
            if calc.count < int(cfg["window_size"]):
                logger.info("%s warming up full window: %s/%s z=%+.3f", cfg["label"], calc.count, cfg["window_size"], z)
                continue
            z_vol = calc.z_volatility
            close_signal = build_close_signal(cfg, snap, z, state.shadow_position(cfg["id"]))
            if close_signal:
                key = f"{cfg['id']}:{close_signal.direction}:{close_signal.level}"
                if force or state.should_send(key, int(cfg.get("close_cooldown_sec", 300))):
                    notifier.send(format_message(cfg, snap, z, z_vol, close_signal))
                    state.clear_shadow_position(cfg["id"])
                continue
            if state.shadow_position(cfg["id"]):
                logger.info("%s shadow position active; waiting for close signal. ratio=%.8f z=%+.3f", cfg["label"], snap.ratio, z)
                continue
            signal = build_signal(cfg, z, cfg.get("_prev_z"), z_vol)
            cfg["_prev_z"] = z
            if not signal:
                logger.info("%s no signal: ratio=%.8f z=%+.3f", cfg["label"], snap.ratio, z)
                continue
            key = f"{cfg['id']}:{signal.direction}:{signal.level}"
            cooldown = int(cfg.get("cooldown_sec", 1800))
            if force or state.should_send(key, cooldown):
                notifier.send(format_message(cfg, snap, z, z_vol, signal))
                if signal.level == "ENTRY" and signal.tradeable and cfg.get("shadow_position_enabled", True):
                    state.set_shadow_position(cfg["id"], make_shadow_position(cfg, signal, snap, z))
            else:
                logger.info("%s signal suppressed by cooldown: %s %s", cfg["label"], signal.direction, signal.level)
        except Exception as exc:
            logger.exception("Monitor failed for %s: %s", cfg.get("label", cfg.get("id")), exc)
    state.save()


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
        notifier.send("Mean reversion alert bot test: TG push is working. No orders will ever be placed.")
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
