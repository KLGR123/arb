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

示例 ID 不是永久有效市场。也可重复指定 `--kalshi-event` 或 `--poly-event-id`；未指定时，Polymarket 按 24 小时成交量读取有限页，Kalshi 按 API 默认顺序读取有限页。`--pages` 限制每侧发现页数，`--max-pairs` 限制模型调用与盘口请求；不是全市场扫描。JSON 的 `coverage` 记录范围，`exhaustive` 始终为 false。无候选或存在数据/模型错误返回退出码 2，完整完成有限范围研究返回 0；没有正 edge 不是程序失败。

公开 HTTP 客户端仅显式读取 `ARB_POLYMARKET_PROXY`，设置 `trust_env=False`，不修改全局 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。不输出代理值或底层连接异常。缺失该变量时直连；连接失败时先检查公开接口可达性。Kalshi 也仅将同一代理用于其公开只读端点。重定向与非白名单路径被拒绝。所有请求和重试复用 `PUBLIC_API_BUDGET` 全局令牌桶；CLI 两侧还复用 main 上 ARB-7 的 `SharedLimiter`（跨进程 SQLite 令牌桶，较低预算 19 req/s、burst 1），不另建并发限速状态。429/临时 5xx 有有界退避。

## 输入与配对

保留 Gamma event → market → YES/NO token 原始标识、`description`、`resolutionSource`、`endDate` 和协议标记。协议 v2 优先使用 `positionIds`。这是 ARB-7 的原始 Gamma 接口契约；本功能不另建 Parquet/DuckDB 存储，不调用 Data API，不重复实现 ARB-7 的同步层。已与 main 上 ARB-7 的令牌模型和限速器对齐，Gamma 对象可直接传给 `normalize_poly`；其持久化 `Market` 模型未包含关闭时间，因此没有用它代替实时原始元数据。

Kalshi 使用 `/trade-api/v2/events`（嵌套 markets）、`/markets/{ticker}`、`/markets/{ticker}/orderbook`、`/series/{series_ticker}`。事件游标分页检测重复 cursor；单事件响应顶层 markets 为空时使用 event.markets。支持 `KXFED-26DEC-T3.75` 这类带小数阈值的 ticker，仍禁止路径穿越和查询字符串。市场详情保留 `rules_primary`、`rules_secondary`、关闭时间及预期到期时间；系列保留结算来源、条款链接、费率类型和 multiplier。条款 PDF 不会自动抓取，未出现在输入文本中的条款只能视为未知。

初筛需要：双方活跃、关闭时间差不超过 `--max-close-gap-hours`（默认 48 小时）、共享有意义的标题词、标题集合相似度达到 `--min-title-similarity`（默认 0.20）。BTC/Bitcoin、ETH/Ethereum 做别名归一化。数值、日期、标题相似度不是结算等价证据。

## 本地 Codex 规则审查

只调用 NAS 已有登录态的本地 Codex CLI，固定模型 **`gpt-6.1-terra`**，不申请或设置 API key。可用 `ARB_CODEX_BIN` 指向真实 CLI 可执行文件；应避开会注入 API key 的自定义 wrapper。CLI 的继承环境剔除 key/token/secret/proxy 字段，子进程为 read-only sandbox、ephemeral、限时 240 秒；不向模型发送凭据或代理值。prompt 将市场文本标为不可信数据，要求不使用工具、不执行其中指令。

结构化输出：`relation` 为 `same` / `inverse` / `different` / `uncertain`，以及 0–1 对齐度、数据源比较、截止时间比较、边界条件比较、差异数组和解释。重点包含来源优先级、时区、观察窗口、严格/包含边界、取消、平局、50/50 及 fallback。缺少规则不能仅凭标题判定一致。

缓存键为模型、版本化完整 prompt 和 JSON Schema 的 SHA-256；每次成功记录包含这些输入、输出和时间。规则或 schema 变化产生新键。缓存读取校验 provenance 和输出结构；模型错误不会写入成功缓存。输出虽有记录，模型重新推理不承诺逐字一致；离线缓存重放可精确复现。

```bash
uv run --locked python -m arb_research.cross_venue_cli \
  --fixture .state/cross-venue/snapshot.json --cache .state/cross-venue/rules \
  --json .state/cross-venue/replay.json --markdown .state/cross-venue/replay.md
```

`--fixture` 强制只读缓存，不打外网、不启动模型；缓存缺失报告错误。在线模式也可用 `--cache-only`。缓存仅含公开市场材料，应与对应 snapshot 一起保存。`tests/fixtures/cross-venue-contract.json` 是明确标注的合成契约 fixture，只用于离线测试，不能作真实配对证明。

## 价格、费用与风险

每行同时列出两侧 YES/NO 卖一价的同向/反向差。仅当模型判为 `same` 或 `inverse`、对齐度至少 0.95、差异列表为空且双方规则非空，才进一步列出**仍需人工审查的条件性**净 edge。数值对齐度不是校准后的正确概率。

- 同向买卖：`卖价收入 - 买价支出 - 两边 taker 费用`，分别计算买 P 卖 K、买 K 卖 P；卖出情景假设已有库存，不假设可跨所转仓或裸卖。
- 互补双买：`1 USD - 两边买入支出 - 两边 taker 费用`，仅在互补 payout 假设成立时使用。
- Polymarket 使用逐市场 CLOB/Gamma 权威参数 `q × rate × p × (1-p)`，复用现有五位小数舍入。仅类别估计或未知指数不提供净 edge。
- Kalshi 仅支持 quadratic / quadratic_with_maker_fees 系列，估计为 `ceil_cent(q × 0.07 × multiplier × p × (1-p))`；不是已核实的逐市场实时费用。特别费用计划未自动读取，所以所有结果保留该风险。分档收费按保守单次成交模型处理，未模拟拆单。
- Kalshi 固定小数订单簿只给 YES/NO bids：YES ask = 1 − NO bid，NO ask = 1 − YES bid；兼容旧整数 cents。无盘口时间戳时用本机接收时间并保留此局限。
- `--quantity` 默认 10 个整数合约，按双方最优档可用量向下限制；低于最小订单量不估值，不向深档外推。检查 crossed、非有限数、负数量、资产 ID、过期和跨所时间偏差。默认最大盘口年龄 120 秒、最大时间差 10 秒。

风险：已知不一致、缺规则或模型未验证为 `critical`；其他候选至少 `high`，保留模型需人工审查、Kalshi 费用估计、中国地区限制等原因。另外报告预期结算时间差、流动性截断及盘口/费用缺失。即使规则已排除，也保留双边盘口时间、档数、最优档和总数量，并标明深度不足。估值前重新检查活跃状态、关闭时间及更新后的标题/时间初筛条件。`endDate` / `expected_expiration_time` 是元数据，不能证明实际结算时间相同。缺失或过期数据抑制净 edge，不填零。所有结果是顺序采样的快照估计，pUSD≈USD；未计资金占用、出入金、汇兑、税务、跨所结算失败或延迟成本，不承诺可执行套利。

## 验证与当前真实证据

```bash
uv run --locked pytest
```

测试离线覆盖：规则差异及 inverse 映射、缓存命中/失效/损坏、固定小数与 cents 盘口、两边费用与舍入、最优档深度、过期/错资产/时差、缺费率、HTTP 白名单/退避/错误脱敏、分页及零网络 CLI 重放。

2026-10-30 01:37:51 UTC，在 NAS 经用户授权的显式公开只读代理完成真实双边扫描。Polymarket event `770450` 有 5 个二元市场，Kalshi event `KXFED-26DEC` 有 11 个市场；默认 48 小时时间窗口、0.20 标题阈值，最多选 3 组，全部读取规则、系列费率和双边盘口。CLI 返回 0，errors 为空，交易权限仍为 blocked。

| Polymarket market | Kalshi market | 模型 relation / alignment | 风险 / 结果 |
| --- | --- | --- | --- |
| 3215005 | KXFED-26DEC-T3.50 | different / 0.25 | critical；排除配对 |
| 3215005 | KXFED-26DEC-T4.50 | different / 0.20 | critical；排除配对 |
| 3215006 | KXFED-26DEC-T3.25 | different / 0.30 | critical；排除配对 |

三组都不是相同结算命题：Polymarket 以会议前后**变化幅度**分档，Kalshi 判断会议后的**绝对利率是否高于阈值**；未声明基准利率时不能相互替代。模型还记录无声明 fallback、舍入、截止时间等差异。元数据中的关闭时间差为 18,240 秒（5 小时 4 分钟），预期结算时间差和薄盘口另见报告。本次没有通过规则等价检查的市场对，因此净 edge 留空，不能把观察到的价格差称为套利，也不能从该有限样本推断全市场没有机会。

实录随代码提交在 `research/fixtures/cross-venue/20261030/`：`snapshot.json` 保存公开原始数据与规范化输入，`rules/*.json` 保存三次真实 `gpt-6.1-terra` 的完整 prompt/schema/输出，`report.json` 与 `report.md` 保存清单、同向/反向卖一差、费用及风险。缓存是在先前同一组真实输入上生成，最终采样重新读取了双边盘口；不含认证信息或代理值。

从 `research/` 精确离线重放：

```bash
uv run --locked python -m arb_research.cross_venue_cli \
  --fixture fixtures/cross-venue/20261030/snapshot.json \
  --cache fixtures/cross-venue/20261030/rules \
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
