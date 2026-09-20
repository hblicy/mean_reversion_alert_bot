# Mean Reversion Alert Bot

通用行情提醒，只推送，不下单。使用 Variational 行情监控 BTC/ETH、XAG/XAU、BZ/CL，允许跨日，ENTRY 信号最长跟踪 72 小时。收到信号后，可在自己选择的平台自行核对对应合约、报价和成本。

## 行情源

默认使用 Variational 官方只读 API：

```text
https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats
```

信号使用 `quotes.size_1k` 的 `(bid + ask) / 2`，属于 Variational 参考中间价，不是其他平台的可成交价。`size_1k` 仅表示行情报价档位，不是建议仓位。行情适配器保留 mark price 回退，但没有有效双边报价时禁止采样和价格信号。Variational 文档说明报价可能缓存最长 600 秒，不能把每次轮询当作一次新报价。

每 30 秒轮询；按报价时间划分 10 分钟桶，每桶最多一个有效样本，且两腿更新时间都必须比上一样本新。默认拒绝超过 120 秒、来自未来、两腿相差超过 30 秒或买价高于卖价的报价。缺少有效桶时重启连续窗口预热，不用旧报价填充缺口。新时间戳下价格恰好相同仍是有效观测。

另外持久化记录两腿最新有效报价时间，包括同桶内未采样的报价。任意一腿时间回退时，整组报价不参与 ENTRY/CLOSE 判断；相同报价仍可重试通知，同桶内更新的报价仍可判断 CLOSE。72 小时超时检查不受行情时间回退影响。

以上时间参数是初始设置，并非回测最优值；严格新鲜度限制可能导致低频更新品种长时间无法完成预热。官方接口说明：[Read-Only API](https://docs.variational.io/technical-documentation/api)。

## 信号逻辑

- 只推 `ENTRY` 行情机会提醒和 `CLOSE` 结束跟踪提醒；不指定下单平台、仓位或订单类型。
- 不再推 `WATCH` 观察信号，也不再推 `EXTREME` 极端偏离信号。
- BTC/ETH：只推 `多 BTC / 空 ETH`。
- XAG/XAU：比率为 XAG/XAU，高位空 XAG / 多 XAU，低位多 XAG / 空 XAU。
- BZ/CL：比率为 BZ/CL，高位空 BZ / 多 CL，低位多 BZ / 空 CL。
- 默认要求 Z 仍超过入场阈值、同方向绝对值开始下降，而且实际比率向本次采样前的均值移动。仅仅均值追上价格，不视为回归确认。
- 不按 Variational 点差、资金费、滑点或净收益目标过滤信号；成本由用户在实际交易平台核对。资金费字段缺失或格式异常不影响行情提醒。
- ENTRY 成功发送后，仅记录方向、参考比率、时间和本次采样前窗口的均值/标准差，不模拟成交数量、盈亏或资金费。沿用 `shadow_positions` 存储键，内容表示信号跟踪状态，不是真实持仓。
- 回归区间固定为 **ENTRY 参考均值 ± `z_close` × ENTRY 参考标准差**，默认 `z_close=0.35`。做空比率方向回落至区间上沿或更低，做多比率方向上升至区间下沿或更高，且实际比率已朝信号方向移动，触发 CLOSE。直接跳过整个区间也算完成回归。均值区间和 `z_close` 随该条信号持久化，重启和后续滚动均值变化不会挪动目标。
- `entry_max_z>0` 时仍可发风险提醒：滚动 Z 朝信号不利方向越过上限，而且比率较 ENTRY 继续恶化，即结束跟踪。BTC/ETH 沿用 4.2；金银、原油暂未设该阈值（0），不能把这视为已经具备经验证的止损规则。
- **从 Telegram 确认 ENTRY 发送成功的时间起达到 72 小时，无论盈亏提醒结束跟踪。** 此检查在行情请求、统计预热之前执行，即使接口离线也能发出；进程停机期间无法推送，重启后会检查原 ENTRY 时间。
- CLOSE 发送成功才清掉跟踪状态；发送失败保留并重试。没有 Telegram 配置或仅打印消息，不创建或清除跟踪状态，也不消耗通知冷却时间。
- ENTRY 发送失败会持久化待重试信号，同一采样桶内可重试，不重复采样。每次重试重新校验当前报价、入场条件及方向，沿用原参考均值和已验证的 Z 波动值，重启后也可恢复；原报价超过 `max_quote_age_sec`、条件失效或出现新有效采样时，取消旧信号，由新采样重新判断。重试成功后才开始跟踪和计时。

提醒：CLOSE 不代表你已经盈利或自动平仓。推送中的比率变化百分比也不是持仓收益率；实际交易仍需自行核对两腿合约、手续费、资金费/隔夜费及可成交报价。

## 通用提醒与升级状态

Variational 仅作行情源；本版本没有接入其他交易所的执行或成本接口，也不会把 Variational 的成本当成其他平台成本。旧配置中的 `position_size_usd`、`close_profit_ratio_pct`、`market_slippage_tolerance_pct`、`close_on_z_reversion` 不再参与信号计算，默认配置已移除它们。

旧的纯比率历史没有报价时间戳时，需要重新预热：连续有效采样下，BTC/ETH 约 45 小时、XAG/XAU 与 BZ/CL 约 30 小时。上一版带采样时间的历史可继续使用。旧跟踪状态不会被自动删除或重置起始时间；由于没有 ENTRY 固定均值/标准差，它不能补算新的回归目标，只保留已启用的风险提醒和 72 小时超时提醒。旧成本字段不再读取。

XAG/XAU 使用新的 `xag_xau_variational` 状态 ID，不继承 XAG/BTC 历史。旧 `xag_btc_variational` 状态保留在文件中，但默认配置不再监控它；升级前请核对旧 XAG/BTC 是否仍有真实持仓。不要删除整个 state 文件来处理单个持仓，否则会丢失其他组合的入场时间。

状态文件不存在时可首次启动；已有文件无法读取或 JSON 损坏时，程序报错退出并保留原文件，不用空状态覆盖跟踪记录。

当前没有加入未经历史数据验证的协整/趋势模型，也没有宣称信号胜率提升。参数验证应按时间切分训练与样本外区间，纳入资金费、点差、最大浮亏、全部超时及未退出样本；禁止只统计盈利平仓。

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

仅 Telegram 确认发送成功时返回退出码 0；未配置、发送被拒绝或网络失败时返回 1。

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
- `sample_interval_sec`: 默认 600，统计采样桶长度；与轮询间隔不同。
- `window_size`: 有效连续样本数量；BTC/ETH 为 270，其他为 180。
- `max_quote_age_sec` / `max_quote_skew_sec`: 报价最大年龄 120 秒 / 双腿最大时间差 30 秒。
- `z_open`: 入场阈值；现有阈值保留作为待验证基线，XAG/XAU 的 1.7 尚未针对金银回测。
- `entry_require_cross`: 默认 `true`，要求 Z 向内移动及实际比率收敛；不是首次向外越界就入场。
- `entry_max_z` / `z_vol_max`: 极端 Z / Z 波动过滤，对双向策略同样生效；0 表示不启用该项。启用 Z 波动过滤时，重启后需积累至少 10 个新的 Z 观测才允许入场，避免缺少统计量时绕过过滤。
- `max_holding_hours`: 默认 72，从 ENTRY 发送时间计算超时，不会因重启重新计时。
- `z_close`: ENTRY 固定均值区间的半宽，以 ENTRY 标准差为单位，默认 `0.35`；与利润无关。
- `cooldown_sec`: 同一入场方向冷却时间，默认 1800 秒。
- `close_cooldown_sec`: 平仓提醒冷却时间，默认 300 秒。
- `shadow_position_enabled`: 是否记录 ENTRY 跟踪状态并发出结束提醒；沿用旧字段名，不代表模拟交易。

离线验证（不访问行情、不发送 Telegram）：

```bash
python -B -m unittest discover -s tests -v
```
