# Nautilus Polymarket 适配器验证（ARB-9）

验证日期：2026-10-30（Asia/Shanghai）。运行 Python 3.12.15、锁定的 `nautilus_trader==1.231.0`，未升级引擎。

结论：**保留 1.231.0 作为离线回测引擎；当前不能把它的原生历史 loader 用于新在线数据管线。** 下一步建议单独实现统一 SDK `polymarket-client` 的只读 Data API v2 loader，转换为引擎 `TradeTick`。升级 2.0 rc 只能作为后续评估，不能据文档直接判定已支持 Protocol V2。

本票使用 Done when 中“跑通，或明确记录失败点”的后者交付真实历史验证：**没有完成已结算、已确认 v1 事件的真实历史回测**。已跑通的是原生 factory → fixture 成交解析 → BacktestEngine → PolymarketFeeModel 的离线机制验证。下表、数据来源与失败点将两者分开。

## 实测环境与复现

从仓库根目录执行：

```bash
cd research
uv sync --frozen --extra polymarket-validation
uv run --frozen --extra polymarket-validation python scripts/validate_polymarket.py --output .state/polymarket-validation.json
uv run --frozen --extra polymarket-validation pytest -q
```

默认脚本完全离线。持久结果为 `research/fixtures/polymarket/validation-result.json`；`active-sample.json` 保存公开抽样事实投影，`settled-metadata.json` 标注上游来源和人工构造部分。普通 `uv sync --frozen` 不安装验证 extra；不带 extra 时三个依赖适配器的测试跳过，抽样器测试仍运行。

原有依赖集合下，`from nautilus_trader.adapters.polymarket.loaders import PolymarketDataLoader` 报 `ModuleNotFoundError: py_clob_client_v2`。包的 `__init__.py` 提前导入 factories，导致纯数据路径也需要这个包。因此增加**可选** extra，精确锁定 `py-clob-client-v2==1.0.2`。这是 V2 SDK，并非禁止使用的归档包 `py-clob-client`；没有构造 SDK client、执行客户端、签名器或配置任何密钥。当前版本的数据 factory 也不能直接换成统一 SDK，因此其在线调用没有启用。

验证以安装 wheel 的实际签名、源码和执行输出为准。上游 v1.231.0 tag 解引用到 [`27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`](https://github.com/nautechsystems/nautilus_trader/tree/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317)。最新文档是变化中的预发布接口说明，本票**未安装或实测 2.0 rc wheel**。

## 1.231.0 与当前文档的差异

右列来自 [Nautilus 当前 Polymarket 文档](https://nautilustrader.io/docs/latest/integrations/polymarket/)，不是对某个 2.0 二进制的验证承诺。

| 项目 | 1.231.0 安装包实测 / 源码检查 | 当前文档（预发布接口） |
| --- | --- | --- |
| 实现及导入 | Python adapter；loader 在 `.polymarket.loaders`，费用模型在 `.polymarket.fee_model`；根包导入要求 V2 SDK extra | Rust adapter，Python 提供配置和绑定；无额外 adapter extra |
| 工具类型 | factory 产生 `BinaryOption`，本例 quote 为 `pUSD` | `BinaryOption` / pUSD |
| 盘口 | 源码含 `L2_MBP` 本地聚合簿和订阅支持；本票未做 WebSocket / 历史 L2 验证 | 支持 L2 市场数据 |
| `from_event_slug` | async `(slug, token_index=0, http_client=None, sanitize_info=False)`，可注入离线响应 | Rust 网络方法；公开示例没有上述 Python HTTP 注入参数 |
| `load_trades` | `(start: pd.Timestamp=None, end: pd.Timestamp=None)`；无 `limit` 参数 | `start` / `end` 与 `limit` |
| 历史路由 | Data API `/trades`，`market` + `offset`；camelCase；客户端时间过滤、排序 | Data API v2，cursor，snake_case |
| protocol 检查 | 将完全相同的 fixture 分别标记缺失 / `v1` / `v2`，三者均被当作 CTF `BinaryOption` 接受 | 要求 Gamma / CLOB 都为 `v1`，不支持 v2 / 缺失版本 |
| 结算信息 | 需显式 `sanitize_info=True`；`winner` / `closed` 移至 `resolution_metadata`，token 的终局 `price=0/1` 仍留在 instrument.info | 历史结算信息与策略可见 metadata 分离 |
| 费用模型 | `PolymarketFeeModel(maker_rebates_enabled=True, config=None)`；读 `instrument.taker_fee`；未指定 liquidity side 也走 taker | 从 instrument `fee_schedule` 读取参数并验证输入 |
| maker 返佣 | 默认启用估算；按标签 / 费率推断，非 crypto 的旧推断可能过时；本例显式关闭 | 按 schedule rebateRate 估算 |

源码依据：[loader](https://github.com/nautechsystems/nautilus_trader/blob/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317/nautilus_trader/adapters/polymarket/loaders.py)、[费用模型](https://github.com/nautechsystems/nautilus_trader/blob/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317/nautilus_trader/adapters/polymarket/fee_model.py)、[parsing](https://github.com/nautechsystems/nautilus_trader/blob/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317/nautilus_trader/adapters/polymarket/common/parsing.py)、[data client](https://github.com/nautechsystems/nautilus_trader/blob/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317/nautilus_trader/adapters/polymarket/data.py)。源码检查不等同于在线功能通过。

**接受 v2 fixture 不等于支持 Protocol V2**：旧解析器没有做相应拒绝，仍以 CTF token 构造工具。不能用“没抛异常”作为覆盖证明。结算 metadata 的已知字段清理也不构成完整防前视保证；本例策略不读取 instrument.info，更不据终局价格做交易决策。

## 已结算事件尝试与失败点

选择了上游已有 closed fixture 的 Katana `$1B` 市场（Gamma ID `1460332`，condition `0x8ccc…78bb6`）：Gamma 为 resolved、closedTime 为 `2026-03-19 23:20:15+00`，CLOB 的 No token 标为 winner。上游记录没有 `version`，因此**不能将它认证为 v1**。

真实历史路径停止于以下可核验问题：

1. NAS 对公开 Gamma 和 geoblock 端点多次直接 HTTPS 连接超时（包含 IPv4 重试），没有得到可用的在线市场 / 位置判定。本票没有把超时误报为 US/VA geoblock，也没有使用代理或 VPN。
2. 更直接的产品限制：锁定版 `fetch_trades` 固定调用 `https://data-api.polymarket.com/trades`，发送 `market`、`limit`、`offset`。官方 [Data API v2 迁移说明](https://docs.polymarket.com/migrate/data-api-v1-to-v2) 标明 v1 在 **2026-10-24 退役**；v2 改为 `/v2/trades`、响应 envelope、cursor 与 snake_case，且拒绝 `market` / `offset`。本票按只用现行接口的约束没有发送旧路由请求；失败依据是实际安装代码与官方接口契约，不是捏造的线上 HTTP 错误。

离线补充验证采用这份**结算 metadata 的事实投影**，再人工构造单市场 event wrapper、4 个成交和两个受控费率情景。`from_event_slug`、`load_trades` 都调用原生代码；注入的纯内存 transport 拦截所有请求，未知路由直接失败。`/trades` 在这里仅是旧 schema 的 fixture dispatch key。没有把 synthetic ticks 宣称为 Katana 的真实历史，也没有改写引擎或 loader 以假装迁移成功。

| 离线情景 | 输入 | 引擎观测 |
| --- | --- | --- |
| 费用开启（人工 rate=0.07） | 4 ticks，价格 0.30 / 0.30 / 0.50 / 0.50；买卖各 10 shares | 2 个订单、2 次模拟成交；手续费 0.147 + 0.175 = **0.322 pUSD**；初始 1000，最终现金 **1001.678 pUSD** |
| 费用关闭（rate=0） | 同一 synthetic series | 2 次模拟成交、手续费 **0**；最终现金 **1002 pUSD** |

这证明费用模型确实进入 BacktestEngine 现金记账，不证明真实成交可获得、策略盈利、终局结算、L2 成交真实性或 v1/v2 在线覆盖。

## 当前活跃市场抽样

NAS 的公开取数不可用后，使用浏览工具读取公开 [Gamma 默认页 `limit=5`](https://gamma-api.polymarket.com/markets?limit=5)，没有任何交易调用或地区限制绕过。观测日 2026-10-30；返回市场的 `updatedAt` 均为 `2026-10-29T15:54:45.892886Z`。记录为人工事实投影，**不是 NAS 网络录制 fixture**。

| 指标 | 样本内数量 / 比例 |
| --- | --- |
| 去重且 active=true、closed=false | 5 |
| protocol `v1` | 5 / 100% |
| protocol `v2` | 0 / 0% |
| 缺失或未知 version | 0 / 0% |
| 实际在线 loader / 历史数据验证通过 | 0 / 0% |

市场 ID：`559651`、`559652`、`559653`、`559654`、`559655`（完整 slug 见 `active-sample.json`）。这是默认顺序下的便利样本，主要来自较早的政治类市场，四个还属于同一事件；**不能推断全站 v1 为 100%，更不能推断不存在 v2 市场**。v1 数量仅表示协议候选资格，尚未校验对应 CLOB 版本及数据可得性。Protocol V2 按 [官方协议迁移说明](https://docs.polymarket.com/migrate/polymarket-v2/api-integrations) 识别 `version` 和 `positionIds`，不凭 `clobTokenIds` 的存在猜测。

需要更广的独立样本时，可在可直连环境显式执行：

```bash
uv run --frozen python scripts/sample_polymarket.py --pages 2 --output .state/polymarket-sample.json
```

该命令最多取 2 × 100 条当前活跃市场（可选 1–5 页），按 id 倒序，保留未知版本并去重。所有请求 / 重试共用一个每秒 2 次、容量 1 的令牌桶，三次以内指数退避，禁用环境代理和重定向；只读 Gamma 与 geoblock。US/VA 或 blocked 返回不启用任何执行路径，公开读取仍可继续。geoblock 失败明确记录未知，不授予交易资格。它不属于测试或默认离线示例；测试只用内存 transport。

Gamma 请求失败时输出仍保存位置未知、错误和 failed / partial 状态，命令非零退出；空样本的比例为 null，不伪装成有效的 0% 覆盖率。本机运行已在重试后确认 `httpx.ConnectTimeout`，没有取得更广样本。

## 费用公式验证

[官方费用说明](https://docs.polymarket.com/trading/fees) 给出 `fee = C × feeRate × p × (1 − p)`，仅 taker 收费，并保留 5 位小数。1.231.0 的 `calculate_commission` 使用同一乘法公式，随后 `round(float(value), 5)`；模型将返回值转为 pUSD Money。`feeRate` 来自 `feeSchedule.rate` 映射到 `instrument.taker_fee`，不是签名用的旧 base fee bps。缺少 schedule 会回落到零，真实数据管线应将“缺失”与“明确免费”分开。

| C | p | rate | 未舍入结果 | 1.231.0 |
| --- | --- | --- | --- | --- |
| 100 | 0.01 | 0.07 | 0.0693 | 0.0693 |
| 100 | 0.30 | 0.07 | 1.47 | 1.47 |
| 100 | 0.50 | 0.07 | 1.75 | 1.75 |
| 100 | 0.70 | 0.07 | 1.47 | 1.47 |
| 100 | 0.99 | 0.07 | 0.0693 | 0.0693 |
| 0.002 | 0.50 | 0.07 | 0.000035 | **0.00003** |

前五组与公式及对称性一致。最后一组若按 Decimal `ROUND_HALF_UP` 得 0.00004，与实现不同；测试另覆盖 `C=0.007, rate=0.02, p=0.5` 的相同边界。**官方该页没有规定 tie-breaking 模式**，因此这里只报告浮点舍入与精确 half-up 基准的差异，不声称已证明线上计费错误。精确结算一致性尚未验证。

本例显式禁用 maker rebate，maker commission 验证为零。默认启用的负 commission 是返佣估算，不能当作真实逐笔收费或已收到的每日返佣。

## 建议与验收边界

| 选项 | 判断 |
| --- | --- |
| 保持 1.231.0 | **保留回测核心**与冻结 fixture；不直接启用其原生在线历史取数。现有双引擎行为不变 |
| 升级 2.0 rc | 本票不升级。文档有较新的 Data API 与协议校验，但不支持 Protocol V2 新市场；需单独做版本迁移与回归评估 |
| 自定义只读 loader | **建议的后续工作**：优先统一 SDK `polymarket-client`（官方称 Data API v2 从 0.10.0 支持），按版本校验标识、cursor 分页、保留精度 / 时间范围 / 来源，转换为引擎数据。尚未实现，不能当作本 PR 功能 |

交付包含原生机制回放、真实历史路径的明确失败原因、版本差异、带偏差说明的活跃样本和费用实测。没有执行或配置实盘 / 纸面交易客户端、私钥、订单签名、充值或提现；模拟订单只存在于本地 BacktestEngine。完整新增验证命令通过后即可审查，PR 不自动合并。

验证记录：带 extra 的 research 全量测试 **32 passed**（11 条上游弃用警告），其中新增离线测试 7 passed；普通 frozen sync 后新增组 4 passed / 3 skipped。wheel 中 loader、fee_model、parsing 三个文件与上述固定 tag 源码逐字节一致。原有包版本均保持不变；只新增可选 extra 的锁定依赖。独立只读审查通过，覆盖代码、来源、文档声明和默认环境兼容性；未独立重取外网样本。
