# Mean Reversion Alert Bot

只读取公开行情、推送 Telegram 提醒，不下单。默认仅启用 US30/US100，固定 β=0.5，138 个 10 分钟桶，ENTRY 阈值 2.0、回归带半宽 0.5σ、最长跟踪 46 个交易小时。BTC/ETH、XAG/XAU、BZ/CL 已迁移配置并默认关闭；状态 ID 保留，改 `enabled` 即可恢复。

## 行情源

默认使用 Variational 官方只读 API：

```text
https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats
```

信号使用 `quotes.size_1k` 的 `(bid + ask) / 2`。缺少该档报价时状态为 `CLOSED_OR_NO_QUOTE`，不会用 mark price 产生信号。`size_1k` 是报价档位，不是建议仓位。资金费仅展示，不筛选信号；swap 优先展示 next、否则 last 的多空年化费率，perpetual 展示 last 年化费率。

| 配置组合 | A 腿 / B 腿 | 合约类型 | 默认状态 |
| --- | --- | --- | --- |
| US30/US100 | US30-SWAP / US100-SWAP | swap | 启用 |
| BTC/ETH | ETH-PERP / BTC-PERP | perpetual | 关闭，保留 ETH/BTC 比率及仅多 BTC / 空 ETH 的方向 |
| XAG/XAU | XAG-PERP / XAU-PERP | perpetual | 关闭 |
| BZ/CL | BZ-PERP / CL-PERP | perpetual | 关闭 |

`US500-SWAP`、`CNA50-SWAP` 是可用的指数 ticker，本次未配置该组合。**US500-PERP 是 SPY ETF 永续，不是 US500 指数 swap。** 启动时校验所有配置腿的 ticker 存在且 contract_type 一致，错配拒绝启动。

所有组合共享一个 HTTP Session，每轮只拉取一次 v2 全量行情，超时 12 秒。429 按接口 `retry_after_seconds` 等待；网络/服务异常采用 30、60、120、240、300 秒退避。v1 `variational_metadata` 保留为永续行情的显式回退，指数 swap 必须使用 v2。Binance/OKX/Yahoo 旧适配器已移除。

官方接口说明：[Read-Only API](https://docs.variational.io/technical-documentation/api)。

## 交易时段与采样

每 30 秒轮询，按较早的报价时间划分 600 秒桶，每桶至多采样一次，两腿时间戳都须推进。拒绝报价超过 120 秒、来自未来、双腿相差超过 30 秒、时间回退或交叉 BBO。相同报价可重试未送达的提醒，桶内更新可判断 CLOSE。

`sessions` 采用 `America/New_York`，由 zoneinfo 处理夏令时。US index swap 通常周一至周四为 00:00–17:00、18:00–24:00 ET，**周五 17:00 ET 起周休**；周末默认关闭，假日由 `sessions.holidays.us_index_swap` 的 ET 日期列表维护。日常 17:00–18:00 ET 休市对应夏令时 05:00–06:00 北京时间、冬令时 06:00–07:00 北京时间。

接口优先：计划开市但没有报价视为无报价；计划休市但有新鲜双边报价则接受，并每天至多记录一次 `session table mismatch`，支持周日提前恢复。每组每 10 分钟至多一条状态摘要，包含 ticker、状态、报价年龄和窗口填充度。

计划休市的缺桶保留窗口；US30/US100 开市桶跨度超过 3 桶时重新预热，例如开市期间中断 40 分钟。复市前 2 个有效桶更新统计但不发 ENTRY。任一腿中间价在连续 3 个候选桶保持不变且时间推进，标记 `STALE`，暂停采样和提醒，保留窗口；价格恢复变化后继续。旧三组保持原来的严格连续桶策略，不启用平价检测。

## 信号逻辑

- `method` 支持 `ratio`（A/B）、`log_ratio`（ln A − ln B）、`beta_spread`（ln A − β ln B）。组合及方向由 legs、allowed_sides 定义，不需增加每组代码分支。
- β 可固定，或用 `rolling_return_ols` 在前序 `rolling_buckets` 个收益上估计并限制在 min/max；数据不足时用 beta.value。当前桶不参与 β 估计。
- Z 使用**当前点之前** W 个价差的均值/样本标准差；前序窗口未满不发信号。默认要求绝对 Z 开始收敛，并且价差实际向前序均值移动。高 Z 为 `short_a_long_b`，低 Z 为 `long_a_short_b`。
- ENTRY 送达后冻结 method、β、均值、标准差、z_close、价差和起始时间。CLOSE 使用冻结参数，需实际朝信号方向移动并达到或越过固定回归带；修改配置及重启不会移动该目标。`entry_max_z` 另提供继续恶化时的风险结束提醒。
- US30/US100 按两腿共同开市时间跟踪，计划休市及已观测到的无报价期间暂停计时，周日有报价的时段正常计入。超时在休市期间到期时保留状态，待恢复有效报价后补发一次“（休市期间到期，开市后补发）”。停机期间无法观测行情，恢复时对未知区间按日程计时。旧三组保持 72 个自然小时，即使行情离线也检查超时。
- 发送失败不消耗冷却、不创建或清除跟踪；ENTRY 的待重试信号持久化，同桶内重新校验后重试，原报价过期或条件失效即取消。Telegram 返回非 JSON 或拒绝响应视为失败。
- 消息展示双腿点差、各自资金费和北京时间。资金费缺失不影响信号；价差变化 bps **不是持仓收益率**，不模拟成交或宣称收益。

提醒：CLOSE 不代表你已经盈利或自动平仓。推送中的比率变化百分比也不是持仓收益率；实际交易仍需自行核对两腿合约、手续费、资金费/隔夜费及可成交报价。

## 通用提醒与升级状态

`shadow_mode: true` 只计算并在日志中记录 `SHADOW` 信号，不发送 Telegram、不创建新的跟踪状态，也不发心跳/降级通知（所有启用组合均为 shadow 时）。`tier: observe` 会在消息中加“观察级·弱相关”标记。

建议合并后先跑两周 shadow 日志再审核统计与阈值。复制 `config.json` 为 `config.shadow.json`，把 US30/US100 的 `shadow_mode` 改为 true，并给 `state_file` 设置独立路径，再运行 `python bot.py --config config.shadow.json`。默认正式配置按任务说明设为 false。

新历史保存 `{bucket, ts, a_mid, b_mid}`，β 变化时重算价差；前序 Z 和 Z 历史也持久化。ratio 组合兼容旧数值历史及 `SHORT_BZ_LONG_CL` 等方向，保留旧跟踪的参考和入场时间。旧信号无冻结参考时不补造回归目标。被关闭组合的历史和跟踪仍留在 state 文件中；不要删除整个文件。

状态保存采用临时文件、flush、fsync 和原子替换。已有状态无法读取或 JSON 损坏时拒绝启动，保留原文件。正常运行时，单组合异常不阻止其他组合，主循环记录完整堆栈并继续下一轮。

连续 5 轮失败或共同开市期间 30 分钟无有效报价会推送 `bot degraded`，6 小时冷却。每天 09:00 北京时间后首轮发送一次心跳，包含每组窗口进度、最新报价年龄、开闭市状态、跟踪状态；重启后可补发当日未送达的心跳。

日志由 bot.py 写入 `logs/alert-bot.log`，每份 10 MB，保留 5 份备份。`start.sh` 不再同时重定向到同一个文件，避免重复写入及轮转失效。systemd 示例见 [deploy/alert-bot.service](deploy/alert-bot.service)，部署前修改 User、WorkingDirectory、EnvironmentFile 和 ExecStart；该文件仅供部署参考，不会自动安装或启用。

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
- `legs.a` / `legs.b`：ticker、显示名称、contract_type、session。启动校验也涵盖禁用组合。
- `method` / `beta`：价差方法及固定/滚动 β。
- `window_buckets`: 前序有效窗口；US30/US100 为 138，BTC/ETH 为 270，其他为 180。
- `max_quote_age_sec` / `max_quote_skew_sec`: 报价最大年龄 120 秒 / 双腿最大时间差 30 秒。
- `z_open`: 入场阈值；现有阈值保留作为待验证基线，XAG/XAU 的 1.7 尚未针对金银回测。
- `entry_require_cross`: 默认 `true`，要求 Z 向内移动及实际比率收敛；不是首次向外越界就入场。
- `entry_max_z` / `z_vol_max`: 极端 Z / Z 波动过滤，对双向策略同样生效；0 表示不启用该项。启用 Z 波动过滤时需至少 10 个有效 Z；相同参数重启会恢复 Z 历史，方法或 β 参数改变时重新积累。
- `max_holding_trading_hours`: US30/US100 为 46；`always` 组合使用 `max_holding_hours: 72`。
- `z_close`: ENTRY 固定均值区间的半宽，以 ENTRY 标准差为单位，默认 `0.35`；与利润无关。
- `cooldown_sec`: 同一入场方向冷却时间，默认 1800 秒。
- `close_cooldown_sec`: 平仓提醒冷却时间，默认 300 秒。
- `shadow_position_enabled`: 是否记录 ENTRY 跟踪状态并发出结束提醒；沿用旧字段名，不代表模拟交易。
- `max_open_gap_buckets` / `reopen_grace_buckets` / `flat_buckets`：开市缺口、复市宽限和平价检测。
- `shadow_mode` / `tier`：日志观察及提醒等级；`sessions.holidays` 需按实际休市日维护。

离线验证（不访问行情、不发送 Telegram）：

```bash
python -B -m unittest discover -s tests -v
```

测试涵盖三天模拟、周末复市、DST、无未来数据统计、旧状态恢复和通知重试。模拟不代替 24 小时实时 shadow 验收；从一天中途或周五启动，剩余交易时段可能不足以填满 138 桶。首次实时 `--once` 会打印 ticker、状态、报价年龄和窗口进度；休市无报价仍返回 0。
