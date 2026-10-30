# Nautilus Data API v2 只读加载器（ARB-14）

锁定 `nautilus_trader==1.231.0`，统一 SDK `polymarket-client==0.15.0`。加载器使用 Nautilus 核心 `BinaryOption` / `TradeTick` / `QuoteTick`，绕过原适配器中已退役的 Data API v1 历史路由。没有升级引擎，没有实例化执行客户端、签名器或钱包。

ARB-29 收紧：生产转换现在仅接受明确 Protocol `v1`，Protocol `v2` 不再因 synthetic `positionIds` 映射而放行。需要历史已知条款与严格前视隔离时，使用[冻结成交转换层](nautilus-frozen-ticks.md)；本页的捕获快照回放仍只是数据与费用记账 smoke。

## 用法

仓库根目录进入 `research/`：

```bash
uv sync --locked

# 复用 ARB-7 的同步器，把价格/结算存入 Parquet + SQLite manifest。
uv run --locked polymarket-data --data-dir .state/polymarket sync --event 1278073 --event 1278072

# 显式联网：事件 ID 或 slug；可只选事件内的一个市场。
uv run --locked python -m arb_research.nautilus_data_v2_cli collect \
  1278073 --market-id 6388418 --data-root .state/polymarket --output .state/eth-settled.json

# 默认复用 request cache。--refresh 强制重新读取；--offline 禁止网络。
uv run --locked python -m arb_research.nautilus_data_v2_cli collect \
  1278073 --market-id 6388418 --offline --output .state/eth-cached.json

# 完全离线回放已录制的真实数据；此处 extra 只用于导入原生费用模型。
uv run --locked --extra polymarket-validation python -m arb_research.nautilus_data_v2_cli replay \
  tests/fixtures/nautilus-data-v2/eth-settled.json --output .state/eth-result.json
uv run --locked --extra polymarket-validation python -m arb_research.nautilus_data_v2_cli replay \
  tests/fixtures/nautilus-data-v2/btc-settled.json --output .state/btc-result.json

uv run --locked pytest
uv run --locked --extra polymarket-validation pytest
```

Python 入口：

```python
import json
from arb_research.nautilus_data_v2 import load_snapshot

with open("tests/fixtures/nautilus-data-v2/btc-settled.json") as source:
    tokens = load_snapshot(json.load(source), start=1793288773, end=1793288773)
for token in tokens:
    instrument, trades, quotes = token.instrument, token.trades, token.quotes
    print(instrument.id, len(trades), token.metadata["quote_kind"])
```

`load_snapshot` 自身没有 HTTP 操作。可用 `token_id=` 选 token，`start/end` 是 epoch 秒或带时区的 ISO 字符串，**本地过滤两端包含**。无此 token、无效协议、缺费率、无法表示的精度或不完整分页均报错。`collect --max-pages N` 是每条流的请求上限，达到上限而仍有 cursor 时整次 collect 失败，不交付“完整历史”的假象。

## 字段映射

| 来源 | Nautilus / 处理方式 |
| --- | --- |
| Gamma event ID → market ID → `conditionId` | 从事件元数据选市场，再读取市场详情；instrument ID 为 `{condition}-{token}.POLYMARKET` |
| `version=v1` / `clobTokenIds` | 按 SDK 的 outcome 顺序映射两个 CTF token |
| `version=v2` / `positionIds` | 拒绝；缺少专用的身份、费用、结算资格 fixture，不提供放行开关 |
| `outcomes` / `question` / `endDate` | `outcome` / `description` / `expiration_ns`；activation 未知记 0 |
| `feeSchedule.rate`、exponent=1 | `taker_fee`，maker fee=0；缺 schedule 只有明确 `feesEnabled=false` 才按零处理 |
| Data v2 `token_id`, `condition_id` | 校验市场关联并选择对应 token；未知 token 拒绝 |
| `price` / `size` / `side` | `TradeTick` price / shares / BUY→BUYER、SELL→SELLER；只请求 `taker_only=true` |
| `timestamp` | 秒转纳秒；离线回放 `ts_init=ts_event`，不假装测得网络延迟 |
| `transaction_hash` + 经济字段 + wallet + occurrence | 哈希生成 `TradeId`，同 transaction 的多次甚至相同 fill 保留，不以 transaction hash 去重 |
| price-history `price` | `QuoteTick` bid=ask=price，bid/ask size=0；**中点近似，没有历史价差或深度** |
| price-history `resolution_seconds` | 原始值保存在 snapshot，实际宽度集合及覆盖说明在 `LoadedToken.metadata` 中；不以请求桶宽代替返回事实 |
| caller 提供的录制 CLOB book | `book_quote` 取最高 bid / 最低 ask，汇总该价位 size；毫秒转纳秒；单边 / crossed book 拒绝 |

真实 Data API 成交含 `0.829999997` 等成交均价，不总能整除捕获时的交易 tick。因此 instrument 的 price precision 设为 16、size precision 为 6；超出表示范围直接拒绝。`orderPriceMinTickSize` 只记录在 `info.market_tick_size_at_capture`，不是历史撮合规则。这个 instrument 描述观测精度，不能当作在线订单资格校验。币种为 pUSD，账面按 pUSD≈USD，不模拟跨协议抵押品兑换。

策略可见 `instrument.info` 只保留 ID、protocol、negRisk、精度说明及费率来源。`outcomePrices`、winner、closed、current quotes、结算 payload 都不复制；完整 Gamma 与 resolutions 只在离线 snapshot 中作为验收/研究资料。元数据和费率是捕获时快照，并非 point-in-time 历史版本；策略不得据捕获后状态构造历史信号。

## 接口与协议差异

Data API v1 于 2026-10-24 退役。本实现仅 GET `/v2/trades`、`/v2/prices-history`、`/v2/resolutions` 和 Gamma event/market 详情；没有 `/trades` 兼容回退。价格/结算由 ARB-7 统一同步并读取本地缓存，collect 只额外请求 Gamma 元数据与成交。v2 使用 `data/pagination` envelope、snake_case、`condition` 和 opaque cursor，拒绝旧 `market` / `offset`。参考[官方迁移说明](https://docs.polymarket.com/migrate/data-api-v1-to-v2)。

**CLOB API V2 与 market Protocol V2 是不同维度。** 本次两个真实已结算样本均由 Gamma 明确标为 Protocol `v1`，但所有历史请求均走现行 Data API v2。旧的 Protocol `v2` position 受控映射测试不构成资格证明；ARB-29 已改为拒绝测试。没有真实 Protocol V2 历史回测证据，也不声称 Nautilus 原生适配器已支持其在线交易。

市场级 `/v2/trades` 只提供固定三年窗口，忽略请求的 `start/end`；所以先遍历完整 cursor，再本地过滤。默认最小成交量 0.01 share 以下不保证覆盖。接口不暴露 fill sequence / log index，无法严格恢复同一秒内链上先后；回放反转 newest-first 返回流并稳定按秒排序，保留 multiplicity，不臆造亚秒时间。相同录制输入的 TradeId 稳定，不保证不同抓取范围间的 occurrence 编号相同。见[官方成交契约](https://docs.polymarket.com/api-reference/trades/list-trades)。

ARB-7 首次价格同步采用 `interval=max, bucket_seconds=43200`，本 loader 完整读取其 Parquet/DuckDB 查询结果，保留服务端实际宽度。已 resolved 的序列最后一个 `resolution_seconds=0` 点是结算尾点，保守移除（包括 fractional payout）；之前的 0 宽度点是 exact observation，保留。若序列本身截断或末点语义不清，调用者应继续核对原始数据；quotes 只用于分析。参考[官方价格历史](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history)。不抓当前 book 冒充历史；需要真实历史 L1/L2 时使用 ARB-8 recorder 的带时间戳快照。

## 真实已结算事件验证

2026-10-30（Asia/Shanghai）通过用户批准的 NAS 公共只读路径录制，数据位于 `research/tests/fixtures/nautilus-data-v2/`。`*-settled.json` 保存 event、选定 market、完整成交分页汇总、从 ARB-7 导出的两个 token 价格及 Data v2 resolved 记录；`*-result.json` 为真实 Nautilus 运行结果。分页 envelope 测试用录制行进行受控分页，不冒充线上多页录制。synthetic book 与 Protocol V2 字段改写只验证契约。

| 事件 / 选定市场 | 真实 tick 数（Yes / No） | 回放 token / fills | taker commission | 1000 pUSD 初始资金后的现金 |
| --- | --- | --- | --- | --- |
| Ethereum above on Oct 29, 2026 12PM ET：event `1278073` / market `6388418`（above $2370） | 1 / 5 | No / 2 | 0.023940 pUSD | 999.926060 pUSD |
| Bitcoin above on Oct 29, 2026 12PM ET：event `1278072` / market `6388399`（above $82000） | 3 / 13 | No / 2 | 0.018310 pUSD | 999.951690 pUSD |

复用 ARB-9 的本地两次 taker fill 方式：第 1 个 tick 买 1 share，第 3 个 tick 卖 1 share。使用原生 `PolymarketFeeModel(maker_rebates_enabled=False)`，采用录制 Gamma `rate=0.07, exponent=1`。两个 token 的价格历史都只有一个 exact observation 加一个结算尾点；去尾点后每个 token 产出 **1 个**分析用 QuoteTick。**这些报价未送入撮合引擎。** 回测只消费真实成交；没有虚构多天高频报价。

这是历史数据导入和费用记账 smoke，不是策略收益评估。费用模型把捕获时费率应用于历史成交；未验证逐笔历史费率、价差、订单簿排队、延迟、终局兑付或 maker 返佣。真实 v1 事件回测成功不等于在线执行适配器验证成功。

## extra、网络和合规边界

纯加载器不需要 `py-clob-client-v2`，直接构造核心数据类型；子进程测试禁止导入原生适配器仍可加载真实 fixture。只有原生费用模型的包级导入需要现有 `polymarket-validation` extra，已由 ARB-9 锁定为 `py-clob-client-v2==1.0.2`；没有使用归档的 `py-clob-client`。统一 SDK 的依赖树包含以太坊工具，业务代码只使用 response models，不调用其交易/签名方法。

HTTP client 只在 `collect` 创建。显式传 `ARB_POLYMARKET_PROXY`，禁用环境代理发现和重定向；不写全局 `HTTP(S)_PROXY` / `ALL_PROXY`，不把代理值写入 cache、输出或异常。未设置该变量时直连，失败提示检查连接和变量配置。复用 ARB-7 `PublicDataClient`，仅在其公共 GET allowlist 增加 `/v2/trades`。请求和所有重试共用 `compliance.PUBLIC_API_BUDGET` 及 ARB-7 跨进程 SQLite `SharedLimiter`（含更严格的公共接口桶）；429 / 指定 5xx / 连接错误最多四次，指数退避加抖动，`Retry-After` 超 60 秒直接失败。没有复制同步/限流逻辑。

此模块没有交易类入口，因此不新增 guard stub。HTTP transport 继承 ARB-7 的 guard；本次统一同步实测 geoblock 为 US/CA、blocked=true，trading_allowed=false。现有 `arb_research.compliance` 拒绝逻辑保持生效，`blocked:true` 不能授权执行；本地 `BacktestEngine` 模拟订单没有网络出口。没有 geoblock 资格提升、L1/L2 认证、私钥、充值或提现路径。

## ARB-7 缓存对齐

基于已合入 ARB-7 的 `main`（ARB-7 合入点 `1d0da1d`）。生产价格与结算直接复用 `DataStore`：先用 `polymarket-data sync` 同步，再 `collect --data-root <目录>`。loader 不另写 price Parquet、manifest 或 checkpoint；通过 `history` 翻完 `next_offset`，并要求每个 token 的 checkpoint 已有水位且 pending 为空。市场/condition/token 必须与缓存元数据匹配。不完整同步明确失败，不能作为完整价格历史使用。读取多页价格及结算时持有 ARB-7 的公共 sync lock，避免并行刷新改变 offset 页边界；遇到正在进行的同步则拒绝，待其完成后重试。闭市市场缺结算资料，或 Gamma 已 resolved 而缓存仍未 resolved，均拒绝输出，防止把 payout 尾点当作报价。

`load_snapshot(..., price_store=DataStore(root))` 与 `replay --data-root` 可在离线回放时直接读取 ARB-7 缓存；默认则读取 portable snapshot 内的导出价格。先读完整序列再去结算尾点，之后才按调用者的时间窗口过滤，避免把窗口尾部真实 observation 当作结算点。

ARB-7 归一化元数据尚无 fee schedule、tick size、endDate，因此本 loader 通过同一只读 transport 补充 Gamma 原始市场详情，构造 `BinaryOption`；不从欠缺字段推断免费。trades 是本票新增流，暂以 request-keyed JSON cache 保存，默认 `.state/nautilus-data-v2/requests/`，原子替换；没有另建长期同步数据库。后续由统一数据层定义 trades 的 Parquet schema 后，可替换这个补充缓存，无需改变 Nautilus 转换。

cache 命中是明确的快照复用，不保证最新状态。`--refresh` 只刷新补充 Gamma/trades，价格/结算需显式重跑 ARB-7 sync；`--offline` 对补充响应禁止网络。所有成交分页完成才返回 snapshot；失败页之后的历史不会被视作完整。导出文件用于便携离线回放，不能替代 ARB-7 的增量同步水位。
