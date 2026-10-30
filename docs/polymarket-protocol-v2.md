# Polymarket 协议、数据接口与 SDK 迁移评估（ARB-18）

核对日期：**2026-10-30（Asia/Shanghai）**。仓库比较基点：main `1d0da1d`（含 ARB-7 / PR #13）；参考 [ARB-9 验证](nautilus-polymarket-validation.md)（PR #8）。本文只交付评估与后续票建议，没有迁移业务模块、升级依赖或新增网络/执行脚本。

结论：复用 **ARB-7 已合入的只读数据层**，在同一契约上补足严格协议身份与消费者校验；保留 NautilusTrader 1.231.0 的离线引擎，停用其原生在线历史 loader。Protocol V2 市场必须按显式版本选择 position ID，不能因使用 CLOB V2 或名称带 V2 的 SDK，就宣称支持新仓位协议。现有 CTF 持仓不会因升级集成而自动转换。[协议总览（核对 2026-10-30）](https://docs.polymarket.com/migrate/polymarket-v2/overview)

## 1. 时间线与名称边界

下表日期区分官方事件、软件发布日期与本次观察日；未核实的投产时间不补成事实。

| 对象 | 时间线与证据 | 接口变化 | 只读/回测影响 |
| --- | --- | --- | --- |
| CLOB V2 | 2026-04-17 预告，**2026-04-28** 正式切换；标准生产 host 保持 `clob.polymarket.com` | CTF Exchange V2 / Neg Risk CTF Exchange V2；pUSD；订单字段与费用机制改变，域版本由 `"1"` 到 `"2"` | 历史切点前后的币种、费用与行情来源分开；不能把旧订单/费率字段直接套到新记录。Gamma `version:"v1"` 的 CTF 市场仍可使用这代 CLOB |
| Protocol V2 | 官方 Python SDK 0.9.0 于 **2026-09-04** 发布了初步 ID/交易支持；截至 **2026-10-30**，官方迁移页要求继续支持 CTF | 仓位账本改为 PositionManager；按市场 `version` 选 ID；V2 结算读 `resolutionStatus` | 双账本并存。软件支持发布日期不代表所有市场切换；不能按市场创建日期猜版本 |
| ExchangeV3 | 属于 Protocol V2；**2026-10-30** 官方合约表已列出 Polygon 部署（不是本文确认的部署日） | 同一 V2 交换合约；签名域 `"3"`；订单结构仍沿用 CTFExchangeV2，`tokenId` 装入 position ID | 公共 `/book?token_id=` 与 WS 订阅必须选正确资产；离线撮合需核对整数取整与费用记账 |
| `CONDITIONAL-V2` | 属于 Protocol V2 仓位余额选择器；官方 SDK **0.12.0** 已补齐相应处理，release 发布于 **2026-10-01** | 与 CTF 的 `CONDITIONAL`、pUSD 的 `COLLATERAL` 区分 | 不是公共数据 API 版本，也不是可据此访问的公开余额接口；本项目不启用相应认证路径 |
| Data API v2 | **2026-09-04** 上线；**2026-10-24** v1 退役，迁移页有一项 accounting snapshot 例外 | `/v2`、envelope、cursor、snake_case；价格历史移到 data host | 旧 loader 改 host/path 仍不够，解析、过滤、分页和完整性都要重做 |
| `polymarket-client` | **2026-09-10** 发布 0.10.0，Data API v2 发生破坏性迁移；Protocol V2 官方迁移基线 **≥0.12.0**；GitHub 最新 release 为 **0.15.0（2026-10-29）** | 统一 Gamma/CLOB/Data；公共与安全客户端分离；模型/时间/游标经过规范化 | main 已锁定 0.15.0 并复用公开解析模型；保持锁定与离线验证，不把文档的 `latest` 变成无锁依赖 |

时间线来源：[Predictions changelog](https://docs.polymarket.com/changelog/predictions)（事件 2026-04-17、04-28、09-04），[Python releases](https://github.com/Polymarket/py-sdk/releases)（发布日 2026-09-04、09-10、10-01、10-21），[0.15.0 release](https://github.com/Polymarket/py-sdk/releases/tag/polymarket-client-v0.15.0)（2026-10-29），[Protocol SDK 迁移](https://docs.polymarket.com/migrate/polymarket-v2/sdk-integrations)、[合约表](https://docs.polymarket.com/resources/contracts)、[Data 迁移](https://docs.polymarket.com/migrate/data-api-v1-to-v2)（以上均核对于 2026-10-30）。0.12.0 的 release 标题内日期为 09-30，GitHub `published_at` 为 10-01，本文按后者写发布日期。最新版本结论以本轮 GitHub API 的 release 记录为准。

本次核读的官方迁移指南没有给 Protocol V2 的统一切换日，也没有确认全站已完成切换。因此不将媒体中的 canary / 11 月预期排期作为硬截止；持续关注官方更新和市场自身 `version`。**CLOB `/version` 的数值 `2`、Gamma 的 `"v2"`、Exchange 域 `"3"`、Data `/v2` 与 SDK semver 分属不同层。**

## 2. Protocol V2、ExchangeV3 与新仓位系统

### 身份与公共读取

官方直接 API 规则与统一 SDK 模型必须分开处理：

| 维度 | CTF 市场 | Protocol V2 市场 |
| --- | --- | --- |
| Gamma `version` | `"v1"` | `"v2"` |
| Gamma outcome ID 数组 | `clobTokenIds`：**JSON 编码的字符串数组** | `positionIds`：**原生十进制字符串数组** |
| Gamma `outcomes` | JSON 文本；按解码后的 outcome 索引配对 | 同左 |
| Python SDK 的 outcome | `token_id` | `position_id` |
| 资产账本 | Conditional Tokens | PositionManager |
| 交易域版本（只作来源标识） | `"2"` | `"3"`，ExchangeV3 |
| Gamma 结算状态 | `umaResolutionStatus` | `resolutionStatus`：inactive / active / resolved |

来源：[Protocol API 迁移](https://docs.polymarket.com/migrate/polymarket-v2/api-integrations)、[Protocol SDK 迁移](https://docs.polymarket.com/migrate/polymarket-v2/sdk-integrations)（均核对 2026-10-30）。即使两种 ID 字段都存在，也只能按显式版本选择。outcome 缺失/重复、ID 非十进制或缺失、版本未知都应隔离并报告；ID 全程保留字符串，不能经 JavaScript Number 中转。

公共 CLOB 盘口仍接收 `token_id=<所选资产>`；public market WS 的 `assets_ids` 也是所选资产 ID，不能因为参数名含 token 就只接受 CTF。用户流的 condition 选择器属于另一种身份，本项目不接入 user 频道。[API 迁移（核对 2026-10-30）](https://docs.polymarket.com/migrate/polymarket-v2/api-integrations)

`CONDITIONAL-V2` 是 **CLOB 认证余额/allowance 查询**的 `asset_type`；不是“CTF 合约改名”。V2 仓位位于另一套 ERC-1155 账本，Router 负责仓位操作，模块承载市场逻辑。它不能替换 Data API 的 positions 查询，更不能被公共采集器用来刷新余额。本票仅记录差异，不提供认证、签名、授权或链上操作代码。[协议总览](https://docs.polymarket.com/migrate/polymarket-v2/overview)、[仓位账本](https://docs.polymarket.com/trading/positions/how-positions-work)（均核对 2026-10-30）。

### 回测必须保留的语义

以下事实来自官方文档；括号内为本项目后续建模建议。

- PositionManager 的 module 1/2/3 分别是 Binary / NegRisk / Combinatorial。position ID 不应被应用层当作普通整数推断类型。（将 module 与支持范围单独记录；初期隔离未知模块和 Combo，不把二元外形等同于可按普通二元市场定价。）[仓位说明（核对 2026-10-30）](https://docs.polymarket.com/trading/positions/how-positions-work)
- V2 condition 的原生 ABI 宽度是 `bytes31`，兼容 `bytes32` 时右侧补零；缩窄前检查末字节。Router 使用 6 位小数 base units，赎回 outcome index 为 0/1，与 CTF index set 1/2 有别。（存储原始 ID 与边界格式，不截断十六进制字符串，也不跨协议共用索引枚举。）[合约迁移（核对 2026-10-30）](https://docs.polymarket.com/migrate/polymarket-v2/contract-integrations)
- ExchangeV3 自算 fill 的对手金额按 maker 签署金额的整数比例向下取整；BUY 的费用加到抵押品支出，SELL 的费用从收入扣除。（现有连续价格×份额模型只能标为近似，增加 base-unit 边界 fixture 后才能声称重现此语义。）[API 迁移（核对 2026-10-30）](https://docs.polymarket.com/migrate/polymarket-v2/api-integrations)
- Binary/NegRisk 最终结果以百万分之一为分母；部分 neg-risk 结果可由事件推导，不能等待每个结果都有独立 `ConditionResolved`；迁移 condition 又有例外。（结算事件与策略可见行情分离，保留结果向量和可得时间，不能一律压成 winner 0/1。）[链上数据说明（核对 2026-10-30）](https://docs.polymarket.com/resources/onchain-position-data)
- CTF→PositionManager 需要预先注册，现有持仓不自动转换。ERC-1155 转移用于余额变化，Router/module 事件用于归因，重复计入会双算。（后续若读取迁移历史，要保存映射、账本和事件身份，不按相同标题/condition 合并余额。）[合约表](https://docs.polymarket.com/resources/contracts)、[链上数据说明](https://docs.polymarket.com/resources/onchain-position-data)（均核对 2026-10-30）。

## 3. CLOB V2（2026-04-28）与历史切片

4 月升级仍服务 CTF：旧签名域 `"1"` 不再兼容；订单去掉 `nonce` / `feeRateBps` / `taker`，新增毫秒 `timestamp`、`metadata`、`builder`，费用在匹配时决定，抵押品改为 pUSD，切换时旧挂单清空。本项目按 **pUSD≈USD** 进行研究记账，不把旧 feeRateBps 当成新费用 schedule，也不把切点前后订单生命周期拼成连续状态。[官方 changelog，2026-04-17 / 04-28（核对 2026-10-30）](https://docs.polymarket.com/changelog/predictions)

对只读接口，应保留市场当时的 tick、最小份额、费率与可交易状态；盘口格式相似不保证费用参数不变。对于跨 4 月的历史，记录 API 代际与抵押品来源，必要时分 Dataset。4 月的 preview host 不能当作当前生产默认地址；当前 public CLOB 使用标准 host。[官方 changelog（核对 2026-10-30）](https://docs.polymarket.com/changelog/predictions)

这些差异不要求本项目写订单构建器；它们界定了回放模型和历史归因能声称的准确度。

## 4. Data API v2：路由、字段与完整性

### 路由变更

| 旧来源/路由（仅作识别，禁止新增调用） | 后续采集使用 | 消费方 |
| --- | --- | --- |
| Data `/trades`、`/activity` | `/v2/trades`、`/v2/activity` | 历史 ticks、成交/持仓特征 |
| `/positions`、`/closed-positions`、`/v1/market-positions` | `/v2/positions`，明确 status 与 condition/user 锚点 | 持仓与 PnL |
| `/holders`、`/oi`、`/live-volume` | 对应 `/v2/…`；holders 可选 `include_pnl=true` | 集中度与流动性 |
| CLOB 的价格历史读取 | Data host `/v2/prices-history`，按 `token_id` 选资产 | ARB-7 价格 Dataset |
| 无等价旧结算接口 | `/v2/resolutions` | 提案/争议/最终结果 |

路由来源：[Data 迁移](https://docs.polymarket.com/migrate/data-api-v1-to-v2)、[价格历史](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history)、[结算状态](https://docs.polymarket.com/api-reference/markets/get-resolution-state)（均核对 2026-10-30）。官方保留 `GET /v1/accounting/snapshot`，不在 10-24 退役范围内；**本项目仍只使用 Data API v2**，这项例外不构成调用旧路由的授权。

### 不能只替换 URL

| 项目 | 旧契约 → v2 | 对本项目的要求 |
| --- | --- | --- |
| 响应 | 裸对象/数组 → `data`；分页另有 `pagination` | `data:null` / `[]` 按端点区分正常空态与缺失，不当网络错误；HTTP 错误独立处理 |
| 字段 | `proxyWallet` / `conditionId` → `proxy_wallet` / `condition_id` | HTTP 原始契约与 SDK 模型各自归一化，避免双重改名 |
| 筛选 | `market` → `condition`（支持 condition_id / conditionId 别名，列表最多 20 个） | v2 对 `market` / `market_id` / `marketId` / `offset` 返回 400，`offset=0` 也不允许 |
| 仓位 | 多条路由 → status 过滤统一路由 | OPEN / REDEEMABLE / REDEEMABLE_LOST / MERGEABLE / CLOSED 是查询过滤值，不能把未请求的状态当成无仓位 |
| 数量 | 数值为 JSON number；裸 size/volume 为 shares，`_usdc` 为 USD | 原始解析保留十进制精度；单位显式。`_usdc` 后缀不表示本项目要改回 USDC.e 抵押品 |
| 缺失值 | null/缺失不等于 0；outcome_index=999 表示无法标注 | 缺失费用、结果、时间和方向都保留未知，不用零填充掩盖不可计算 |

来源：[Data 迁移](https://docs.polymarket.com/migrate/data-api-v1-to-v2)、[Data v2 通用约定](https://docs.polymarket.com/api-reference/data-api/overview)（均核对 2026-10-30）。

分页按 `pagination.next_cursor` 到 null 才结束；短页/空页不能证明结束，响应里的 offset 只是展示信息。游标是 opaque：原样保存、检测重复/循环，不自行解码或累计 offset；每页复用相同锚点/筛选。Data 的 cursor 与 Gamma keyset 的 `after_cursor` / `next_cursor`、SDK 的 cursor 也不能互换。断点应绑定 endpoint、参数、schema 与 SDK 版本；400 或失效后从首屏重新同步并去重，禁止声称游标失效意味着历史为空。[Data v2 分页（核对 2026-10-30）](https://docs.polymarket.com/api-reference/data-api/overview)

### 历史窗口与回测边界

`/v2/trades` 的 condition/event 形态固定提供三年窗口，忽略 start/end；user 形态才按其规则使用时间边界。`taker_only` 默认 true；false 会包含 maker 行，不能把两边重复计成两次市场成交。时间以 epoch 秒提供，转换为 Nautilus 纳秒不会凭空恢复子秒顺序；应保留可复现的同秒排序与不确定性。[List trades（核对 2026-10-30）](https://docs.polymarket.com/api-reference/feeds/list-trades)

价格历史 `/v2/prices-history` 有自己的 interval / start+end / as_of 与 bucket_seconds 规则；不能套用 trades 的时间窗口语义。价格桶不包含历史 L2 或队列，完整价格曲线不代表真实可成交，也不能替代 ARB-8 录制。[价格历史（核对 2026-10-30）](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history)

建议 Dataset 明确 `window_requested` / `window_observed`、分页是否耗尽、桶宽、数据新鲜度、采集时间、协议与资产来源；“请求成功”“游标走完”“覆盖全生命周期”是不同证据。终局价格和结算答案只能用于结果评价，不能作为更早时刻的策略输入。

## 5. 统一 Python SDK 与依赖边界

官方 `polymarket-client` 的公共入口为 PublicClient / AsyncPublicClient，无需凭据；安全客户端是另一入口。Python 模型将精度敏感值映射为 Decimal、时间映射为 datetime，list 方法提供统一 paginator。[Python 官方入口（核对 2026-10-30）](https://docs.polymarket.com/getting-started/python)

main 的 ARB-7 已锁定 0.15.0，复用公开 Gamma/Resolution 解析模型；因公共 client 未公开 transport 注入，HTTP 由受控 httpx 边界负责代理、预算、退避与录制。后续沿用此选择，不为统一名称强行切换 transport。本票同步了 main 的锁定环境并运行离线回归，没有构造 SDK 安全客户端或进行在线 SDK 验证。现有价格/payout 输出使用 float，若后续用于精确记账，应在消费契约上明确精度或增加 Decimal/base-unit 字段，不能声称 SDK 的 Decimal 已自动贯穿到 Parquet/API。[本地数据层说明](polymarket-data.md)

| 版本证据 | 应用决策 |
| --- | --- |
| 0.9.0 首次提供 Poly V2 标识支持 | 不能仅据此认定完整兼容 |
| 0.10.0 Data API v2 破坏性更新 | 旧参数/持仓字段与历史 cursor 需要重新核验 |
| 0.12.0 修正按市场版本选择仓位操作与余额选择器 | 遵守官方 Protocol 迁移最低版本 ≥0.12.0，不复用早期不完整支持 |
| 0.14.0 拒绝重复 continuation cursor，并保留更多 V2 metadata；增加 token reference lookup | 用这些修复作为回归案例；main 已使用更高版本，无需降级 |
| 0.15.0 新增 taker delay 毫秒字段（2026-10-29） | 复用 main 的精确锁定；未来回测读取市场有效期内的 delay，不能固定套用旧 50ms 示例 |

来源：[官方 releases](https://github.com/Polymarket/py-sdk/releases)、[0.15.0](https://github.com/Polymarket/py-sdk/releases/tag/polymarket-client-v0.15.0)、[SDK 迁移](https://docs.polymarket.com/migrate/polymarket-v2/sdk-integrations)（核对 2026-10-30）。合入状态与发布日期见下节追踪表，不把 SDK 支持等同于 Nautilus 支持。

`Polymarket/py-clob-client` 已归档（2026-10-30 GitHub 仓库状态核验）；不新增它。`py-clob-client-v2` 是不同仓库/包，官方引导迁到统一 SDK；ARB-9 可选 extra 精确锁定的 1.0.2 只为导入旧适配器和离线验证保留，不能把它当作 ARB-7 的统一 SDK，也不能不经验证直接替换旧适配器的 import。[旧仓库](https://github.com/Polymarket/py-clob-client)、[V2 仓库](https://github.com/Polymarket/py-clob-client-v2)、[统一 SDK 迁移](https://docs.polymarket.com/migrate/clob-sdk-to-unified-sdk)（均核对 2026-10-30）。

## 6. 逐模块影响评估

以下是 **需要实施的点，不是本票已实现的功能**。main 已存在代码按 `1d0da1d` 核读；ARB-7 已合入，ARB-8 以当前 issue 范围与交付评论为依据，不将未合代码描述为 main 能力。现有文档见 [合规](compliance.md)、[数据层](polymarket-data.md)、[扫描器](negrisk-scanner.md)、[做市](mm-backtest.md)、[看板](polymarket-dashboard.md)。

| 模块 | 当前事实/缺口 | 后续改动与消费边界 |
| --- | --- | --- |
| ARB-6 compliance | `research/src/arb_research/compliance.py` 固定 `trading_allowed=False`；协议升级不改变授权 | 复用现有 guard，不增 stub。公共 market 读取与交易判定分开；blocked 或 geoblock 失败都不能放行交易；SDK/协议可用不等于允许交易。只读代理仅显式注入对应公共客户端，异常/审计不可泄漏代理值 |
| ARB-7 数据层（已合入 PR #13） | 已有 Data v2 价格/结算、Parquet/DuckDB、断点、重复 cursor 检测及两个真实事件录制；`polymarket/client.py` 保留 token_id 与 position_id 两组并分别标协议，缺版本记 unknown；当前消费者还缺严格选择规则 | 复用已有归一化与存储；增加 ledger/asset kind、所选协议资产视图、未知版本隔离。检查本地 float 精度、可得时间、结果向量与 cursor 失效后显式重建入口（当前报错留断点）；保留历史两组 ID，但消费时不遍历所有 ID 猜协议；不另建 loader/存储 |
| ARB-8 录制器（进行中，Draft PR #11） | 公共 market WS、L2 重建和 gaps；PR 仍待真实录制验收 | 通过 ARB-7 选 `asset_id`；录制 manifest 保存 protocol、ledger、condition、outcome、tick 与来源；保留源时间/接收时间/本地序号/重连 epoch；重连后以新快照恢复，跨缺口不外推。未知字段保留原文；不引入 user/PolyBolt 认证频道 |
| ARB-10 扫描器 | `negrisk.assets()` 已对 v2 选 positionIds，但所有其他 version 都回退 clobTokenIds；`str(x)` 也没有十进制 ID 校验。报告未保留完整协议身份 | 用共享严格解析替代回退；version 缺失/未知与非法 ID 输出错误。按 protocol/module 验证 negRisk 完整组、转换比例与 Other/Combo 支持边界；沿用费用/深度/非原子快照风险，未知模块不计算组合机会 |
| ARB-12 做市回测 | 离线单 outcome 合成 fixture；身份只有本地 market.id/category；未连接真实数据。文档/默认 latency 是示例参数 | 后续输入来自冻结的 ARB-7/8 Dataset，附身份与参数有效期；历史成交 tape 不重复叠加 taker latency。加入整数取整、费用与结算向量 fixture 后再评价 V2 精度；返佣/奖励仍为估算，不能因迁移自动变成现金 |
| ARB-13 前端 | `apps/web/src/features/polymarket/model.ts` 的 Dashboard 固定 `protocol_version:'v2'`，字段仍叫 token_id，来源固定 synthetic-fixture | 接数据前扩为 v1/v2/unknown + asset kind/来源；显示未知、空态、partial/gap/as_of；协议与 API/schema 版本分开。平滑图或成功渲染不代表真实/全市场覆盖；迁移字段需升 schema 并提供 adapter，不能只改字面量 |
| Nautilus 1.231.0 adapter/loader | ARB-9 已测：原生 `/trades` + market/offset/camelCase；版本缺失/v1/v2 fixture 都被当作 CTF 接受；费用缺失会回零 | 保留离线引擎；不再调用原生在线历史 loader。后续从 ARB-7 的合格冻结 ticks 转 TradeTick，单独验证 instrument 身份、价格/份额精度、时间、手续费和前视隔离。未经专门离线资格验证前，Protocol V2 输入拒绝；不能以不抛异常证明支持 |

模块间交接字段（ARB-27 已发布身份子集为 `asset_selection` / `selected-assets.v1`，见 [共享视图](polymarket-data.md#共享所选资产视图arb-27)；费用/tick 有效期仍由对应数据契约提供）：`protocol_version`（v1/v2/unknown）、`asset_kind`（ctf_token/position）、`asset_id`（decimal string）、`ledger`、Gamma market/event ID、原始与规范化 condition ID、outcome、module、source/API/schema/SDK version、`observed_at` / `received_at`、费用与 tick 的有效时间、完整性状态。保留原始 ID 字段便于追溯，存储主键至少区分 chain、ledger 与资产，不能只凭标题关联。

公开 market WS 文档给出毫秒字符串时间和 book hash，没有承诺适用于所有消息的全局连续序号；本地 sequence 不能伪装成交易所 sequence，hash 存在也不等于已证明无缺口。[Market channel（核对 2026-10-30）](https://docs.polymarket.com/api-reference/wss/market)

网络预算需由所有请求/重试共享；本项目约束保留 ≤9,000 req/10s 的总上限，但它不是每个接口都可用的配额：官方当前表列 CLOB 通用 9,000、Data v2 总 800、Data v2 prices-history 200、Gamma markets 300（均 req/10s）。按最严格适用桶限速，429 遵守 Retry-After，瞬态错误有限退避，400 修参数，403 不盲重试。SDK 内部重试也必须进入同一预算，不为每个 worker/分页另开无限桶。[Rate limits（核对 2026-10-30）](https://docs.polymarket.com/api-reference/rate-limits)

ARB-7/8 状态依据：2026-10-30 读取当前 issue 与评论；[ARB-7 PR #13](https://github.com/KLGR123/arb/pull/13) 已合入 `1d0da1d`，验收记录为 2 个真实已结算事件、4,245 条价格点与 8 个 resolved 市场；[ARB-8 Draft PR #11](https://github.com/KLGR123/arb/pull/11) 在读取时 head=`0808eda`。公开录制不因 geoblock blocked 而拒绝，是 ARB-8 当前验收意见；它只适用于公共读取，交易 guard 保持拒绝。本票没有修改这些票、另发任务或更改其状态。

## 7. 后续票建议（本票不创建）

优先级表示处理顺序，不赋予任何签名、交易或链上写入权限。ARB-7 已完成，不重建其采集工作；兼容性增量另行承接，ARB-8 优先复用现有票补验收，不另建同范围票。

| 优先级 | 建议标题 / 承接处 | 验收标准 |
| --- | --- | --- |
| P0 | **统一 v1/v2 所选资产视图与未知版本隔离**（复用 ARB-7；ARB-10/13 消费） | fixture 覆盖 v1/v2/缺失/未知版本、两类 ID 同时存在、outcome 乱序/缺失/重复、非法/超 53-bit ID；原始未知记录可保留，但只有显式合法版本进入可消费视图；消费者选择同一资产且无数值精度损失 |
| P0 | **扫描器严格版本与模块筛选**（ARB-10 后续） | 缺失/未知 version 不再落入 CTF；非法 ID 不请求盘口；V1/V2 二元组 fixture 结果可核对；Combo/未知 module/不完整 negRisk 均报告跳过；保留只读 guard 与错误覆盖 |
| P1 | **强化 Data v2 断点恢复与精度契约**（ARB-7 后续） | 复用已录制的两个真实事件；保留已有 envelope/重复 cursor/幂等测试，补空/短页有 cursor、400 失效后的显式重建入口、同秒边界、精度/单位与参数版本绑定；不重做已有采集或把 12 小时桶改称逐笔历史；测试禁网；旧 Data 路由零调用 |
| P1 | **录制 manifest 增加协议身份并完成真实回放**（补 ARB-8） | 至少 v1 和 v2 各一份公开 market 录制（若无可用 v2 则记录缺口，不伪造）；重连后 snapshot 恢复，跨 gap 不输出有效 L2；源/接收时间、本地序号和 hash 语义明确；fixture 离线重建与 manifest 身份一致 |
| P1 | **冻结数据到 Nautilus 1.231.0 的只读转换层**（承接 ARB-9 建议，复用 ARB-7） | 不调用旧在线 loader；对合格冻结 CTF ticks 核对 instrument/tick/数量/排序/费用与引擎现金。V2 先默认拒绝，单独 fixture 证明身份、费用和结算输入正确后才放行离线转换；没有 exec client、密钥或网络调用；防终局 metadata 前视 |
| P1 | **做市回测的 V2 fill 与结算精度评估**（ARB-12 后续） | 整数 base-unit 比例向下取整、BUY/SELL 费用、6 位份额和有效 tick 边界 fixture；保留现有近似模型标签；覆盖非 0/1 payout、未决/派生 negRisk 和迁移例外；PnL 对账，rebate 不入可用现金 |
| P1 | **前端协议与数据质量契约升级**（ARB-13 后续） | 移除固定 v2；v1/v2/unknown、ctf_token/position 大整数原样显示；空/未知/partial/gap/as_of 可辨；合成来源不能被改标真实；旧 schema fixture 经 adapter 回归 |
| P2 | **评估 Nautilus 新版本只读能力** | 选择固定 wheel/tag；对照 1.231.0 的同一冻结 Dataset 与费用基准，验证导入、API、游标、版本拒绝、时窗和数值精度；区分已发布/仅 develop/RFC；通过后再另提升级 PR，本票不升级 |
| P2 | **持续协议契约核对** | 官方文档/SDK/上游 adapter 有变化时更新来源日期与变更表；已合 PR 要核对进入哪个 release；只增加公共 fixture 与离线回归，不采用上游建议的实盘 smoke |

## 8. 上游 issue / PR 追踪

**状态快照日期统一为 2026-10-30**，来自 GitHub API；“merged”只表示进入上游仓库，不代表进入本项目锁定的 wheel。

| 上游对象 | 已核验状态与日期 | 持续关注内容 / 关闭本地追踪的条件 |
| --- | --- | --- |
| [Nautilus #5455：Protocol V2 RFC](https://github.com/nautechsystems/nautilus_trader/issues/5455) | **open**；创建 2026-10-25 | 首阶段 metadata/market data/resolution 的具体 PR、release 与拒绝边界；还只是提案，不能算支持证据。本项目只评估其只读部分 |
| [Nautilus #5433：100-ID 后续 keyset 页 HTTP 414](https://github.com/nautechsystems/nautilus_trader/issues/5433) | **open**；创建 2026-10-23，更新 10-24 | 批量发现/closure refresh 的 URI 长度与续页策略；后续选定版本通过大批量离线契约验证才消除风险 |
| [Nautilus #3844：CTF Exchange V2 迁移](https://github.com/nautechsystems/nautilus_trader/issues/3844) | **closed**，2026-04-30 | 作为 4 月迁移历史依据；不把其完成状态误认成 #5455 完成 |
| [Python SDK PR #322：按市场版本路由](https://github.com/Polymarket/py-sdk/pull/322) | **merged 2026-09-30**；0.12.0 release 收录 | 在固定版本公共模型的 fixture 中核对 V2 metadata/ID；不调用该 PR 涉及的写入方法 |
| [Python SDK PR #358：重复游标保护](https://github.com/Polymarket/py-sdk/pull/358) | **merged 2026-10-21**；0.14.0 release 收录 | SDK 可能在已产出部分页后抛错，消费者不能将 partial Dataset 发布成 complete；重启幂等验证 |
| [Python SDK PR #352：V2 oracle metadata/filters](https://github.com/Polymarket/py-sdk/pull/352) | **merged 2026-10-19**；0.14.0 release 收录 | metadata 与 protocol 筛选跨分页保留；unknown/缺失不被静默归成 v1 |
| [Python SDK PR #353：token reference lookup](https://github.com/Polymarket/py-sdk/pull/353) | **merged 2026-10-19**；0.14.0 release 收录 | 可用于公共身份对照，核对 condition/module/outcome；仍按 Gamma version 决定路径 |

SDK release 收录依据：[0.12.0](https://github.com/Polymarket/py-sdk/releases/tag/polymarket-client-v0.12.0)、[0.14.0](https://github.com/Polymarket/py-sdk/releases/tag/polymarket-client-v0.14.0)（核对 2026-10-30）。

同时关注 [官方 Protocol 迁移总览](https://docs.polymarket.com/migrate/polymarket-v2/overview)、[Predictions changelog](https://docs.polymarket.com/changelog/predictions)、[SDK releases](https://github.com/Polymarket/py-sdk/releases)、[Nautilus releases](https://github.com/nautechsystems/nautilus_trader/releases)（核对 2026-10-30）：明确发布日期、市场覆盖或迁移规则后再更新本表，不根据 RFC 或 develop 文档自行解锁。

## 9. 证据范围与本票交付

官方页面没有单独发布日期时，本文写“核对日期”；带事件日期的 changelog、release、issue/PR 另标日期。协议 API/SDK/合约迁移页正文已在本轮通过显式配置的公共只读 HTTP 客户端核读；其余官方文档与 GitHub 元数据交叉核对。未读取任何账户私有数据，没有真实市场覆盖率抽样、链上调用、签名或交易测试。

ARB-7 已完成并合入、ARB-8 继续独立推进，本评估不复制数据层或录制器；后续增量字段是建议，需与已有 schema 对齐。ARB-9 的旧 loader 和前视限制继续有效。本文没有把上游当前文档视为 1.231.0 能力，也没有承诺升级 2.0 rc 即可支持 Protocol V2。

本票交付为本文的变更说明、七个模块的影响表、按优先级的验收清单及上游追踪表，并加入 docs 入口；可选识别脚本不实现，以免复制 ARB-7 的解析 ownership。后续代码改动须在对应票执行，所有回归使用本地 fixture，CI 不发 Polymarket 外网请求。
