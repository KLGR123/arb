# 短周期加密 Up/Down 定价研究

2026-10-29 的连续 60 个 BTC 五分钟已结算市场中，Binance 秒级行情的 TWAP 方向与真实结果匹配 **59/60（98.33%）**。这验证了 CEX 代理的方向近似能力，没有验证它等同于 Chainlink。分钟历史价格下的扣费理论价差也不能证明可捕获利润：缺少历史双边盘口、秒级 Polymarket 报价和实际接收时间。

机器结果：[JSON](reports/crypto-updown-pricing.json)。输入：`research/fixtures/updown/btc-5m-20261029.json.gz`；计算与采集入口：`arb_research.updown`、`arb_research.updown_cli`。不创建钱包、不签名、不下单。

## 规则与样本

当前规则由每个市场自己的文本、`resolutionSource` 和 `cryptoMarketConfig` 核验。此次全部是 `btc-5m-twap-60`：开盘价格基准和终值来自 BTC/USD Chainlink 60 秒 TWAP，终值大于或等于基准时 Up 获胜。不是用市场开启时的 CEX 单 tick 作基准，也不是五分钟全程均价。[官方结算说明](https://docs.polymarket.com/concepts/resolution)、[实测市场规则](https://polymarket.com/event/btc-updown-5m-1793272800)。本实现遇到不同资产、时长、TWAP 窗口、来源、未结算或分割结算会拒绝，不能套用于其他规则。

样本在录制前固定为 UTC **2026-10-29 11:20–16:20** 的连续 60 个窗口，按 slug 时间顺序取样，没有按涨跌、成交量或结果筛选。录制始于 16:57:10 UTC。每个 Gamma 记录保留完整规则、二元最终 payout 与手续费；真实结果要求 `closed=true`、`umaResolutionStatus=resolved`，且 payout 为 `[1,0]` 或 `[0,1]`。历史价格的结算终点从预测输入中排除，最终结果只用于评价。

数据均为无需账号的官方公开接口：

- Binance `data-api.binance.vision/api/v3/klines`，`BTCUSDT`、`interval=1s`，19,000 个收盘点，含 1,000 秒预热。时间按 `(close_time_ms+1)/1000` 归一化，价格为刚结束一秒的 close，并从此刻起左保持。原始 kline 的规范化 JSON 响应摘要和参数随录制保存。[官方行情接口](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)、[官方仅行情域名](https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only)。
- Gamma `/events?slug=...` 提供规则和结果。Data API **v2** `/v2/prices-history` 按 Up token、明确起止时间、`bucket_seconds=60` 获取价格，逐页走完游标；不调用已下线 v1，也不调用旧 CLOB 历史端点。[官方历史接口](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history)。Gamma 中的 `version=v1` 是市场协议元数据，不是本次 HTTP API 的版本。

Polymarket 网络读取现在复用 ARB-7 的 `PublicDataClient`、共享限流器和 main 的 `PUBLIC_API_BUDGET`；不另建元数据/Parquet/DuckDB 层。采集器只添加公开 CEX candles 和本研究的不可变录制。`ARB_POLYMARKET_PROXY` 只显式传入 Polymarket 客户端；CEX 直连，忽略全局代理变量，日志和录制不含代理值。所有请求含重试退避。录制的 compliance guard 返回 `blocked=true`、`trading_allowed=false`，该研究始终只读。

## 结算复刻

以秒级 close 的分段常数路径积分：`TWAP(t)=∫[t−60,t) S(u)du / 60`。积分使用 Decimal，支持不等间隔，以时间加权而非成交量加权；起止点必须有覆盖，超过 2 秒的缺口拒绝，不补造价格。比较开收盘 TWAP，平价归 Up。这里复刻的是 CEX 代理，无法重建 Chainlink 多交易所聚合、异常值处理和发布时序。

| 项目 | 实测结果 |
| --- | ---: |
| 已结算、规则合格市场 | 60 |
| 一致 / 不一致 | 59 / 1 |
| 方向一致率 | 98.33% |
| Wilson 95% 区间 | 91.14%–99.71% |

区间只是样本比例的描述。连续窗口相关且仅有一天、一个资产，不满足独立随机抽样假设，不能据此外推生产一致率。

唯一不一致为 [14:15–14:20 UTC 窗口](https://polymarket.com/event/btc-updown-5m-1793283300)（market `6343878`）：CEX 开盘 TWAP **80,450.38217 USDT**，收盘 **80,446.45683 USDT**，差 **−3.92533 USDT**，预测 Down，实际 Up。这与来源差异、USDT/USD 基差或边界时序误差相容，但本录制没有 Chainlink 真值序列，不能判定原因，也不能报告“结算价格误差”。官方当前的原生 TWAP 流需要认证；本票不使用账号或 API key。[官方实时 TWAP 文档](https://docs.polymarket.com/market-data/realtime-data#twap-prices)。

## 公允概率模型

局部零漂移价格扩散 `dS=S_t σ dW`，其中 `σ=sqrt(Σlog(S_i/S_(i−1))² / elapsed_seconds)`，只使用观察时点之前的 900 秒已完成数据，单位为 `1/√秒`。此模型对短时小幅波动采用正态价格近似；不是精确对数正态算术平均分布。未拟合任何未来结果。

设收盘为 `T`，TWAP 窗口 `L=60`，开盘 CEX TWAP 为 `K`，观察时刻 `t`：

- `t≤T−L`：终值均值为 `S_t`，方差 `S_t²σ²(T−L−t+L/3)`。
- `T−L<t<T`：已知积分 `A=∫[T−L,t)S(u)du`，终值均值 `(A+S_t(T−t))/L`，方差 `S_t²σ²(T−t)³/(3L²)`。
- `p_up=Φ((mean−K)/sd)`；零方差和到期按平价 Up 的确定性比较处理。

因此最后一分钟不能直接套“当前现价是否高于开盘”的终值公式。该模型仍忽略跳跃、波动率变化、CEX/Chainlink 基差；98.33% 结算方向匹配也不等于概率已校准。需跨日样本检验后才能作更强结论。

## 价格误差与理论 edge

分钟价格的桶内聚合/可见时序不能由录制恢复。为避免把整桶信息提前使用，统一将 `timestamp+resolution_seconds` 作为参考价的可用时刻，仅比较到期前的桶；每个市场得到剩余 **240、180、120、60 秒**的四个观察点。此约定保守地引入了约一分钟的价格滞后。下表误差 `p_model−p_reference` 是与这个历史参考价的差，不是真实公允概率误差或同一时刻可交易报价误差。

基准无额外延迟、共 240 点：有符号误差均值 **+2.5602 个百分点**，p05/p50/p95 为 **−35.3556 / +4.3600 / +37.2389** 个百分点；绝对误差均值 **18.3580**、中位 **13.9491**、p95 **49.9565** 个百分点。对最终二元结果的 Brier 均方损失为模型 **0.15618**、历史参考 **0.19629**。两者信息时效不同，这个差不能证明模型胜过即时市场。

| 剩余时间（秒） | 点数 | 平均绝对差（百分点） | p95 绝对差（百分点） | 扣费理论 edge（USD/份） |
| --- | ---: | ---: | ---: | ---: |
| (0,60] | 60 | 17.0213 | 65.7172 | 0.16250 |
| (60,180] | 120 | 18.6535 | 48.4032 | 0.17322 |
| (180,300] | 60 | 19.1036 | 34.7400 | 0.17424 |

| 已实现 σ（1/√秒） | 点数 | 平均绝对差（百分点） | p95 绝对差（百分点） | 扣费理论 edge（USD/份） |
| --- | ---: | ---: | ---: | ---: |
| <0.00005 | 92 | 19.5302 | 57.6626 | 0.18264 |
| [0.00005,0.00010) | 132 | 17.9765 | 39.0068 | 0.16676 |
| ≥0.00010 | 16 | 14.7655 | 35.1694 | 0.13605 |

方向按决策时 `p_model−p_reference` 选择，Down 参考价假设为 `1−Up`；没有观测 Down ask。每份理论 edge 为到达时新鲜模型的该方向概率减到达时已可见的分钟参考价，再减单份 taker 费 `rate×price×(1−price)`，按 0.00001 USD 四舍五入。每个市场录制的 rate 为 **0.07**、exponent 为 1，复用现有 `negrisk.taker_fee`。[官方费率说明](https://docs.polymarket.com/trading/fees)。pUSD 和 USDT 均近似 USD，不计返佣。

基准理论 edge 均值 **0.17080 USD/份**，p05 **0.00461**、p50 **0.12594**、p95 **0.48244**，235/240 点为正。这些很大的数字主要暴露了分钟参考的时效和假设限制；不包括 spread、深度、滑点、排队或成交概率，不能称作套利回报、PnL 或已验证可捕获 edge。

## 延迟敏感性

完整 JSON 包含数据延迟与下单延迟 `{0,0.05,0.25,1,2}` 秒的 **25 个组合**；“下单延迟”只是纸面到达时间参数。数据截止 `floor(decision−data_delay)`，到达模型使用 `floor(arrival)`，不得读取未来 CEX 记录；参考价只向后取已可见的桶，不插值、不越过到期。方向在决策时固定，延迟后才计算保留的理论价值。

| 数据延迟（秒） | 到达延迟（秒） | 平均绝对差（百分点） | 平均扣费 edge（USD/份） | edge>0 点数 |
| --- | --- | ---: | ---: | ---: |
| 0 | 0 | 18.3580 | 0.17080 | 235 |
| 0.05 / 0.25 / 1 | 0 | 18.1289 | 0.16859 | 231 |
| 2 | 0 | 18.0043 | 0.16861 | 232 |
| 0 | 1 | 18.3580 | 0.17218 | 228 |
| 0 | 2 | 18.3580 | 0.17497 | 228 |
| 2 | 2 | 18.0043 | 0.17277 | 225 |

50ms/250ms 到达延迟与 0 秒相同；50ms/250ms/1s 数据延迟均落到前一秒。它们的重合是时间分辨率的结果，不能用于声称低延迟无价值。平均 edge 没有随延迟单调减少，因为价格只按分钟更新且到达时模型会变；2s/2s 场景的 p05 已降为 **−0.00410 USD/份**。JSON 的 `probability_staleness` 另记录陈旧概率相对决策时新鲜模型的误差。严格评估毫秒级可捕获价差仍需同步 tick/盘口及接收时间录制，本次没有这类证据。

## 复现与接口

在 `research/` 内运行，分析和测试均离线：

```bash
uv run --locked python -m arb_research.updown_cli analyze \
  --input fixtures/updown/btc-5m-20261029.json.gz \
  --output ../docs/reports/crypto-updown-pricing.json
uv run --locked pytest
```

重新读取公开数据（旧的分钟数据可能因保留期到期而缺失，优先用已提交录制复现）：

```bash
uv run --locked python -m arb_research.updown_cli record \
  --start 1793272800 --count 60 --output btc-5m-recording.json.gz
```

JSON v1 保留输入 SHA256、模型/时效假设、60 条逐市场复刻结果、240 条逐时点公允 p、按剩余时间与波动率分桶的分布、Brier、费用、25 组延迟结果及错误。没有数据的桶输出 `n=0` 与 `null`；小于 50 市场或任一市场缺规则/路径/历史时输出 `incomplete`，分析 CLI 返回 2，不会把不完整样本当整包成功。真实录制与分析不能以合成 fixture 替代。

单测覆盖精确时间加权、平价、TWAP 内外的独立解析公式、已知部分积分、未来数据扰动不改变当前概率、缺口/预热/无效规则拒绝、真实录制回放、费用、时间量化、结算点排除、代理作用域与错误脱敏。未接入交易入口；已有合规 guard 的拒绝行为由全量测试继续验证。
