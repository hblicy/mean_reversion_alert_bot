# Mean Reversion Alert Bot

只推送，不下单。用于把 BTC/ETH 与 XAG/BTC 均值回归策略改成 Telegram 信号，方便你在 Variational 上手动确认盘口后下单。

## 行情源

默认使用 Variational 官方只读 API：

```text
https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats
```

价格优先使用 `quotes.size_1k` 的 `(bid + ask) / 2`，quote 缺失时回退到 `mark_price`。注意：Variational 文档说明 quote 可能缓存最长 600 秒，所以真正下单前仍要手动确认页面 RFQ。

## 信号逻辑

- 只推 `ENTRY` 入场信号和 `CLOSE` 平仓提醒。
- 不再推 `WATCH` 观察信号，也不再推 `EXTREME` 极端偏离信号。
- BTC/ETH：只推 `多 BTC / 空 ETH`。
- XAG/BTC：双向推 `空 BTC / 多 XAG` 或 `多 BTC / 空 XAG`。
- ENTRY 推出后，机器人会按每腿 `position_size_usd` 记录一个本地“影子持仓”。
- 后续如果 ratio 朝盈利方向移动超过 `close_profit_ratio_pct`，会推一次 CLOSE 平仓提醒并清掉影子持仓；默认不再用均值回归触发平仓。

提醒：影子持仓只代表“机器人上次推过入场信号”，不代表你真的下单。CLOSE 只是提醒：如果你跟了上一条 ENTRY，可以考虑手动平仓。

## 快速开始

```bash
cd ~/mean_reversion_alert_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

测试通知：

```bash
python bot.py --test-notify
```

前台运行：

```bash
python bot.py
```

Linux/VPS 后台运行：

```bash
chmod +x start.sh stop.sh
./start.sh
./stop.sh
```

查看日志：

```bash
tail -100f logs/alert-bot.log
```

只跑一轮：

```bash
python bot.py --once
```

## 主要配置

在 `config.json`：

- `quote_size`: 默认 `size_1k`。
- `position_size_usd`: 每腿预估开仓名义本金，默认 `1000`。
- `z_open`: 入场阈值。
- `close_on_z_reversion`: 是否允许 Z 回归触发平仓，默认 `false`。
- `close_profit_ratio_pct`: ratio 有利移动多少百分比后提醒平仓，默认 `0.5`。
- `spread_cost_pct`: 预估点差磨损百分比；BTC/ETH 默认 `0.0044`，XAG/BTC 默认 `0.024`。
- `cooldown_sec`: 同一入场方向冷却时间，默认 1800 秒。
- `close_cooldown_sec`: 平仓提醒冷却时间，默认 300 秒。
- `shadow_position_enabled`: 是否启用影子持仓和平仓提醒。
