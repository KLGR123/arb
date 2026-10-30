# BTC Up/Down 成交与滑点约束

2026-10-30 的短窗口真实盘口回放中，100 份独立买入探针的平均扣费纸面 edge 为 **0.04247 USD/份**，逐档买入后为 **0.02562 USD/份**；额外承受 3 tick 不利价格变化后变成 **−0.00387 USD/份**。这三个均值来自 **90 个逐秒信号中能够模拟成交的同一组 89 个点、一个 BTC 五分钟市场的 REST 采样估计**，不是实际成交、收益或可重复套利证据。两次轮询间的变化不可观测，CEX 概率也使用假定可得时序，结果应视为乐观的成本敏感性研究。

机器结果分别为 [REST 快照 JSON](reports/crypto-updown-fillable-edge-rest.json) 和 [WebSocket JSON](reports/crypto-updown-fillable-edge.json)。两组按不同采样方式独立报告，不混合统计。代码只读取公开行情和离线文件，不构造钱包、签名或订单；复用 main 的 `arb_research.compliance`，两份录制均为 `blocked=true`、`trading_allowed=false`。

## 数据与可用性

优先检查了 ARB-8 的现有 `tests/fixtures/orderbook-live`，其市场是以色列总理政治盘，不能与 ARB-15 的 BTC 定价直接配对。因此复用 ARB-8 `Recorder`、校验和 journal、`Rebuilder`，补录当前与下一连续 BTC 五分钟市场的 Up/Down 两个真实 outcome，而非用 `1−Up ask` 伪造 Down 深度。

| 录制 | UTC 时间 | 原始记录 | 纸面信号 / 无法定价的秒点 | 使用到的市场 |
| --- | --- | ---: | ---: | --- |
| WebSocket | 02:00:47.591–02:03:47.614 | 812 | 4 / 176 | `6376279` |
| REST | 02:05:23.810–02:06:53.404 | 258 | 90 / 0 | `6376466` |

WebSocket 观察到 **26 条 gap 记录**，包含 hash 冲突、top-of-book 不匹配和断线；录制器按原有规则重连。无法使用的盘口不向前填充，恢复后也拒绝跨缺口的延迟探针。4 个纸面信号中只有 2 个能在零延迟模拟成交，不足以评估分布；这里保留失败覆盖率作为证据，没有调整校验来扩大样本。REST 录制是其后的独立短窗口：每轮顺序读取活跃市场两个 outcome，最短轮询周期 0.5 秒；实测两个 receipt 并不同步，不能把它们当原子快照。第一个观察点缺少所选 Down book，仍保留纸面信号，零延迟计为未成交。

Up book 无法提供有效 midpoint 或 CEX 无覆盖时无法构成纸面信号，计入 `excluded`；规则不合格则拒绝整个录制。已构成信号但所选 outcome 无盘口时保留在探针分母，`decision_book_reason` 和 `depth_unavailable` 桶记录缺失。所有成交覆盖率仍以这组可计算纸面信号为条件，不代表全部市场时刻的策略成交率。

REST 全量 snapshot 的正常变化不当作 WebSocket delta mismatch；只有显式 `book_transport=rest`、每条事件都标记 `source=rest_snapshot` 才启用该模式。按最后已收到的 snapshot 左保持到下一 receipt，同时检查本地记录年龄和服务端 timestamp 不超过 5 秒、未来偏差不超过 1 秒。**这不能排除两次请求之间撤单或流动性消失**，也不提供连续性或真实填单概率。WS 使用同一年龄限制，并额外校验 gap。

输入保存在 `research/fixtures/updown/btc-books-20261030.json.gz` 和 `btc-rest-books-20261030.json.gz`，包含完整规则、费用、原始公开 book、接收时间、CEX 秒级价格和规范响应摘要。每条 book journal 有 SHA256，报告另存压缩输入 SHA256，离线重放验证原始数据未变。没有 secrets 或代理地址。

数据来源是官方公开 [Market Stream](https://docs.polymarket.com/market-data/realtime-data)、Gamma `/events?slug=...`、[CLOB V2 book](https://docs.polymarket.com/market-data/prices-order-books) `/book?token_id=...` 和 [Binance Kline](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)。不调用 Data API v1、旧历史端点或归档 SDK。REST 复用现有 allowlist `ReadOnlyAPI`，Gamma/CEX 复用 ARB-15 `StudySources`；WS 复用 ARB-8 连接、令牌桶与退避。各路径同时消耗 main 的 `PUBLIC_API_BUDGET`。Polymarket HTTP/WS 客户端显式读取 `ARB_POLYMARKET_PROXY`，CEX 直连；不设置全局代理、不记录代理值。未配置代理时使用直连，失败报错明确说明这一点。

## 信号、费用与成交近似

公允概率直接复用 ARB-15 `PricePath` / `fair_up`：BTC/USD Chainlink TWAP60 规则、CEX 开盘 TWAP 基准、900 秒尾随已实现波动率和局部零漂移扩散。规则核验增加 `require_resolved=False` 供活跃盘使用；原有默认仍要求已结算二元 payout，不把结算结果输入概率。Gamma 的协议 `version=v1` 不等同于 HTTP CLOB API 版本；协议 V2 使用 `positionIds`，CTF 市场使用 `clobTokenIds`。

对每个覆盖区间内的整数秒 `t`，使用时间 `≤t` 收到的盘口。CEX 数据在录制后读取，规范化为刚结束一秒的 close；决策只用 `t−1` 及以前的点，额外假定 1 秒可得延迟。**没有现场 CEX receipt 时间，不能据此验证端到端 50ms 延迟**。CEX 是 Chainlink 代理、USDT 和 pUSD 近似 USD 的限制沿用 ARB-15。

令 `m=(Up best_bid+Up best_ask)/2`，按 `p_up≥m` 选择买入 Up，否则买入 Down。方向和模型公允值在决策时冻结，所有延迟场景保持相同信号；这衡量同一信号遇到不同盘口成本后的保留空间，不引入到达后的未来公允值：

```text
paper edge = selected fair value − paper reference
paper net edge = paper edge − taker_fee(Q, rate, paper reference) / Q
paper reference = m (Up), 1−m (Down)
fillable edge = frozen fair value − arrival stressed ask VWAP − filled fee / filled quantity
expected surplus per requested share = fill fraction × fillable edge
```

真实到达盘口取 `received_ns≤t+delay` 的最近记录，不插值、不使用未来 receipt、不跨到期。按真实所选 outcome 的 ask 价格升序消费，每档数量不超过显示深度和剩余需求；默认请求 **Q=100 份**。记录实际模拟填充量、余量、VWAP、逐档费用、成交比例与原因。请求量或模拟部分成交小于市场最小 **5 份**时拒绝。每个探针独立重置盘口，没有连续执行、持仓、排队、市场冲击反馈或资金路径；100% 模拟覆盖不表示真实成功率。

费用复用 ARB-10、ARB-15 和 ARB-17 的 `negrisk.taker_fee`，按 [官方公式](https://docs.polymarket.com/trading/fees) `shares×rate×price×(1−price)`，每档 half-up 到 0.00001 USD；录制 schedule 为 rate=0.07、exponent=1、takerOnly=true。不计返佣。每档模拟价格再加 `adverse_ticks×arrival tick_size`，据此重新计算费用；达到或超过 1 的档位不可成交。这是额外成本压力，不声称压力后的价位真实存在对应流动性。该样本 tick 为 **0.01 USD/份**。

无成交的 `fillable_edge=null`，不混入已填充 edge 均值；按请求量计的预期 surplus 计零，并同时显示缺口、尾段截断和成交覆盖率。这个 surplus 是模型价值减成本，**不是已实现 PnL**。`paired_paper_net_edge` 只使用该场景成功模拟成交的同一组信号，便于识别选择偏差。

## 分布与深度分桶

以下是 REST 的零额外延迟、零额外 tick 压力基准，90 个决策、89 个模拟全量成交、1 个缺少所选 outcome 的 book；其中 **82/90** 个纸面扣费 edge 为正、**66/89** 个已模拟成交的 edge 为正。

| USD/份 | 均值 | p05 | p50 | p95 |
| --- | ---: | ---: | ---: | ---: |
| paper edge，扣费前，90 点 | 0.05849 | 0.01360 | 0.04707 | 0.15207 |
| paper net edge，90 点 | 0.04218 | −0.00363 | 0.03042 | 0.13465 |
| paired paper net edge，89 点 | 0.04247 | −0.00375 | 0.03082 | 0.13635 |
| fillable edge，89 点 | 0.02562 | −0.02480 | 0.01662 | 0.09085 |
| paired paper net − fillable，89 点 | 0.01686 | −0.00492 | 0.00907 | 0.07572 |

部分 erosion 为负，与两个 outcome 非同步报价和 `1−Up midpoint` 的纸面参考定义相容，不能当作负交易成本。真实深度始终来自所选 outcome 的 book。

| 决策时所选 outcome 的 ask 深度（距 best ask 5 tick 内） | 点数 | paper net 均值 | fillable 均值 |
| --- | ---: | ---: | ---: |
| <100 份 | 1 | 0.08043 | −0.00317 |
| [100,1000) 份 | 16 | 0.08884 | 0.05821 |
| ≥1000 份 | 72 | 0.03164 | 0.01877 |
| 所选 book 不可用 | 1 | 0.01592 | null |

小于 100 份的桶仍可继续吃 5 tick 以外的深度，故不自动等于部分成交。这里的桶不代表同质、随机或独立样本。

剩余时间按 `(0,60]`、`(60,180]`、`(180,300]` 秒分桶；REST 的 90 点全部在 **187–276 秒**，均在第三桶。波动率按 `<0.00005`、`[0.00005,0.00010)`、`≥0.00010`（单位 `1/√秒`）分桶，本次全在第三桶。没有覆盖的桶保留 `n=0`、统计量 `null`，不从同一短窗口外推其他期限或低波动环境。WS 可成交的两个点分别剩余 245、130 秒，盘口厚度均 ≥1000 份，同组扣费纸面均值 0.00438、成交近似均值 −0.00052 USD/份；另两个信号所选盘口不可用。样本过少，不与 REST 作跨时间收益比较。

## 延迟与滑点敏感性

REST 每个单元格为已模拟成交信号的 **平均 fillable edge（USD/份）**。同一决策队列逐一尝试 `{0,0.05,0.25,1,2}` 秒到达和 `{0,1,3,5}` tick 压力，共 20 个场景。1/2 秒分别有 1/2 个末尾探针超出录制，报告保留为 `recording_tail`；它们不属于市场深度不足。

| 到达延迟 | 0 tick | 1 tick | 3 tick | 5 tick | 模拟全量成交 / 90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 | 0.02562 | 0.01577 | −0.00387 | −0.02345 | 89 |
| 50ms | 0.02645 | 0.01661 | −0.00304 | −0.02263 | 89 |
| 250ms | 0.02958 | 0.01973 | 0.00008 | −0.01952 | 90 |
| 1s | 0.02583 | 0.01599 | −0.00365 | −0.02324 | 89 |
| 2s | 0.02775 | 0.01791 | −0.00173 | −0.02131 | 88 |

延迟均值不单调，可能因为等待后盘口价格变化有利、异步采样和尾部队列变化；不支持“延迟增加收益”或“50ms 优化有效”的结论。250ms 首个信号已能观察到 Down book，因此模拟成交数从 89 增到 90；1/2 秒场景的 paired paper net 均值分别为 0.04278/0.04328，不同于全队列的 0.04218，不能把它们的均值差全归因于延迟。JSON 另保留按请求量计的 surplus 分布和每个探针的逐档计算。

WS 的 0/50ms 保留 2/4 个模拟成交，250ms 为 1/4，1s/2s 为 0/4，失败均因盘口不可用；不能把空均值当零 edge，也不能把这种数据缺失解释成真正的市场成交概率。

## 复现与验收边界

在 `research/` 内离线运行：

```bash
uv run --locked python -m arb_research.fillable_cli analyze \
  --input fixtures/updown/btc-books-20261030.json.gz \
  --output ../docs/reports/crypto-updown-fillable-edge.json
uv run --locked python -m arb_research.fillable_cli analyze \
  --input fixtures/updown/btc-rest-books-20261030.json.gz \
  --output ../docs/reports/crypto-updown-fillable-edge-rest.json
uv run --locked pytest
```

自定义研究参数用 `--shares 100 --delays 0 0.05 0.25 1 2 --ticks 0 1 3 5`；两类场景必须保留零基准。补录公开短窗口（需要网络，测试和 CI 不执行）：

```bash
uv run --locked python -m arb_research.fillable_cli record \
  --duration 180 --transport websocket --output btc-ws.json.gz
uv run --locked python -m arb_research.fillable_cli record \
  --duration 90 --transport rest --output btc-rest.json.gz
```

`status=complete` 只表示录制含有效信号并可完成离线回放，不表示样本量足够、策略获利或生产可执行；无有效信号时 `incomplete`，CLI 返回 2。JSON 含所有信号、每个延迟/压力探针、原始 coverage、排除原因和分桶。

单测使用明确的手算深度/费用 fixture、合成时序边界和两份真实录制；验证部分成交、最低数量、逐档舍入、tick 压力、未来盘口/CEX 不影响当前信号、断线缺口、陈旧数据、录制尾部和活跃规则不会放松默认结算校验。真实录制测试禁用 HTTP 网络，并核对完整报告与输入摘要。没有任何自动执行或实盘路径。
