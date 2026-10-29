# 做市回测原型

ARB-12 提供独立的离线、单 outcome 做市回放：可配置库存报价，使用 L2 快照和模拟 taker 意图近似撮合，输出交易损益、返佣、流动性奖励、结算跳变及成交后价格漂移。内置 Crypto、Sports、Politics 三类**手工合成**样本，均非真实市场、收益预测或已到账奖励。没有交易所客户端、网络请求、认证或订单签名；不读取环境密钥。pUSD 按 1 USD 计。

## 运行

仓库根目录：

```bash
cd research
uv sync --locked
mkdir -p .state
uv run --locked python -m arb_research.mm --sample crypto --output .state/mm-crypto.json
uv run --locked python -m arb_research.mm --sample sports
uv run --locked python -m arb_research.mm --sample politics
uv run --locked pytest tests/test_mm.py -q
```

`--output` 需指向已存在目录下的文件。省略它向 stdout 输出完整 JSON；输入无效、文件读取/写入失败以退出码 2 结束。

自有本地 fixture 和参数覆盖：

```bash
uv run --locked python -m arb_research.mm --fixture path/to/l2.json --config path/to/config.json --output result.json
```

`config.json` 可以只包含需覆盖的字段，例如：

```json
{
  "initial_cash": 1000.0,
  "initial_inventory": 50.0,
  "target_inventory": 50.0,
  "max_inventory": 200.0,
  "order_size": 20.0,
  "center_offset": 0.0,
  "spread": 0.02,
  "inventory_skew": 0.0001,
  "risk_aversion": 0.1,
  "variance_per_second": 0.0001,
  "horizon_seconds": 60.0,
  "taker_latency_ms": 50,
  "markout_horizons_ms": [1000, 5000],
  "max_markout_gap_ms": 1000
}
```

默认 `risk_aversion * variance_per_second * horizon_seconds = 0.0006`。报价中心为 `mid + center_offset - (inventory - target_inventory) * (inventory_skew + risk)`，全价差为 `spread + risk`。这是 Avellaneda–Stoikov 类库存偏移，使用固定方差和固定风险期限，没有标定到达强度或拟合完整最优控制解。

bid 向下、ask 向上按 tick 取整，并限制为被动报价；不合法的边界价不挂单。买单受现金和库存上限限制，卖单受已有 outcome 库存限制，不做空、不借贷、不自动补充资本。起始 outcome 库存按首个 midpoint 计入初始权益，避免把预置库存误当利润。

## Fixture 合约

权威类型在 `research/src/arb_research/mm.py`（`Fixture` / `Config`）；完整示例在 `research/src/arb_research/fixtures/mm/*.json`。输入拒绝额外字段、非有限数、乱序/重复/交叉盘口、未对齐 tick 的价格、超出时间窗口的 taker 以及非法仓位配置。

| 字段 | 语义 |
| --- | --- |
| `schema_version` | 固定 `mm-fixture-v1` |
| `provenance` | 数据来源与录制/合成说明，不可省略 |
| `market.id/category` | 本地市场身份；当前类别 crypto / sports / politics |
| `market.tick_size/min_order_size` | tick（支持 0.000001–0.1）、最低下单 shares |
| `market.fee_rate/rebate_fraction` | 该市场该时期的参数，零费市场设置 fee_rate=0 |
| `market.reward_min_size/reward_max_spread` | 奖励最低剩余 shares、距离 midpoint 的最大距离（概率单位，0.03=3 cents） |
| `market.reward_daily_pool` | 假定每日奖励预算，零表示没有奖励 |
| `books[]` | 至少两份快照；`timestamp_ms` 为 UTC Unix 毫秒，严格递增；`bids/asks` 为降序/升序的唯一 `{price,size}` 层级 |
| `books[].competitor_score` | 其他 maker 的合计 Q_min 假设，不能仅凭 L2 总量推得 |
| `books[].reward_midpoint` | 可选的录制 adjusted midpoint；缺省按外部盘口累计最低奖励量到达的 bid/ask 均值近似；深度不足时不计奖励 |
| `takers[]` | `{timestamp_ms,side,limit_price,size}`，时间为**模拟 taker 发送时刻**；side=buy 吃 ask，sell 吃 bid |
| `end_ms` | 显式结束时间；taker 到达必须早于它；最后快照至结束之间可持有挂单 |
| `settlement_price` | 可选 0/1，运行结束时按 outcome 结算；缺省保持末尾 midpoint 估值 |

行情快照优先于同毫秒到达的 taker，同毫秒 taker 保持文件顺序。延迟加在模拟 taker 的发送时间上，默认 50ms；超过运行末尾的意图不撮合并计入 `takers_arriving_after_end`。

每次 L2 快照原子取消并重报报价、重建外部深度，重置排队优先级。两次快照间按价格优先、同价外部在前进行撮合；已吃掉的外部深度不会在下一笔 taker 中复用。到价本身不代表成交，必须有 taker 消耗。快照间订单不因成交重报，剩余量会参与后续撮合和奖励计算。`quotes[].bid/ask.queue_ahead` 是报单瞬间更优价及同价外部总量。

这是小型事件模拟，不是逐订单 L3 撮合：没有撤单位置估计、订单消息延迟、隐含流动性、真实 cancel/replace、随机队列、市场冲击或自身交易对未来录制行情的反馈。下一份快照覆盖外部剩余量，视为新的盘口事实。稀疏录制会夸大挂单可用时长，需在数据准备时裁剪断流。

## 返佣和奖励假设

核对日期：2026-10-30。官方 [Fees](https://docs.polymarket.com/trading/fees) 与 [Maker Rebates](https://docs.polymarket.com/programs/maker-rebates) 的当前示例参数为 Crypto 0.07 / 20%、Sports 0.05 / 15%、Politics 0.04 / 25%（fee_rate / rebate_fraction）；参数可变，类别本身不证明某个市场费率生效。样本参数仅用于演示。

maker 自身费用为零。每笔成交以 `shares * fee_rate * price * (1-price)` 估算 fee equivalent，decimal half-up 保留五位，再乘 rebate_fraction。它等价于假定当日返佣池恰为全市场 fee equivalent 的对应比例；没有完整的全市场费池，不能验证实际分配。

奖励规则参考官方 [Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)：单边按价差平方衰减；中央价格区间单边分数可按 1/3 计分，极端区间要求双边。原型只计一个 outcome，不合并互补 outcome 的订单，in-game multiplier 固定 1。

与官方随机分钟采样相比，原型将事件间剩余挂单视为恒定，连续积分 `daily_pool * own_Q_min / (own_Q_min + competitor_score) * seconds / 86400`。奖励最低量检查剩余挂单而非原始量，成交后及时降低分数。跨 UTC 0 点的区间拆入两天，末尾未覆盖整天不外推。`reward_scoring` 给出合格时长与分数积分，`daily_accruals` 给出逐日应计。

返佣和奖励都是**估算应计**，不会加入可继续买入的现金。没有模拟官方最低 $1 支付门槛、UTC 0 点发款、延期、资格检查或真实 Rewards API。分钟采样、对手身份、完整日数据和互补盘口不足时，收益估算有偏差。

## JSON 结果与 PnL

结果固定 `schema_version=mm-result-v1`、`mode=offline_approximation`。包含原参数、来源、规范化 fixture 的 SHA-256、逐次报价、成交、权益轨迹及分日应计，供 ARB-8 后续展示消费。该原型尚未接入 Web 或 `/backtests` 双引擎合约，也没有宣称使用 NautilusTrader；迁移决策留待相关技术评估。

`pnl` 五项及总数均按 pUSD：

| 字段 | 计算与符号 |
| --- | --- |
| `trading_pnl` | 末尾现金 + 库存 × 末尾 midpoint − 初始权益，包含已实现及未实现损益，不含应计奖励与结算 |
| `maker_rebate` | 正数，应计返佣 |
| `liquidity_rewards` | 正数，应计流动性奖励 |
| `settlement_jump` | 若已指定 outcome，结算前库存 × (outcome − 末尾 midpoint) |
| `fees` | 正数代表成本；当前仅 maker 成交，因此为 0 |
| `total` | trading_pnl + maker_rebate + liquidity_rewards + settlement_jump − fees |

`ending_equity_with_accruals = initial_equity + pnl.total`；发生结算时本地账本将 outcome 库存兑换为现金并清零库存，保留 `inventory_before_settlement`。JSON 使用有限浮点数；它是分析输出，不是账务精度结算单。

每个成交的 `markouts` 按配置的 N 毫秒查找首个不早于目标时刻的盘口；偏差超过 max_markout_gap_ms 或数据不足时为 null，汇总记录 missing_fills，禁止把缺数当零。买入符号 +1、卖出 −1：

- `signed_midpoint_drift = sign * (future_mid - fill_mid)`，负数表示逆向选择。
- `adverse_selection_cost = -signed_midpoint_drift * shares`，正数表示损失。
- `execution_markout = sign * (future_mid - execution_price) * shares`，同时包含买卖价差收益。

汇总给出数量加权 drift 和总金额；这些诊断不再计入 PnL，避免重复计价。使用的是未来 L2 midpoint，结算跳变单独报告。

## 后续录制与验证

接入录制数据时保留来源、时间和市场当时的 tick / 奖励 / 费率参数，并显式处理停机间隔。实际交易 tape 的时间已是交易所成交时刻，需设置 taker_latency_ms=0，避免二次延迟；将成交方向映射为 taker side，成交价作为 limit，仅是另一种近似，不能恢复完整 taker 扫单意图。本模块只读取本地文件；若后续另建采集 adapter，使用 V2 之后的公开接口与 `polymarket-client`，实施全局限流/退避，不增加任何签名或下单路径。

`tests/test_mm.py` 覆盖队列消耗、部分成交、限价、被动取整、延迟、资本/库存、奖励阈值与午夜、返佣、PnL/结算对账、双方向 markout、缺数、输入拒绝、三类样本确定性、CLI 和禁止网络访问。现有 API/双引擎测试继续单独覆盖原有能力。
