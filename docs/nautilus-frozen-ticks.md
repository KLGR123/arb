# 冻结成交 → Nautilus 1.231.0（ARB-29）

`arb_research.frozen_nautilus` 是纯离线转换层，只导入 Nautilus 核心类型，锁定 `nautilus_trader==1.231.0`（依赖已锁定，入口另做版本检查）。不创建 HTTP / WS、SDK、原生在线历史 loader、执行客户端或订单。输入不变，输出只有 `BinaryOption` 与按时间全局排序的 `TradeTick`，可交给现有离线引擎。

## 输入契约与示例

身份复用 ARB-7 的 `Event` / `Market` / `Token`，例如已有只读冻结副本的 `store.event(event_id)`。成交使用 ARB-14 schema_version=1 portable snapshot 中已收齐分页的 `markets[].trades`，字段来自 **Data API v2**。ARB-7 `PricePoint` 只有价格和桶宽，缺少成交份额、方向，**不能伪造成 TradeTick**。本模块不连接或写入数据存储，不重新同步、不补页；调用者负责确认冻结来源、完整性和条款证据。

还需按 market ID 提供 `FrozenTerms`：event/market/condition 身份、`known_at_ns`、包含的 `valid_from_ns`、排他的 `valid_until_ns`、预先已知的 `expiration_ns`、明确的 `fees_enabled`、`fee_rate` 和 `fee_exponent=1`。`known_at_ns` 表示这些字段当时全部已知，不是随意填写的回测起点。未知历史费用不能用结算后 Gamma 的 `feeSchedule` 回填；未取得证据的输入不合格。历史条款变化时分段调用，不把一个静态 instrument 的费率套到整个历史。

以下完全离线示例使用受控 fixture，不认证真实市场的历史费率：

```python
import json
from pathlib import Path
from arb_research.frozen_nautilus import FrozenTerms, load_frozen_snapshot
from arb_research.polymarket.models import Event

# 在 research/ 目录运行；fixture 的身份、条款和成交均为 synthetic。
raw = json.loads(Path("tests/fixtures/frozen-nautilus/ctf.json").read_text())
batch = load_frozen_snapshot(
    raw["snapshot"],
    event=Event.model_validate(raw["event"]),
    terms={key: FrozenTerms.model_validate(value) for key, value in raw["terms"].items()},
)
assert len(batch.instruments) == 2
assert len(batch.ticks) == 4
# 单市场已归一化输入也可用 convert_frozen_ticks(market, rows, terms=terms)。
```

## 转换与拒绝边界

| 输入 / 检查 | 输出 / 处理 |
| --- | --- |
| 显式 Protocol `v1`、两个不同的 uint256 CTF ID、outcome index 0/1 | `{condition}-{token}.POLYMARKET`；ARB-7 身份必须与冻结 snapshot 一致 |
| Protocol `v2`、缺版本、unknown、token 协议不一致 | 默认拒绝，**没有 override / 放行开关**；将来需另票的专用 fixture 证明身份、费用和结算输入，再引入放行路径 |
| Data v2 condition/token/可选 outcome 标签和 index | 全部校验；未知 token、跨市场、重复市场、重叠 condition/token 或错误方向拒绝 |
| price / size | Decimal 精确检查，最多 16 / 6 位小数，价格 [0,1]、份额 >0；非有限数、超精度、超 Nautilus Quantity 范围均拒绝，不四舍五入 |
| 整数 epoch 秒 | 精确转 uint64 纳秒；不猜毫秒、不截断小数。全量先校验，再按 `start_ns/end_ns` 两端包含过滤 |
| newest-first 冻结 rows | 反转后稳定排序；跨市场再按 `ts_event` 排序。同秒仅保留确定的录制顺序，无法证明链上亚秒顺序 |
| tx hash + wallet + 规范化经济字段 + occurrence | 稳定 TradeId；同交易内相同 fill 保留重复次数，窗口过滤不改变 ID；不同录制范围间不承诺同一 occurrence 编号 |
| fees_enabled / fee_rate / exponent | maker fee=0、禁用返佣；taker rate 写入 instrument，使用 ARB-9 的 `shares × rate × p × (1-p)` 语义；缺字段、非 1 exponent、免费但非零 rate 拒绝 |
| `known_at_ns <= tick` 且 `valid_from_ns <= tick < valid_until_ns` | 超出历史条款区间拒绝，包括被窗口过滤掉的 rows。已提前宣布但尚未生效的费用不能用于更早成交。instrument 的保守 activation 为已知/生效时间的较大值，`ts_event/ts_init` 为条款已知时间；ticks 的 `ts_init=ts_event` 是理想离线回放时间，未测量历史接收延迟 |

`TradeTick` 本身没有逐笔 commission 字段，本转换只携带历史 instrument 费率，不计算或声称恢复实际手续费。价格 increment 是历史观测精度，不是当时的下单 tick 规则；pUSD 按约 pUSD≈USD 记账，不模拟跨协议抵押品转换。

## 前视隔离与 ARB-9 / ARB-14 的关系

instrument 仅带身份、outcome index、条款时间及固定转换语义；outcome 标签来自冻结身份。`closed`、question、negRisk 捕获状态、winner、outcomePrices、当前报价、resolution payload 和 capture-time fees 均不复制。输出不含 quotes、结算状态或终局研究 metadata；`expiration_ns` 只接受明确属于历史已知条款的值。测试修改终局字段后，早期 instrument/ticks 的完整序列化输出保持相同，并拒绝把晚到条款回填到早期成交。

这落实 ARB-9 的结论：保留 1.231.0 离线核心，绕过其旧 `/trades` 原生历史 loader。ARB-9 的原生 admission 测试仍作为上游缺陷证据；ARB-14 的生产 instrument 入口同步收紧为仅 v1，旧的 synthetic `positionIds` 字段映射不再授予 V2 资格。ARB-14 `load_snapshot` 保留捕获快照的分析 smoke 用途，不能代替此处的历史条款校验。

验证全部离线：`uv run --locked pytest`。新增测试覆盖受控 CTF fixture、ARB-14 BTC/ETH 录制成交（分别 16 / 6 条，搭配**明确 synthetic 的历史条款**）、身份/精度/排序/重复/费用/前视拒绝，以及 fresh process 禁止网络、SDK、transport 和原生适配器依赖。没有取得真实 point-in-time fee 证据，不将这些测试宣传为真实历史费用或结算验证。未改引擎撮合、未启用在线或纸面自动下单，也未修改 ARB-23 录制观察。
