# negRisk 只读扫描器

扫描 Gamma 活跃事件与 CLOB V2 公共盘口，输出按扣费后单位 edge 降序排列的纸面机会。没有下单、签名、钱包、私钥、充值或提现代码。抵押品按 pUSD≈USD 估值。报告只是非原子盘口快照的估计，不能视为成交承诺。

## 运行

从仓库根目录运行，Python 3.12。先在 `research/` 运行 `uv sync --locked` 安装锁定环境（含 HTTPX SOCKS 支持与现有 guard），再回到根目录执行：

```bash
research/.venv/bin/python -m arb_research.negrisk_cli \
  --max-events 10 --max-pages 5 --max-shares 1000 \
  --split-usd 0.01 --merge-usd 0.01 --convert-usd 0.01 \
  --conversion-fee-bps 0 \
  --record .state/negrisk/snapshot.json \
  --json .state/negrisk/report.json --markdown .state/negrisk/report.md

# 已知 Gamma event ID 时可重复传入 --event-id；仍要求事件是活跃 negRisk。
research/.venv/bin/python -m arb_research.negrisk_cli --event-id 12345

# 离线回放；完全不发网络请求。
research/.venv/bin/python -m arb_research.negrisk_cli \
  --fixture .state/negrisk/snapshot.json --json .state/negrisk/replayed.json

# 离线合约示例和测试（示例是合成数据，不是真实扫描记录）。
research/.venv/bin/python -m arb_research.negrisk_cli \
  --fixture research/tests/fixtures/negrisk-contract.json
research/.venv/bin/python -m unittest discover \
  -s research/tests -p test_negrisk.py -v
```

在已有 uv 环境也可 `cd research` 后用 `uv run --locked python -m arb_research.negrisk_cli ...`。已锁定 `httpx[socks]`，不使用全局环境代理。每天由调用方运行一次；日期取报告的 UTC `as_of`，输出路径由调用方选择。本实现不安装 cron 或改变 NAS 调度。

无 `--json` 时向 stdout 输出纯 JSON，Markdown 是可选摘要。返回码 0 表示至少分析了一个组且无请求/解析错误；2 表示参数无效、无组或部分数据失败。0 不表示有套利机会，也不表示所有活跃事件均已覆盖。完整数据且没有正 edge 时，`opportunities` 是空数组。

发现范围默认按 `volume24hr` 降序读取至多 5 页，每页 100 个事件，选至多 10 个活跃 negRisk 事件。报告明确记录截断边界；这不是全市场普查。会检查重复分页并对 event ID 去重。每个选中事件保留完整 markets 列表，不把缺腿偷偷当成完整集合。

## 网络与集成边界

已 rebase 到 main `9bba8c5`（含 ARB-6、ARB-9、ARB-12、ARB-13），复用 `arb_research.compliance.ComplianceGuard`、`GeoblockClient` 和 `PUBLIC_API_BUDGET`。`collect(api, ...)` 把此只读 HTTP 客户端注入 geoblock 检测，并把 guard 的 status 写入报告；不修改 guard 的拒绝规则，不设置交易允许标记。两个接缝为 `collect(api, ...)` 与纯函数 `scan(snapshot, ...)`，票 2 的数据层后续可产生相同 snapshot。JSON 报告供后续真实看板适配消费，前端无需调用交易接口。

官方推荐的 Python SDK 是 [`polymarket-client`](https://docs.polymarket.com/getting-started/python)。本次使用官方文档公开的 GET REST 接口作为轻量适配器，便于统一限速、限定只读代理、记录原始响应且避免引入签名依赖；未引入已归档的 `py-clob-client`。后续 SDK 集成只应使用 `PublicClient`，并保留同一全局限速边界。

请求入口只允许以下 HTTPS GET；拒绝重定向，不读取交易凭据，也不继承全局 HTTP(S)_PROXY：

| 用途 | 接口 |
| --- | --- |
| 出口判定 | `https://polymarket.com/api/geoblock` |
| 事件发现/指定事件 | `https://gamma-api.polymarket.com/events`、`/events/{id}` |
| 完整盘口 | `https://clob.polymarket.com/book?token_id={id}` |
| 市场费用 | `https://clob.polymarket.com/clob-markets/{condition_id}` |

这里只使用 CLOB V2 切换后仍受支持的公开读接口。不要将 CLOB API V2 与市场 `version=v2` 的仓位协议混为一谈：只有显式 `version=v2` 才用 `positionIds`，显式 `version=v1` 才用 `clobTokenIds`。缺失/未知版本隔离，禁止回退；ID 必须为 ASCII 十进制字符串，拒绝数值、负数、指数、十六进制及重复 ID。两类 ID 同时出现时仍只按 version 选取；大整数原样传递。始终按 `outcomes` 标签匹配 YES/NO，不能假设数组第一项为 YES。

进程内所有 API 和重试首先经过本扫描器 10 req/s、突发 10 的桶，并共用 main 的 `PUBLIC_API_BUDGET`；低于通用 9,000 req/10s 及所用端点限额。429/500/502/503/504 与连接错误最多尝试 3 次，指数退避带抖动，处理至多 30 秒的 `Retry-After`；403 不重试。单请求连接/读取 timeout 默认 10 秒，系统 DNS 与多地址重试可能使总耗时更长。多进程部署需由票 2 统一限速，当前桶不跨进程。

`geoblock.blocked=true` 或 country=US 会记录 blocked；请求失败记 unknown。无论地理结果如何，`execution` 永远是 blocked。即使 US/VA 也只读取公开数据，不实现交易，不将网络可达性视为交易许可。根据 Pie 对本票的明确授权，仅从 `ARB_POLYMARKET_PROXY` 读取 SOCKS5h 配置，并显式传给本扫描器 HTTPX client 的 `proxy` 参数；`trust_env=False`。不设置全局代理，不打印该值，不把它写入 snapshot、报告、仓库或异常消息。未配置该变量时使用直连；配置无效会以脱敏错误失败，不偷偷更换出口。该客户端只允许上表公共读接口，禁止接入交易或签名路径。geoblock 经相同只读出口检测并由 main guard 校验，返回的 IP 不保存。main 默认 geoblock 客户端仍保持原有直连行为；本票只注入本扫描器自己的 fetch。

## 估值、费用和深度

以 Q 份完整组合为单位，按每条腿实际消耗的盘口价位计算名义金额与费用：

| kind | 纸面路径 | 费用前收益 |
| --- | --- | --- |
| `buy_yes_basket` | 买入完整事件各结果 YES，持有至结算 | `Q − Σ YES 买入额` |
| `split_sell_yes_basket` | 用 Q 抵押品 split 一条 YES+NO，将 NO 转成其他结果 YES，再出售全部 YES | `Σ YES 卖出额 − Q` |
| `no_to_yes` | 买 NO(i)，转换为其他结果 YES，再按买盘出售 | `Σ 其他 YES 卖出额 − NO(i) 买入额` |
| `buy_binary_merge` | 同一市场买 YES+NO，再 merge 为抵押品 | `Q − YES 买入额 − NO 买入额` |

组内 `yes_ask_sum` / `yes_bid_sum` 为每条 YES 腿卖一/买一之和，`*_deviation = sum − 1`，与实际逐档成交估算分开。缺任意盘口、市场停用、组 ID 不一致时，组合估值禁用；独立的有效二元市场仍可分析。增强版 negRisk、Other 或 placeholder 会使完整组假设失效，其组合 sum 与 deviation 输出 null，并禁用跨市场策略；Other 与 placeholder 本身也不生成二元机会。

每档 taker 费：`shares × feeRate × p × (1−p)`，舍入到 5 位小数，Half Up。优先用 CLOB `fd.r/e`，其次 Gamma `feeSchedule.rate/exponent`。只支持 exponent=1；其他曲线输出错误并跳过该市场，避免套错公式。明确 `feesEnabled=false` 时费率为 0；缺 schedule 但 feesEnabled=true 且明确 category 时，可使用以下类别基线并标为 `category_fee_estimate`：

| 类别 | feeRate |
| --- | ---: |
| Crypto | 0.07 |
| Sports / Economics / Culture / Weather / Other / General | 0.05 |
| Finance / Politics / Mentions / Tech | 0.04 |
| Geopolitics | 0 |

类别未知且无费用配置时跳过市场，不默认为免费。不同体育版本、促销和临时费率以实时 schedule 为准；类别估算可能与实际不同。未扣任何预期 rebate。按聚合价位估费无法重现未知 maker 订单拆分带来的逐撮合舍入差，因此属于纸面估计。

`--split-usd`、`--merge-usd`、`--convert-usd` 是每次操作的固定 USD 费用假设（如 gas）。`--conversion-fee-bps` 使每份 NO 仅产生 `1−bps/10000` 份各其他 YES；限制 `0≤bps<10000`。转换输出比例也参与深度与最小下单量计算。组买入持有到结算不执行 merge，不计 merge 费用；赎回成本/资金占用未建模。默认操作成本为 0，是显式未知成本假设，不能理解为链上操作保证免费；报告始终包含 `operation_cost_assumptions`。

盘口会自行按价格排序并聚合同价深度，买入使用 asks 升序，卖出使用 bids 降序。跨腿可用深度取最小值，并遵守各腿 `min_order_size`、最大扫描规模。只在共同深度断点、最小规模和规模上限中选择估计净利润最大的 Q；最终仅输出净利润大于 0 的候选，按 `net_edge_per_share` 排序。报告里的 `quantity` 是选定估值规模，`depth_capacity_shares` 是快照总容量，不表示这些份额一定可成交。不同候选会共享同一流动性，不能直接相加。

默认排除距 snapshot `as_of` 超过 120 秒或未来超过 5 秒的盘口，单策略腿之间时间差超过 10 秒则不生成机会。时效阈值可调。默认距离 endDate 不超过 48 小时（包含已过 endDate 但仍接受订单的市场）标 `near_resolution`；endDate 不是实际保证结算时间。其他风险包括 `partial_fill`、`non_atomic_snapshot`、`hold_to_resolution`、`unknown_resolution_time`、`incomplete_group`、`augmented_negrisk`、`other_outcome`、`placeholder_outcome`、`category_fee_estimate`。

## JSON v1 合约（票 8）

`schema_version="1.0"`。所有金额、价格、费率、份额及 edge 都是十进制字符串；消费者用 Decimal 解析，不要直接使用二进制浮点进行资金运算。计数和秒/小时配置是 JSON integer。时间是有时区 ISO 8601。缺失可选值用 null，列表没有结果用 `[]`。字段增加可兼容，删除/改名/改语义需提升主版本。

| 顶层字段 | 类型与语义 |
| --- | --- |
| `schema_version` | string，固定 `1.0` |
| `as_of` | string，本次采集结束 UTC 时间；回放使用原时间 |
| `mode`, `currency`, `collateral` | 固定 `read_only`、`USD`、`pUSD≈USD` |
| `geoblock` | `{status, execution, country?, region?}`；status 为 allowed/blocked/unknown，fixture 可为 fixture；execution 固定 blocked |
| `compliance` | main guard 的 `{trading_enabled, trading_allowed, allowlist_configured, mode, geoblock}`；trading_allowed 与 allowlist_configured 固定 false；旧/合成 fixture 可为 `{}` |
| `coverage` | `{max_events?, max_pages?, pages_read?, selected_events?, discovery_exhausted?, selection?}`；采集上限、覆盖计数、是否读尽；不保证瞬时全市场一致性 |
| `assumptions` | `{split_usd, merge_usd, convert_usd, conversion_fee_bps, max_shares, max_book_age_seconds, max_book_skew_seconds, near_resolution_hours, fee_rounding}` |
| `summary` | `{groups_scanned, opportunities, errors}`，integer 计数 |
| `groups` | Group[]，包括不完整组 |
| `opportunities` | Opportunity[]，按净 edge 降序 |
| `errors` | `{reason: string, stage?: string, event_id?: string, market_id?: string, asset_id?: string}[]`；部分失败不静默丢失 |

Group：`{event_id: string, title: string, complete: boolean, risks: string[], yes_ask_sum: decimal|null, yes_bid_sum: decimal|null, yes_ask_deviation: decimal|null, yes_bid_deviation: decimal|null, markets: Market[]}`。complete 只代表未发现缺腿且没有增强版/Other/placeholder 风险，不是法律或结算保证。

Market：`{market_id: string, question: string, neg_risk_market_id: string, fee_rate: decimal, fee_source: "clob"|"gamma"|"fees_disabled"|"category_estimate", risks: string[], yes: Book, no: Book}`。

Book：`{asset_id: string, as_of: string, min_order_size: decimal, best_bid: decimal|null, best_ask: decimal|null, bids: Level[], asks: Level[]}`。Level 为 `{price: decimal, size: decimal}`，含完整已拉取深度，不只卖一/买一。

Opportunity：`{kind: string, event_id: string, anchor_market_id?: string, quantity: decimal, depth_capacity_shares: decimal, gross_profit_usd: decimal, taker_fees_usd: decimal, operation_cost_usd: decimal, net_profit_usd: decimal, net_edge_per_share: decimal, risks: string[], legs: Leg[]}`。kind 取上述四种；anchor 只用于 split 与 NO 转换。`net_profit_usd=gross_profit_usd−taker_fees_usd−operation_cost_usd`，`net_edge_per_share=net_profit_usd/quantity`。

Leg：`{market_id: string, asset_id: string, outcome: "yes"|"no", side: "buy"|"sell", shares: decimal, notional_usd: decimal, fee_usd: decimal}`。leg shares 可能与 Q 不同，因为转换费导致输出折损。

原始 replay snapshot 合约：`{as_of, events: GammaEvent[], books: {[asset_id]: RawCLOBBook}, clob_markets: {[market_id]: RawCLOBMarket}, geoblock, compliance?, coverage, errors}`。原始 snapshot 与前端报告分开保存，原始响应不是前端兼容性承诺。

## 当前验证范围

测试使用 `tests/fixtures/negrisk-contract.json` 合成边界样例和 `tests/fixtures/negrisk-live-20261029.json` 真实公共响应录制；回归时完全禁网。ARB-28 增加 `negrisk-v2-contract.json`（合成 V2 negRisk，不是真实覆盖证据）、`negrisk-rejections.json` 和请求列表断言。覆盖费用/舍入、split/merge/转换、排序与深度、缺腿/Other/增强版、时效、稳定 JSON、限速/退避、显式代理及异常脱敏、main guard 保持拒绝。

2026-10-29 16:40:44 UTC，已通过用户批准的只读客户端完成两个当前活跃组的真实扫描：Balance of Power: 2026 Midterms（event 32228）和 Fed Decision in December?（event 770450），共 10 个二元市场、20 本 YES/NO 盘口，采集/解析错误为 0。上限为 1,000 份，默认操作成本 0（见假设），没有扣费后正 edge 候选；这不代表全市场没有机会。geoblock 为 US/CA、blocked=true，main guard 的 trading_allowed=false。

[原始 JSON 报告](reports/negrisk-2026-10-29.json)和[Markdown 摘要](reports/negrisk-2026-10-29.md)随代码交付，可从 PR 打开。回放命令（历史 JSON 保留当时 schema；当前输出增加身份与跳过诊断，既有估值字段保持一致）：

```bash
research/.venv/bin/python -m arb_research.negrisk_cli \
  --fixture research/tests/fixtures/negrisk-live-20261029.json \
  --json .state/negrisk/replayed-live.json
```

## 官方依据

- [负风险、NO→YES 和增强版约束](https://docs.polymarket.com/concepts/negative-risk)
- [费用公式与类别基线](https://docs.polymarket.com/trading/fees)
- [Gamma 费用 schedule、仓位 ID 与市场详情](https://docs.polymarket.com/market-data/market-details)
- [CLOB V2 市场费用参数](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [官方 Python SDK](https://docs.polymarket.com/getting-started/python)

文档核对日期：2026-10-29 UTC。费率会变化，运行时市场参数优先于这里的类别基线。


## 严格协议与模块资格（ARB-28）

身份统一来自 ARB-27 的 `arb_research.polymarket.assets.select_assets` / `require_assets`。
扫描器的 `assets()` 是该视图的 YES/NO 映射，不再单独解析两类 ID。`collect` 在请求 CLOB 费用和盘口前验证身份与模块；fixture 回放同样检查，预先存在盘口也不能绕过资格。

本扫描器只建模普通 negRisk。V1 要求显式 v1、`negRisk=true` 和非空 `negRiskMarketID`，其有效模块记录为 `v1_ctf`；CTF 原始 module 可缺失，不能提供相冲突的模块。V2 要求显式 v2、同样的 negRisk 成员证据以及显式 `module="neg_risk"`（官方 SDK TokenModule 名称）。`combo`、`binary`、未知/缺失 V2 module 和未经资格验证的 moduleAddress 都跳过，不从 position ID、标题或 YES/NO 外形推断模块；Gamma 若未给足模块证据，报告缺口，不请求该市场盘口。V2 fixture 只证明这一输入契约与纸面算术，没有证明当前公开响应一定包含所需 module。

完整组必须至少有两个有效二元市场，所有返回成员都合格，且协议、账本、chain（若有）、negRisk group 一致；市场/condition/资产不得重复。event group（若有）必须一致；增强版、Other、placeholder、缺腿/停用、身份/费用/盘口失败均禁用跨市场组合。不完整组中的独立有效二元市场仍可产生 `buy_binary_merge`，不产生 basket / split / NO→YES 组合机会。只对返回的完整 markets 列表验证，不能据此证明上游未遗漏未公开成员。

只有组资格通过后才给出 `conversion_ratio=1-conversion_fee_bps/10000`，否则为 null。V1 与 V2 NegRisk 都按每份 NO 转为每个其他结果一份 YES 的公开协议模型，再扣显式配置的比例成本；`0≤bps<10000`，不把该纸面假设声称为实时链上费用。没有执行任何转换。

JSON 保留 `schema_version="1.0"`，新增字段：Market 的 `asset_selection`（完整共享视图）、`protocol_version`、有效 `module` 和按 YES/NO 索引的 `assets`；机会的每条 leg 带同一完整身份。Group 增加 `skipped_markets`、`skipped_reasons`、`conversion_ratio`。拒绝记录保留原始 `version`、`protocol_version`、`asset_kind`、`module`、所选资产和 `raw_identity`，未知版本的 asset kind 为 null；不把非法原始 ID 填进合格资产字段。组跳过也写入 `errors(stage="group")`，CLI 返回 2，Markdown 显示组跳过原因；因此旧不完整扫描的错误计数可能增加。

历史录制及历史报告不改写。离线回归逐字段比较既有金融数值、候选、盘口与原有错误；只允许新增身份/组诊断字段。V1/V2 合成 fixture 使用相同盘口，完整 YES basket 的 Q=20、gross=5 USD、taker fee=0.78050 USD；市场 1 的二元 merge 在固定成本 0.1 USD 时 net=3.34000 USD。转换比例 0.99 时，锚市场 3 的 NO→YES gross=1.5140 USD。

模块与转换依据（核对 2026-10-30）：[官方仓位及模块说明](https://docs.polymarket.com/trading/positions/how-positions-work)、[官方 negRisk 转换与增强版说明](https://docs.polymarket.com/concepts/negative-risk)，以及锁定的 `polymarket-client==0.15.0` 的 `TokenModule` 枚举。代理、共享限速与 ARB-6 guard 沿用原实现；本票没有访问交易、签名或账户路径。
