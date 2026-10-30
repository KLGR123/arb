# Kalshi × Polymarket 只读跨所研究

入口：`research/src/arb_research/cross_venue_cli.py`。只使用无需认证的公开 GET；不开户、不登录、不使用 Kalshi key。没有下单、签名、钱包或充值/提现功能。复用 `arb_research.compliance.ComplianceGuard` 获取地区状态，报告的 `execution` 恒为 `blocked`。公开数据可读不表示有交易资格；Kalshi 的中国地区限制仍适用。

## 运行

在 `research/` 内：

```bash
uv sync --locked
uv run --locked python -m arb_research.cross_venue_cli \
  --poly-event-id 770450 --kalshi-event KXFED-26DEC --max-pairs 3 \
  --record .state/cross-venue/snapshot.json \
  --cache .state/cross-venue/rules \
  --json .state/cross-venue/report.json --markdown .state/cross-venue/report.md
```

示例 ID 不是永久有效市场。也可重复指定 `--kalshi-event` 或 `--poly-event-id`；未指定时，Polymarket 按 24 小时成交量读取有限页，Kalshi 按 API 默认顺序读取有限页。`--pages` 限制每侧发现页数，`--max-pairs` 限制进入规则审查的候选数（拒绝后不会自动补位）；不是全市场扫描。JSON 的 `coverage` 记录范围，`exhaustive` 始终为 false；`combinations_considered`、`shortlisted_before_limit`、`shortlist_truncated` 区分笛卡尔组合、初筛及限额截断。无候选或存在数据/模型错误返回退出码 2，完整完成有限范围研究返回 0；全部因规则拒绝或没有正 edge 均可返回 0。

公开 HTTP 客户端仅显式读取 `ARB_POLYMARKET_PROXY`，设置 `trust_env=False`，不修改全局 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。不输出代理值或底层连接异常。缺失该变量时直连；连接失败时先检查公开接口可达性。Kalshi 也仅将同一代理用于其公开只读端点。重定向与非白名单路径被拒绝。所有请求和重试复用 `PUBLIC_API_BUDGET` 全局令牌桶；CLI 两侧还复用 main 上 ARB-7 的 `SharedLimiter`（跨进程 SQLite 令牌桶，较低预算 19 req/s、burst 1），不另建并发限速状态。429/临时 5xx 有有界退避。

## 输入与配对

保留 Gamma event → market → YES/NO token 原始标识、`description`、`resolutionSource`、`endDate` 和协议标记。协议 v2 优先使用 `positionIds`。这是 ARB-7 的原始 Gamma 接口契约；本功能不另建 Parquet/DuckDB 存储，不调用 Data API，不重复实现 ARB-7 的同步层。已与 main 上 ARB-7 的令牌模型和限速器对齐，Gamma 对象可直接传给 `normalize_poly`；其持久化 `Market` 模型未包含关闭时间，因此没有用它代替实时原始元数据。

Kalshi 使用 `/trade-api/v2/events`（嵌套 markets）、`/markets/{ticker}`、`/markets/{ticker}/orderbook`、`/series/{series_ticker}`。事件游标分页检测重复 cursor；单事件响应顶层 markets 为空时使用 event.markets。支持 `KXFED-26DEC-T3.75` 这类带小数阈值的 ticker，仍禁止路径穿越和查询字符串。市场详情保留 `rules_primary`、`rules_secondary`、关闭时间及预期到期时间；系列保留结算来源、条款链接、费率类型和 multiplier。条款 PDF 不会自动抓取，未出现在输入文本中的条款只能视为未知。

初筛需要：双方活跃、关闭时间差不超过 `--max-close-gap-hours`（默认 48 小时）、共享有意义的标题词、标题集合相似度达到 `--min-title-similarity`（默认 0.20）。BTC/Bitcoin、ETH/Ethereum 做别名归一化。数值、日期、标题相似度不是结算等价证据。

## 本地 Codex 规则审查

只调用 NAS 已有登录态的本地 Codex CLI，固定模型 **`gpt-6.1-terra`**，不申请或设置 API key。可用 `ARB_CODEX_BIN` 指向真实 CLI 可执行文件；应避开会注入 API key 的自定义 wrapper。CLI 的继承环境剔除 key/token/secret/proxy 字段，子进程为 read-only sandbox、ephemeral、限时 240 秒；不向模型发送凭据或代理值。prompt 将市场文本标为不可信数据，要求不使用工具、不执行其中指令。

结构化输出：`relation` 为 `same` / `inverse` / `different` / `uncertain`，以及 0–1 对齐度、比较说明、差异数组和解释。`checks` 对 `source`（来源及 fallback）、`deadline`（实际观察截止、时区及窗口）、`boundary`（严格/包含、舍入、取消、平局、50/50）、`underlying`（标的/指数定义、单位、阈值、绝对水平或变化量）逐项给出 `equivalent` / `mismatch` / `unknown`、双边原文证据及解释。交易关闭时间或预期付款时间不能证明观察截止相同；未抓取的 PDF 条款不得猜测。缺少规则不能仅凭标题判定一致。

ARB-20 将这些检查设为前置硬过滤：只有 relation 为 same/inverse、对齐度 ≥0.95、differences 为空、双边规则非空、四项 verdict 全为 equivalent 且各自证据可在所给规则/来源原文中核对，才进入 `pairs`。任何 critical mismatch、unknown、模型不可用、证据缺失或伪造都进入 `rejected_pairs`，列出 `rejection_reasons`、`difference_fields` 和完整 alignment，不请求盘口、不解析盘口、不估值。模型高分不能覆盖字段不一致；证据字符串核对只证明出处，不能保证模型的语义判断正确。inverse 仍要求翻转 YES/NO 后所有 payout 条件完全互补。

缓存键为模型、版本化完整 prompt 和 JSON Schema 的 SHA-256；每次成功记录包含这些输入、输出和时间。当前 prompt 版本 `kalshi-rules-v2`；旧 v1 缓存不能满足四项检查，必须重新比对，不能直接复用放行。规则或 schema 变化产生新键。缓存读取校验 provenance 和输出结构；模型错误不会写入成功缓存。输出虽有记录，模型重新推理不承诺逐字一致；离线缓存重放可精确复现。

```bash
uv run --locked python -m arb_research.cross_venue_cli \
  --fixture .state/cross-venue/snapshot.json --cache .state/cross-venue/rules \
  --json .state/cross-venue/replay.json --markdown .state/cross-venue/replay.md
```

`--fixture` 强制只读缓存，不打外网、不启动模型；缓存缺失报告错误。在线模式也可用 `--cache-only`。缓存仅含公开市场材料，应与对应 snapshot 一起保存。`tests/fixtures/cross-venue-contract.json` 是明确标注的合成契约 fixture，只用于离线测试，不能作真实配对证明。

## 价格、费用与风险

仅通过硬过滤的 `pairs` 才列出两侧 YES/NO 卖一价的同向/反向差和**仍需人工审查的条件性**净 edge。数值对齐度不是校准后的正确概率。在线模式先完成模型审查，再读取存活对的双边盘口和费用，避免模型推理期间盘口变旧。`summary` 给出候选数、拒绝数、存活数、成功估值数、正净价差情景数、各拒绝原因的计数，以及无正净价差原因。拒绝原因可重叠，计数之和不等于拒绝对数。

- 同向买卖：`卖价收入 - 买价支出 - 两边 taker 费用`，分别计算买 P 卖 K、买 K 卖 P；卖出情景假设已有库存，不假设可跨所转仓或裸卖。
- 互补双买：`1 USD - 两边买入支出 - 两边 taker 费用`，仅在互补 payout 假设成立时使用。
- Polymarket 使用逐市场 CLOB/Gamma 权威参数 `q × rate × p × (1-p)`，复用现有五位小数舍入。仅类别估计或未知指数不提供净 edge。
- Kalshi 仅支持 quadratic / quadratic_with_maker_fees 系列，估计为 `ceil_cent(q × 0.07 × multiplier × p × (1-p))`；不是已核实的逐市场实时费用。特别费用计划未自动读取，所以所有结果保留该风险。分档收费按保守单次成交模型处理，未模拟拆单。
- Kalshi 固定小数订单簿只给 YES/NO bids：YES ask = 1 − NO bid，NO ask = 1 − YES bid；兼容旧整数 cents。无盘口时间戳时用本机接收时间并保留此局限。
- `--quantity` 默认 10 个整数合约，按双方最优档可用量向下限制；低于最小订单量不估值，不向深档外推。检查 crossed、非有限数、负数量、资产 ID、过期和跨所时间偏差。默认最大盘口年龄 120 秒、最大时间差 10 秒。

风险：已知不一致、缺规则或模型未验证为 `critical` 并直接拒绝；存活对至少 `high`，保留模型需人工审查、Kalshi 费用估计、中国地区限制等原因。存活对报告预期结算时间差、双边盘口时间/档数/最优档/总数量、流动性截断及盘口/费用缺失。估值前重新检查活跃状态、关闭时间及更新后的标题/时间初筛条件。`endDate` / `expected_expiration_time` 是元数据，不能证明实际结算时间相同。缺失或过期数据抑制净 edge，不填零。所有结果是顺序采样的快照估计，pUSD≈USD；未计资金占用、出入金、汇兑、税务、跨所结算失败或延迟成本，不承诺可执行套利。

## 验证与当前真实证据

```bash
uv run --locked pytest
```

测试离线覆盖：四项字段不一致/未知/证据缺失即拒绝（即使同向、高分、空 differences）、拒绝对不请求/解析盘口、全拒绝扫描成功、存活对扣费后无正 edge、inverse 映射、缓存命中/失效/损坏、固定小数与 cents 盘口、两边费用与舍入、最优档深度、过期/错资产/时差、缺费率、HTTP 白名单/退避/错误脱敏、分页及零网络 CLI 重放。

### ARB-20：仅规则对等配对再扫

2026-10-30 **02:04:54 UTC**，在 NAS 经 `ARB_POLYMARKET_PROXY` 重新读取 Polymarket event `770450`（5 个市场）和 Kalshi event `KXFED-26DEC`（11 个市场）。55 个组合中，10 对通过标题/48 小时窗口初筛；本轮按 `--max-pairs 5` 审查前 5 对，没有自动补位。5 次真实本地 `gpt-6.1-terra` v2 比较均缓存完成，随后再次读取公开元数据并命中这些缓存，CLI 返回 0、errors 为空。

| Polymarket market | Kalshi market | relation / alignment | 结果 |
| --- | --- | --- | --- |
| 3215005 | KXFED-26DEC-T3.50 | different / 0.20 | 拒绝 |
| 3215005 | KXFED-26DEC-T4.50 | different / 0.20 | 拒绝 |
| 3215006 | KXFED-26DEC-T3.25 | different / 0.25 | 拒绝 |
| 3215006 | KXFED-26DEC-T4.25 | different / 0.30 | 拒绝 |
| 3215006 | KXFED-26DEC-T5.25 | different / 0.20 | 拒绝 |

**审查 5 对 / 拒绝 5 对 / 对等 0 对 / 估值 0 对 / 正净 edge 0 个**。5 对均有标的定义、边界和来源差异；4 对截止条件 mismatch，1 对 unknown。核心仍是“会议前后利率变化幅度”不等于“会后绝对利率是否超过阈值”，另有缺失 fallback/舍入等条件。部分模型证据未逐字引用所给原文，也被独立证据核对拒绝。清单保留每项 verdict、原文证据、说明、差异字段和拒绝原因计数。

`pairs: []`，全部归入 `rejected_pairs`。由于没有存活对，硬过滤后**未请求本轮盘口/费用，也未计算条件性净 edge**；结果原因是 `no_equivalent_pairs`，不能填成“扣费后利润为零”。本轮验证了真实元数据→模型缓存→硬过滤的路径；双边费用、深度与正/负 edge 估值由离线合成 fixture 覆盖，未获得真实等价对的估值证据。交易权限始终 blocked。这一有限样本不代表未审查的其余 5 对或全市场没有机会。

实录在 `research/fixtures/cross-venue/20261030-equivalent-only/`：`snapshot.json`、5 个 `rules/*.json`、`report.json`（对等/拒绝清单及计数）、`report.md`。复现在线采样时使用相同的 event 参数及 `--max-pairs 5`，缓存、录制和报告路径指向该目录；fixture 模式精确离线重放见下方命令。

### ARB-17 历史基线（v1）

2026-10-30 01:37:51 UTC，在 NAS 经用户授权的显式公开只读代理完成真实双边扫描。Polymarket event `770450` 有 5 个二元市场，Kalshi event `KXFED-26DEC` 有 11 个市场；默认 48 小时时间窗口、0.20 标题阈值，最多选 3 组，全部读取规则、系列费率和双边盘口。CLI 返回 0，errors 为空，交易权限仍为 blocked。

| Polymarket market | Kalshi market | 模型 relation / alignment | 风险 / 结果 |
| --- | --- | --- | --- |
| 3215005 | KXFED-26DEC-T3.50 | different / 0.25 | critical；排除配对 |
| 3215005 | KXFED-26DEC-T4.50 | different / 0.20 | critical；排除配对 |
| 3215006 | KXFED-26DEC-T3.25 | different / 0.30 | critical；排除配对 |

三组都不是相同结算命题：Polymarket 以会议前后**变化幅度**分档，Kalshi 判断会议后的**绝对利率是否高于阈值**；未声明基准利率时不能相互替代。模型还记录无声明 fallback、舍入、截止时间等差异。元数据中的关闭时间差为 18,240 秒（5 小时 4 分钟），预期结算时间差和薄盘口另见报告。本次没有通过规则等价检查的市场对，因此净 edge 留空，不能把观察到的价格差称为套利，也不能从该有限样本推断全市场没有机会。

实录随代码提交在 `research/fixtures/cross-venue/20261030/`：`snapshot.json` 保存公开原始数据与规范化输入，`rules/*.json` 保存三次真实 `gpt-6.1-terra` 的完整 prompt/schema/输出，`report.json` 与 `report.md` 保存清单、同向/反向卖一差、费用及风险。缓存是在先前同一组真实输入上生成，最终采样重新读取了双边盘口；不含认证信息或代理值。

从 `research/` 精确离线重放当前 ARB-20 v2 实录（ARB-17 v1 仅作为历史材料保留，旧缓存不得放行新过滤器）：

```bash
uv run --locked python -m arb_research.cross_venue_cli \
  --fixture fixtures/cross-venue/20261030-equivalent-only/snapshot.json \
  --cache fixtures/cross-venue/20261030-equivalent-only/rules \
  --json .state/cross-venue/replay.json --markdown .state/cross-venue/replay.md
```

录制 fixture 的回归测试同时禁止 HTTP 和模型子进程，并逐字段比较完整 JSON 输出，验证 prompt/输出缓存可复现。正向/反向费用、深度截断与条件性净 edge 由明确标为合成的契约 fixture 覆盖；没有伪造真实等价市场或正收益。

## 一手资料

- [Kalshi 无认证公开市场数据](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Kalshi orderbook 固定小数与互补语义](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Kalshi 系列结算来源与费用字段](https://docs.kalshi.com/api-reference/market/get-series)
- [Kalshi 费用表及舍入](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Polymarket 费用公式与逐市场参数](https://docs.polymarket.com/trading/fees)
- [Codex CLI 结构化非交互调用](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)
