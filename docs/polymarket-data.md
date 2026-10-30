# Polymarket 只读数据层

ARB-7 提供 Gamma 事件、市场、YES/NO token 元数据，以及 Data API v2 价格历史和结算状态的本地快照。查询 API 和 CLI `query` 只读本地数据；只有显式执行 `sync` 才联网。不提供下单、签名、钱包、认证或资金操作。

## 安装与运行

仓库根目录执行，使用 Python 3.12 和 `research/uv.lock`：

```bash
uv sync --project research --frozen
uv run --project research polymarket-data --data-dir .state/polymarket \
  sync --event EVENT_ID_OR_SLUG
ARB_POLYMARKET_DATA_DIR=.state/polymarket uv run --project research \
  uvicorn arb_research.polymarket.api:create_app --factory --host 127.0.0.1 --port 8001
```

所有命令应从同一目录执行，或使用绝对数据目录。服务面向本机研究环境，与回测 API 独立启动。`/docs` 是交互文档，`/openapi.json` 含具体 Pydantic 响应类型。

```bash
uv run --project research polymarket-data --data-dir .state/polymarket \
  query --event EVENT_ID_OR_SLUG --kind metadata
uv run --project research polymarket-data --data-dir .state/polymarket \
  query --event EVENT_ID_OR_SLUG --kind prices --limit 1000
uv run --project research polymarket-data --data-dir .state/polymarket \
  query --market MARKET_ID --kind resolutions
```

`sync --event` 可重复指定。`query` 默认返回价格，支持 `--token-id`、`--start`、`--end`（Unix 秒，正数，查询上下界均包含）、`--limit 1..10000`、`--offset`。跟随 `next_offset` 读取全部本地数据，`null` 表示结束。同步过程中，多次 offset 查询不提供一致的快照隔离。

## 查询 API

| GET 路径 | 返回 |
| --- | --- |
| `/events/{id_or_slug}` | 事件、tags 和 market/token 关系 |
| `/markets/{market_id}` | 市场规则文本、来源、tags、negRisk 组、协议、tokens |
| `/events/{id_or_slug}/prices` | 事件所有市场的分页价格 |
| `/markets/{market_id}/prices` | 单市场分页价格 |
| `/events/{id_or_slug}/resolutions` | 结算状态及 `missing_market_ids` |
| `/markets/{market_id}/resolutions` | 单市场结算状态及缺失标识 |

prices 路径支持 `token_id/start/end/limit/offset`。未知事件/市场返回 404；非法时间范围/分页参数返回 422。前端可从 OpenAPI 生成类型：

```bash
npx openapi-typescript http://127.0.0.1:8001/openapi.json -o .state/polymarket-api.d.ts
```

结算 `status` 保留上游生命周期，如 proposed、challenged、disputed、arbitration、resolved。Gamma `closed` 不能推断已结算。`payouts` 经官方 SDK 从微单位转换为 pUSD/share；UMA 行可能只有原始 `price`（1e18 刻度）而无 payouts，这时保留 payouts=null。`split_50_50` 仅在 resolved 且两个 payout 都为 0.5、或 UMA 最终 price 为 5e17 时为 true。`price/proposed_price/reproposed_price` 保留上游数值字符串，不与 pUSD 单价混用；69 unset 经 SDK 转为 null。`raw` 保留公共结算原始行，包括来源、争议和最终性字段。缺失数值保持 `null`，不转成零；pUSD 近似 USD。

## 共享所选资产视图（ARB-27）

`polymarket/assets.py:select_assets` 是 Gamma 身份的唯一选择入口。扫描器与 ARB-7 同步均调用它；本地 market 响应发布 `asset_selection`，看板展示 schema 3 直接消费该视图。历史 `tokens` 仅作追溯，不能用来重选资产。

`asset_selection` 的 schema 为 `selected-assets.v1`，含 `protocol_version`、`status`、`reason`、`assets` 和 `raw_identity`。只有显式字符串 `version="v1"` 选择 `clobTokenIds`，`version="v2"` 选择 `positionIds`。两类同时存在也只验证/消费所选一类；无版本或未知值整市场隔离，assets 为空。原始 version、outcomes 和两类 ID 保留在 raw_identity；数值型 ID 即使在 Python 中精确也拒绝，不能转成字符串掩盖上游类型错误。

每个所选资产包含十进制字符串 `asset_id`、`asset_kind`（ctf_token/position）、`ledger`（ConditionalTokens/PositionManager，逻辑账本名称，非合约地址）、chain_id（缺失为 null）、event/market、原始和小写 condition、outcome 及其原数组索引、module、source、API/schema/SDK version、observed_at/received_at。Gamma 无显式 API 版本时 api_version=null；module 缺失保持 null，不从 ID 推断。现场读取的 observed_at/received_at 是本机接收时间，不是上游事件时间；离线扫描只知道快照 as_of，received_at 保持 null。费用、tick 及其有效期仍由各消费者的独立数据契约提供，本身份视图不证明交易资格。

二元 outcome 必须恰好两个非空且不重复，ID 数量必须匹配，ID 必须为 ASCII 十进制字符串且不重复；乱序按原始索引配对，不把第一项当成 YES。失败原因如 missing_protocol_version、unknown_protocol_version、invalid_binary_outcomes、invalid_decimal_asset_id_string 会写入视图；同步输出 `quarantined_markets`，这些市场不请求价格。扫描器在请求盘口前拒绝隔离资产，报告 schema 1.1 的每个已分析市场附同一 asset_selection。V2 negRisk 估值沿用 ARB-28 的资格规则：要求显式 `module="neg_risk"`，缺失/未知模块输出 `unsupported_or_missing_negrisk_module`，Combo 输出 `unsupported_combo_module`；数字模块、其他模块及未验证 moduleAddress 均拒绝，不推断转换资格。

价格断点按 chain/ledger/asset 区分；新 Parquet 行保存 chain/ledger，查询在分页和去重前过滤当前所选账本和资产。旧数据不会删除，但旧裸 ID 断点和没有账本字段的历史行不会被猜测复用。旧 metadata 没有 asset_selection 时默认隔离，reason=identity_view_missing_resync_required；重新执行上述 sync 命令生成合格视图并从新的账本断点抓取。记录的历史报告保持原版本；新结果不宣称与旧 schema 字节相同。

跨语言 fixture：`research/tests/fixtures/selected-assets.json` 同时由 pytest 与 Playwright 的 TypeScript 契约测试读取，覆盖 v1/v2、双 ID、缺失/未知版本、outcome 乱序/缺失/重复、非法/数值 ID 和大于 53-bit 的字符串。离线 HTTP→同步→存储→FastAPI 测试验证同一所选资产与隔离路径；不使用网络。

## 同步与存储

- Gamma 按事件 ID/slug 读取，再逐市场补全详情及 tags。保留规则原文，不执行规则文本。negRisk 市场关联上游组 ID。`version` 缺失记为 `unknown`，不根据创建时间推断。历史 CTF token 与 V2 position token 分别标为 `v1`/`v2`；所有 Data 请求仍走 `/v2/`，不使用废弃接口。
- 首次每个 token 请求 `interval=max&bucket_seconds=43200` 并跟随全部游标。完整历史指官方可提供的全生命周期 **12 小时桶** 序列及终点，不代表逐笔、分钟、盘口或每桶均有数据。每个点的 `resolution_seconds` 为实际精度，0 表示精确时间点。
- 后续按完成时间水位增量同步，回看 24 小时刷新末端桶，显式窗口最多 15 天。上游窗口 `end` 不包含在内。在所选 chain/ledger 内按 `(market_id, token_id, timestamp, resolution_seconds)` 去重，修订取后写入值。同一完成水位重复执行不再请求价格，元数据和结算仍刷新。
- 每页持久保存请求参数、下一游标、目标水位。中断后从已提交游标恢复；上游拒绝游标时报错并保留进度，不默默跳过。未提供强制清空/全量重抓命令，先诊断游标失效原因。
- 空价格序列会记录完成水位，但同步结果列出 `empty_token_ids`；缺失结算列出 `missing_resolution_market_ids`。这些缺口不能作为完整数据验收成功；后续同步重新查询最近窗口和结算。
- 价格文件为 `prices/market=<id>/date=<UTC-date>/*.parquet`（Zstd）。SQLite 保存元数据、结算、文件清单和水位，DuckDB 查询已提交的 Parquet。文件落盘后，在同一 SQLite 事务中发布清单和水位。崩溃后未入清单的孤立文件不参与查询；重复/重叠页不在查询结果产生重复行。物理压缩及孤立文件清理尚未实现。
- Linux `flock` 排斥同目录并发写者。备份应先停止同步，再复制整个数据目录，包括 SQLite WAL/SHM；不要仅复制 manifest 或 Parquet。

## 请求边界

使用官方 `polymarket-client==0.15.0` 的公开 Gamma/Resolution 解析模型。其 `PublicClient` 构造函数未公开 transport 注入，因此 HTTP 边界使用 httpx，统一每次重试的限流、录制和禁止环境代理发现。不访问 SDK 私有字段，不使用旧 `py-clob-client`。

仅允许生产 Gamma 的事件/市场/tag GET，以及 Data `/v2/prices-history`、`/v2/resolutions` GET。不跟随重定向，不读取全局 HTTP(S)_PROXY，没有 endpoint override 或 Polymarket API 认证参数。每次请求复用 main 的 `PUBLIC_API_BUDGET`，另有跨进程令牌桶及 burst=1、19 req/s 保守桶；当前 OS 用户共享 `~/.cache/arb-research/polymarket-limits.sqlite3`。不协调其他工具/用户的独立请求。

NAS 通过 secret 环境注入获批的 `ARB_POLYMARKET_PROXY`（支持 socks5h）。CLI 只将它显式传入此公共数据 HTTP 客户端的 `proxy=`；不修改全局代理，不在日志、录制或仓库保存该值。没有配置时直连。代理初始化与传输异常经过脱敏。此授权仅用于公共只读访问，不能用来绕过地区限制启用交易。

同步复用 `arb_research.compliance.ComplianceGuard`、`GeoblockClient`，以同一个显式代理读取固定 geoblock URL，并保留 main 的超时、大小限制、缓存和拒绝策略。同步输出带规范化 `compliance` 状态，不录制上游 IP；`blocked=true` 时仍能读公共数据，`trading_allowed` 始终为 false。main 的交易 guard 未增加任何放行路径，`TRADING_ENABLED=true` 也不能放行。

公共数据 429、500/502/503/504 和传输错误最多尝试 4 次，指数退避加抖动；尊重秒数形式的 Retry-After，超过 60 秒报错留待续传。401/403 不重试。

## 离线测试与真实验收

```bash
uv run --project research --frozen pytest research/tests/test_polymarket.py -q
uv run --project research --frozen pytest research/tests -q
```

测试拦截真实 HTTP transport，CI 不打外网。`research/tests/fixtures/polymarket/recorded-events.json` 保存下表两个真实事件的 36 个生产公共 API 响应，含原始 URL、JSON 与录制时间，无代理配置或 geoblock IP。测试完整回放全部 16 个 token，并经 FastAPI 查询价格和结算。另有明确标为合成的 `contract.json`，用于游标翻页、崩溃续传、修订去重、50-50/争议、非法输入等故障回归。

2026-10-29 UTC 已实测：

| 事件 | 市场 / token | 价格条数 | 时间跨度（UTC） | 结算 |
| --- | --- | --- | --- | --- |
| 27824 / Fed decision in October? | 4 / 8 | 2,118 | 2025-06-20 00:00 至 2025-10-31 12:00 | 4 个市场均 resolved |
| 45883 / Fed decision in January? | 4 / 8 | 2,127 | 2025-09-18 00:00 至 2026-01-29 00:00 | 4 个市场均 resolved |

两个事件均为 negRisk 组，所有 token 历史非空，所有结算行齐全；价格实际精度为 43,200 秒桶及 resolution_seconds=0 的精确终点。再次在线增量同步收到 0 条新价格，查询条数与内容保持不变。相同水位的离线重复同步不请求价格。实测 geoblock 为 blocked=true（US/CA），trading_allowed=false。UMA 结算行的 payouts 不可用，price 为 0 或 1e18，保持原值并通过原始行溯源。

复录命令（已配置获批公共数据网络时）：

```bash
uv run --project research polymarket-data --data-dir .state/polymarket \
  sync --event 27824 --event 45883 \
  --record-dir .state/polymarket-recordings
```

文件包含公共请求 URL、HTTP 状态、录制时间、原始 JSON。核对所有 token 的价格条数/跨度/精度、结算 payout 和重复同步结果，写入 PR；审查并选取真实响应作为离线 fixture，保留来源与时间。不要提交整个运行数据目录。

NAS 直连超时已通过用户批准的公共数据代理解决；这不改变 geoblock 判定或交易拒绝。

## 官方契约

- [Data API v2 OpenAPI](https://data-api.polymarket.com/v2/openapi.json)
- [Gamma OpenAPI](https://docs.polymarket.com/api-spec/gamma-openapi.yaml)
- [官方 Python SDK](https://docs.polymarket.com/getting-started/python)
- [SDK 源码](https://github.com/Polymarket/py-sdk)

升级依赖时应重新核验契约并用录制响应回归。
