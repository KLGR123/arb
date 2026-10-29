# 结算风险与巨鲸特征（ARB-16）

提供公开只读采集、离线统计和带可得时间的 Parquet 特征。没有下单、签名、钱包或跟单入口。真实样本与报告在 `research/fixtures/resolution-whales/`，合成边界测试在 `research/tests/fixtures/resolution-whales-contract.json`。

## 运行

在 `research/` 中：

```bash
uv sync --locked
# 默认选择成交量最高的已关闭、未关闭事件，各 4 个，每事件前 2 个市场。
# 此处缩小为各 2 个事件，与随附真实抽样一致。
uv run --locked python -m arb_research.resolution_whales_cli collect \
  --event-limit 2 --markets-per-event 2 --max-pages 2 --output .state/live.json.gz
uv run --locked python -m arb_research.resolution_whales_cli collect \
  --event will-zelenskyy-wear-a-suit-before-july --markets-per-event 1 \
  --max-pages 1 --output .state/disputed.json.gz

# 完全离线复现随附产物；可重复 --input 合并后续采集快照。
uv run --locked python -m arb_research.resolution_whales_cli build \
  --input fixtures/resolution-whales/live-20261030.json.gz \
  --input fixtures/resolution-whales/disputed-20261030.json.gz \
  --output .state/resolution-whales
uv run --locked pytest
```

`collect` 有阶段失败时保存可用数据与 `errors`，退出码为 2；discovery/网络失败会清晰报错。`build` 输出 `features.parquet` 与 `report.json`。默认按每个采集完成时间建立截面；重复 `--as-of <RFC3339>` 可指定 UTC 时间网格。`--threshold-usd` 默认 1000，`--top-n` 默认 10。样本文件名使用采集地日期 2026-10-30（Asia/Shanghai），内部时间为 2026-10-29 UTC。

## 数据源与 ARB-7 对齐

所有 Polymarket 请求都使用显式传入 HTTP 客户端的 `ARB_POLYMARKET_PROXY`，不读全局代理、不修改全局代理、不记录其值。缺少变量时直连，连接失败会指出变量未设置。UMA 使用独立直连客户端，未使用该代理。重定向拒绝；只允许固定主机、固定 GET 路径。每次请求及重试都经过 ARB-7 的跨进程 SQLite 令牌桶（19 req/s、burst 1）及 main 的 `PUBLIC_API_BUDGET`，429/5xx/网络错误做有界退避并遵守有界 `Retry-After`。

采集前复用 main 的 `ComplianceGuard`、`GeoblockClient`。随附实测 `blocked: true`、`trading_allowed: false`，公开读取照常，交易持续拒绝。异常不会暴露代理地址、上游响应全文或私密网络配置。只保存公开地址和金额，丢弃姓名、bio、头像等资料，不做去匿名化。

| 来源 | 用途与参数 |
| --- | --- |
| Gamma `GET /events`、`/events/slug/{slug}` | 市场 ID、condition、协议、规则、分类与 outcome token；协议 v2 使用 `positionIds`，其余使用 `clobTokenIds` |
| Data API `GET /v2/resolutions?condition=…` | 提案、争议、最终价格与 payout；用官方 `polymarket-client==0.15.0` 的 `Resolution.parse_response` 解析 |
| Data API `GET /v2/holders` | `include_pnl=true` 获取每 outcome 的 gross shares；每页每 token 100 行，按 token 合并；不读取经济资料或 profile 字段 |
| Data API `GET /v2/trades` | 每页 1000 行，`taker_only=true`，condition 固定；仅成交公开数据 |
| Data API `GET /v2/prices-history` | `interval=max`，保留每点 `resolution_seconds` 和非均匀终点；通常为 12 小时桶 |
| UMA `GET https://oracle.uma.xyz/api/resolve-deeplink` | 对带问题 ID、交易哈希的已争议记录补充生命周期；要求 Polygon 137、OO V2、问题 ID 与交易哈希同时匹配 |

Data API v2 全部读取 `data` envelope 和 `pagination.next_cursor`；不使用 v1、offset 或旧 CLOB SDK。分页保留全部过滤条件，检测游标循环。holders 检出跨页重复地址时使该阶段失效，避免重复计数；分页不是原子快照，即使游标走完也不能保证全市场原子完整性。trades 的 condition 形态忽略 start/end，必须在本地裁剪；本实现没有把这些参数当成服务端裁剪保证。

已 rebase 到 main `2b737bb`（包含 ARB-7 `1d0da1d`、ARB-8、ARB-15 与 ARB-14）。`PublicAPI` 直接复用 `arb_research.polymarket.client.PublicDataClient`，仅为其固定 GET allowlist 增加 `/v2/holders`（`/v2/trades` 已在 main）；不另建通用同步、存储层、DuckDB、FastAPI 或注册 Dataset。复用 ARB-7 的 SDK 版本、market/condition/token 标识、限流、重试、geoblock guard 和 UTC 约定。UMA 的独立直连查询保留在本研究适配器。

来源：[Data API v2 官方 OpenAPI](https://data-api.polymarket.com/v2/openapi.json)、[官方数据迁移指南](https://docs.polymarket.com/migrate/data-api-v1-to-v2)、[UMA 官方生命周期查询实现](https://github.com/UMAprotocol/optimistic-oracle-dapp-v2/blob/master/src/pages/api/resolve-deeplink/index.ts)。

## 可得时间与防未来函数

1. 每市场 `observed_at` 为该次市场全部读取结束后的本地 UTC 时间。即使成交、UMA 事件发生在几年前，今天查询到的数据也只能从今天起用；从不以 `last_update_timestamp`、成交时间或市场创建时间回填特征可得时间。
2. 对每个 `as_of`，只选择 `observed_at <= as_of` 的最近市场快照。缺失是 null，已观测空列表才是零。禁止不带时区的时间、未来事件、身份不匹配、非有限数值及同一时间冲突快照。
3. 特征表不包含 payout、最终判定时间或事后收益标签。状态与 `was_disputed` 也只有收到对应快照后才可用。按类历史争议率只用当时已观测为 resolved 的其他市场，排除自身。
4. 大额成交阈值在计算前固定；时间窗为 `(as_of-window, as_of]`。大额成交流量是 taker 侧流量，不是全市场资金流或所有巨鲸的总成交。
5. `whale_net_flow_usd` 只用窗口开始前最后一次已收到的持仓快照选 Top-N 地址，然后统计窗口内该固定名单的 taker 成交。没有这样的历史快照时为 null，绝不用今天的持仓者筛选昨天的预测信号。随附只有每市场一个采集时点，因此该字段均为 null；合成序列验证了后续快照计算路径。
6. 今天选择的 Top-N 地址成交明细、最终 payout 与假设持有至结算的收益只在 `report.json` 的描述性统计中，禁止作为历史预测输入。买入、卖出只称为“样本首笔买入/末笔卖出”，不声称是实际建仓/清仓时点。

快照不会自动过期，消费方须按 `as_of-available_at` 限制陈旧度。历史价格桶只用于报告；争议区间价格范围仅纳入完整落在区间内的非零宽度桶，排除终端结算点。12 小时抽样范围不等同于 tick 波动率或区间真实高低点。

## Parquet schema 与读取

合约 `resolution-whales.features.v1`，Arrow schema metadata 包含 `contract` 和 `availability`。每行主键为 `(market_id, as_of)`；生成时去重时间网格。所有 timestamp 都是 `timestamp[us, UTC]`；金额按 pUSD≈USD，shares 为份额。`SCHEMA` 是代码中的权威类型表，nullable 列如下：

| 列 | 类型 | 含义 |
| --- | --- | --- |
| schema_version / market_id / condition_id | string | 合约、Gamma ID、统一 condition |
| as_of / available_at | UTC timestamp | 查询截面与所选市场快照的收到时间 |
| category | string | Gamma 类别或已知顶级 tag；无明确匹配为 unknown |
| rule_subjective / rule_has_source_url | bool | 英文规则是否含 discretion/consensus/credible/substantial/ambiguous；是否有 HTTP URL |
| rule_chars | int64 | 规则字符数；快照元数据同样遵守可得时间 |
| resolution_status / was_disputed | string / bool，nullable | 已知状态及历史争议标记 |
| risk_score | float64，nullable | 可解释状态启发式：0.7×当前争议 + 0.2×extended_review + 0.1×规则更新，截断至 1；不是训练概率 |
| peer_resolved_count / peer_dispute_rate | int64 / float64 nullable | 同类别其他已知 resolved 市场数、其中曾争议比例；分母零为 null |
| top_n | int64 | 集中度及地址名单的 N |
| observed_holder_count / observed_gross_shares | int64 / float64，nullable | 按大小写无关地址跨 outcome 汇总的正持仓人数及份额 |
| top_n_share_of_observed_holdings / observed_hhi | float64，nullable | Top-N 份额占已观察份额比例；HHI=Σ(地址份额/样本总份额)² |
| holders_walk_exhausted | bool | 游标是否走完；不保证原子完整或全市场份额 |
| flow_window_seconds / large_trade_threshold_usd | int64 / float64 | 默认 86400 秒和 1000 USD |
| large_trade_count | int64 nullable | 窗口内超过固定阈值的成交数 |
| large_trade_buy_usd / large_trade_sell_usd / large_trade_net_usd | float64 nullable | 样本买、卖 notional 与差值；notional=size×price |
| large_trade_first_buy_at / large_trade_last_sell_at | UTC timestamp nullable | 窗口内相应大额成交时点 |
| trades_walk_exhausted | bool | 成交游标是否走完；false 时流量只是截断样本，净值不具上下界保证 |
| whale_cohort_available_at / whale_cohort_size | UTC timestamp / int64，nullable | 窗口前固定 Top-N 地址名单的来源时间与地址数 |
| whale_trade_count / whale_net_flow_usd | int64 / float64，nullable | 固定地址名单的窗口内样本成交数、买减卖 notional；继承 trades 覆盖限制 |

集中度分母始终是观察到的 gross holdings，**不是全市场 supply/OI**；缺少全体持有人时可高估。没有“真实全市场集中度”字段。空分母为 null。采样 holders 当前状态也可能包括已结算未领取的余额。

```python
from arb_research.resolution_whales import read_features
import pandas as pd

rows = read_features(".state/resolution-whales/features.parquet",
                     as_of="2026-10-29T18:00:00Z")
features = pd.DataFrame(rows).set_index("market_id")
```

`read_features` 校验完整 Arrow schema 与 metadata、拒绝 `available_at > as_of`，对每市场只返回截止时刻之前的最后一行。ARB-12 可以把结果按市场和决策时间接入其离线策略输入；Nautilus/ARB-14 可以在相同截止时刻转为自定义数据事件。这里提供经过测试的读取边界与普通 Parquet，不改写现有回测引擎、不把这些截面当成 OHLC bars，也不声称已接入现有策略。用户自建 pandas/DuckDB 查询同样必须执行可得时间约束。

## 真实数据统计报告

随附两份 gzip JSON 是公开读取的规范化记录，保留市场规则、地址、成交、价格与分页完成标记。第一批采集于 **2026-10-29 16:55:06–16:55:46 UTC**，按成交量选择已关闭/未关闭各两个事件、每事件前两个市场；另显式加入 Zelenskyy 西服争议市场。该定向选择不代表随机总体。最终共 **9 个市场、5 个已判定（5 个 payout 已知）、4 个 posed**，生成 **45 行 market×时间特征**。每市场只有一份快照；不是 45 个独立市场。

| 分组 | 市场 | 已判定 | 曾争议 / 已知状态 | 50-50 / 已判定 |
| --- | ---: | ---: | ---: | ---: |
| politics | 6 | 2 | 0/6 | 0/2 |
| sports | 2 | 2 | 0/2 | 0/2 |
| unknown（西服案例无顶级类别匹配） | 1 | 1 | 1/1 | 0/1 |
| 含上述主观措辞 | 5 | 3 | 1/5 | 0/3 |
| 不含上述措辞 | 4 | 2 | 0/4 | 0/2 |

总体已观测曾争议率为 **1/9（11.1%）**；如果只以已判定市场为分母则 **1/5（20%）**。未判定市场存在右删失，不能用这些小样本推断总体争议风险。9 个规则文本都未出现 HTTP URL，所以 URL 分组没有对照组；不能从规则中的“可信报道”推出来源缺失风险。

真实争议案例 `546814`：UMA 第二次请求的争议发生于 **2025-07-05 01:09:02 UTC**，该请求结算于 **2025-07-09 00:27:37 UTC**，历时 **343115 秒（95.31 小时）**。这是通过身份核对的单次 OO 请求争议到 UMA 结算时长，不包括前一轮争议，也不是条件代币兑付延迟。区间内完整的 12 小时价格桶最大抽样范围为 **0.0265 USD/份（2.65 美分）**。其他市场没有争议生命周期时长；不将 `was_disputed` 或状态更新时间伪造成起点。

| 市场 ID | 观察地址数 | Top-10 / 样本 gross shares |
| --- | ---: | ---: |
| 253591（Trump 2024） | 400 | 90.41% |
| 253592（Biden 2024） | 252 | 95.88% |
| 546814（Zelenskyy 西服） | 167 | 51.35% |
| 558934（Spain 2026） | 392 | 99.23% |
| 558935（England 2026） | 400 | 81.19% |
| 559652 / 559653（2028 民主党提名） | 357 / 372 | 71.90% / 74.57% |
| 561229 / 561230（2028 总统选举） | 232 / 379 | 49.73% / 65.39% |

成交每市场仅取最近 1–2 页，所有市场的 `trades_walk_exhausted=false`，不能声称全生命周期流量完整。报告同时输出 trades/holders 是否可用、游标是否走完、各 token 价格覆盖标记；采集失败的成交计数和金额为 null，已成功观察到空列表才为零。50-50 分组同时提供 payout 已知/未知数量及已知分母下的比例。各地址的持仓、样本首买/末卖、买卖额见 `example/report.json` 的 `whales_retrospective`，无个人身份归因。

例如西服案例最近 1000 条成交中有 98 笔 ≥1000 USD 的买入，其中 87 笔在预言机最终判定之前，买入成本合计约 **3,094,218.21 USD**；按最终判定假设一直持有的 payout 减成本约 **−6,963.35 USD**。这只是截断样本的逐笔买入反事实，忽略卖出、费用、持仓变动，不是钱包损益或跟单收益。其他市场的同口径结果、地址明细和分组分母一并保存在报告中。

部分旧 UMA 行只有最终 `price`，没有条件 payout/`resolved_at`：仅在 YES/NO 顺序已确认且最终值为 0、0.5×10¹⁸、10¹⁸ 时推导 `[YES,NO]`，标记 `payout_basis=uma_final_price` 和 `oracle_finalized_at`；不能据此宣称已验证链上条件兑付。原生条件 payout 由 SDK 从微单位转换，标记 `condition_payouts`。随附 50-50 实测计数为 0，其 500000/500000 解析及分组在合成 fixture 中验证。

## 验证与限制

离线回归覆盖：可得时间前无行、未来快照不影响历史、固定地址名单不受今日持仓改变影响、未来事件/错误身份拒绝、空值与零、分页完整性/循环、脱敏退避、UMA 独立直连及身份核对、SDK payout 单位、真实 gzip 记录回放、Parquet 往返和截止时间读取。CI 不采外网；实时采集只通过显式 `collect` 命令触发。

2026-10-30 验证：rebase 到 `2b737bb` 后 `uv run --locked pytest` 为 **220 passed、5 skipped、7 warnings**（72.87 秒），包含新增专项 **24 passed**。本次 rebase 保留 main 客户端并仅补充 holders 路径，合并文档索引条目，研究功能代码未变。此前独立只读审查提出的缺失成交误显示为零问题已修复并复审通过，报告可离线逐值复现。复用 ARB-7 客户端实采同一争议市场为 1 个样本、0 错误，guard 持续拒绝交易。

真实样本可以复现计算并证明采集/输出通路；不能证明可交易收益、预测能力或完整市场历史。更长时间的无偏风险估计与地址净流入需要持续采集多时点快照，现有 CLI 已支持离线合并，不在本票创建常驻服务。
