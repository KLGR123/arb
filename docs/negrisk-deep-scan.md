# negRisk 加深扫描（ARB-22）

在 ARB-10 的两组扫描上扩大样本，并保留每种策略的负净收益及无法估值原因。只读 Gamma、CLOB V2 公共盘口和 Data API v2 结算状态；不构造账户、签名或执行客户端。抵押品按 pUSD≈USD 计。

## 采样与边界

默认按 Gamma `volume24hr` 降序分页，每页 100 个活跃、未关闭事件，最多 5 页。从中选择 `negRisk:true` 且含 2–12 个市场的前 30 个事件。市场数限制包含已关闭腿，不通过截取子集把大组伪装成完整组。JSON 的 `coverage.census` 保存每个已访问事件的选择/跳过原因，`discovery_exhausted` 表示是否读到短页；达到目标或分页上限不代表穷尽全市场。

发现后逐事件重新读取完整元数据、每条活跃腿的费用和 YES/NO 盘口，再逐腿读取 `/v2/resolutions?condition=...`。每组单独保存盘口估值时间 `as_of` 和后续结算检查完成时间，避免把整轮采样结束时间误用为所有盘口的参考时间。每条腿都要求唯一资产与 condition、合法深度、权威费率；只有与同一 condition 对应的 `posed`、未争议且未延长审查的结算记录参与估值。缺失/空结算记录也明确排除，不能等同“未结算”。

所有市场同属已核验 negRisk 组、腿数不少于二、无缺腿且非 augmented / Other / placeholder，才估值完整 YES 篮子和跨腿转换。增强型组仍可估值已命名普通二元市场的 YES+NO 合并。单腿失效可保留其他二元市场的估值，同时禁止不完整跨腿组合。JSON 同时列出每组原始市场数、成功验证市场数、完整性与卡点；扫描组数不等于完整篮子数。

默认盘口年龄上限 120 秒、组合内盘口时差上限 10 秒、最多 1,000 个篮子单位。逐档深度估值，候选数量含最小订单量、公共深度上限和各腿深度断点，以**返佣前净 USD 利润最大**选择数量，保留负值。空盘口、时差或最小量问题的毛/净 edge 为 `null` 并给原因。每组展示净 USD 利润最大的候选，其毛/净 edge 必须取同一策略、同一数量；其他策略和 anchor 完整保留在 JSON。不同策略的单位对应不同篮子，不作收益率排名。

## 与 ARB-6 / ARB-17 的费用及合规口径

复用 `arb_research.negrisk.taker_fee`，与 ARB-17 的 Polymarket 腿完全相同：`shares × rate × price × (1-price)`，每价格档按 half-up 舍入到五位小数。费率优先 CLOB `fd`，其次 Gamma `feeSchedule` 或明确的 `feesEnabled:false`。不接受类别猜测或非 1 指数。这里研究独立市场腿构成的纸面篮子，不调用原生 Combos 产品，也不套用原生 Combos 的专用费率。

`gross_profit_usd = 卖出收入 − 买入支出 + 抵押品净额`；`net_profit_usd = gross_profit_usd − taker_fees_usd − operation_cost_usd`。`gross_edge_per_share` / `net_edge_per_share` 都除以本策略的 `quantity`。分拆、合并、转换成本由参数单列，转换损耗用 `conversion_fee_bps` 缩减输出腿。真实报告采用每次 split/merge/convert 各 $0.01 的假设成本，转换损耗为 0 bps；这些是假设，不是链上实测。

基准返佣为零，与 ARB-17 对齐。全是 taker 情景，不能把 maker 奖励算进收益。可选 `--rebate-scenario-rate` 范围 0–0.5，只对同一组 fills 的 taker 费用给出敏感性情景：`net_after_rebate_scenario_usd = net_profit_usd + taker_fees_usd × rate`。返佣前/后分别列出，情景不改变基准数量选择、机会计数或合规权限。扫描不读取账户、等级和资格，不声称返佣可得，也不计等级奖金、最低派发门槛或到账时延。

直接复用 ARB-6 的 `ComplianceGuard` / `GeoblockClient`，每组保留归一化地区结果，剔除原始 IP；任何 geoblock 结果下都保持 `trading_allowed:false` 和 `execution:blocked`。未修改 guard 语义。

## 执行与重放

在 `research/` 中运行：

```bash
uv run --locked python -m arb_research.negrisk_deep \
  --max-events 30 --max-pages 5 --max-markets 12 \
  --split-usd 0.01 --merge-usd 0.01 --convert-usd 0.01 \
  --record fixtures/negrisk-deep/20261030/snapshot.json.gz \
  --json ../docs/reports/negrisk-deep-2026-10-30.json \
  --markdown ../docs/reports/negrisk-deep-2026-10-30.md

# 完全离线，不发外网请求；输出可逐字段比对提交的 JSON。
uv run --locked python -m arb_research.negrisk_deep \
  --fixture fixtures/negrisk-deep/20261030/snapshot.json.gz \
  --split-usd 0.01 --merge-usd 0.01 --convert-usd 0.01 \
  --json .state/negrisk-deep/replay.json

uv run --locked pytest
```

沿用现有只读 HTTPX 适配层：官方 `polymarket-client` 的公开客户端不支持传输注入，显式代理、统一限流与逐请求退避需在该适配层完成；不引入旧 SDK。`ARB_POLYMARKET_PROXY` 只传给公开只读客户端的 `proxy=`，`trust_env=False`、不跟重定向，禁止设置全局代理。变量未设则直连，`network_mode` 明确记录变量缺失；错误只保留脱敏原因。所有服务和重试共享 `PUBLIC_API_BUDGET`（低于通用 9,000/10s 上限）及保守的进程令牌桶。

退出码 0 表示非空且没有采集/市场验证错误；2 表示空扫描或有缺口。增强型组被排除本身不是传输错误；已关闭/非接单腿、缺盘口、缺费率、结算不符则不能伪装成完整成功。应结合覆盖字段阅读，不能把 `null` 当零收益。

## 真实扫描证据

完整逐组表见 [2026-10-30 报告](reports/negrisk-deep-2026-10-30.md)，逐策略估值、盘口卡点和选择清单见 [JSON](reports/negrisk-deep-2026-10-30.json)，公开原始快照保存在 `research/fixtures/negrisk-deep/20261030/snapshot.json.gz`。采样结果与覆盖统计随该报告列出。

2026-10-30 **02:01:18–02:05:51 UTC**，NAS 在已配置的显式只读代理下扫描。读取 3 页共 300 个不同事件，选中 30 个组（ARB-10 的 15 倍）；159 个事件不是活跃 negRisk，91 个超出 2–12 市场范围，另 20 个因已达组数上限跳过，未穷尽发现页。

| 覆盖与结果 | 数量 |
| --- | ---: |
| 扫描组 / 原始市场 | 30 / 194 |
| 结构完整组 / 成功验证市场 | 20 / 153 |
| 可估值候选 / 正净收益候选 | 191 / 0 |
| 可估值二元合并 / YES 买入篮子 | 120 / 11 |
| 可估值 split-sell / NO 转 YES（含不同 anchor） | 30 / 30 |
| 因所需盘口侧为空 / 组内时差而无估值的候选 | 193 / 49 |

全部 191 个可估值候选的毛 edge 已不为正，扣费和操作成本后也均不为正。这里的候选数含相同组内不同 anchor，并非独立事件数。20 个结构完整组中，只有 11 个能对买入 YES 篮子给出数值；其他组受盘口侧为空或时差限制，不能把“结构完整”当作有足够深度。

逐市场验证记录 41 条排除：33 条为关闭/未接单/不活跃（其中包含结算过滤后的腿），8 条盘口过期。结算校验另明确记录 12 条非干净 `posed` 状态，与前述市场排除重叠。10 个不完整组中有 4 个增强型组；仍保留可用二元市场估值。全部 30 组的归一化 geoblock 结果为 `blocked:true`、US，交易保持拒绝。扫描及离线 CLI 返回 2 是因为保留了这些验证缺口，不是外网采集失败；没有采集阶段错误。

这些是顺序采样的纸面上界估计，非原子成交。未计部分成交、价格变化、排队、资金占用、结算延迟、pUSD 脱锚、税务或基础设施成本；没有执行或链上成本验证。无正净 edge 只适用于本次有界覆盖与费用假设，不能推断全市场。ARB-10 的 v1 JSON 和既有实录保持兼容，新报告使用独立 `negrisk-deep.report.v1` schema。

## 一手资料

- [Polymarket 费用公式、精度与 Combos 独立费率](https://docs.polymarket.com/trading/fees)
- [Taker 返佣资格、等级与每日派发](https://docs.polymarket.com/programs/taker-rebates)
- [负风险转换与增强型组的限制](https://docs.polymarket.com/concepts/negative-risk)
- [Data API v2 envelope、condition 筛选与 v1 退休](https://docs.polymarket.com/migrate/data-api-v1-to-v2)
