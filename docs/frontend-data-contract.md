# 前端数据契约（ARB-24 / ARB-26）

展示契约 **schema_version=3**，基于 ARB-7 `research/src/arb_research/polymarket/models.py`、`polymarket/api.py`、ARB-6 `compliance.py` 与 [ARB-18 协议评估](polymarket-protocol-v2.md)（2026-10-30）。实现：`apps/web/src/features/polymarket/contract.ts`，zod 运行时校验并推导 TS 类型。这里只消费仓库已有本地契约；没有新增上游请求、认证或交易入口。

## 来源与命名

| 展示字段 / schema | 来源与语义 |
| --- | --- |
| `event` / eventSchema、marketSchema、tokenSchema | ARB-7 本地事件/市场响应。`id/event_id/condition_id/protocol/tokens/asset_selection` 原样保留。nullable 标签、规则、closed、neg_risk 不补默认真假值 |
| `market_id/outcome_index` | UI 选择器；必须引用 event 内的市场。读取 market.asset_selection 的合格 assets，再按 outcome_index 选资产 |
| `prices` / historySchema | ARB-7 本地 prices 响应。`next_offset` 是本地分页，不是上游 `/v2` envelope 的 cursor；非 null 表示尚未读完 |
| `prices.data[].token_id` | 后端通用资产键，V1 为 CTF token，V2 为 position。十进制字符串，禁止 JS Number 中转。UI 按协议显示 token_id / position_id |
| `timestamp/price/resolution_seconds` | Unix **秒**、pUSD/share 的 [0,1] 展示浮点、桶精度秒数（0 为精确点）。渲染图表时才转换为毫秒；不声称精确会计或可成交报价 |
| `resolutions` / resolutionResultSchema | 本地单市场结算查询：data + missing_market_ids。status 保留新生命周期值，只有 resolved 表示终局；closed 不推断结算 |
| `payouts`、`price/proposed_price/reproposed_price` | payouts 已由 SDK 归一化为 pUSD/share，可为 null 或分拆结果。其余字段保留原始刻度字符串，不能作为价格或补 payouts=0 |
| `compliance` / complianceSchema | ARB-6 status / ARB-7 sync 输出，允许 null（未提供）。trading_allowed 固定 false；geoblock 含 checked_at/error/cached。fixture 为 null，不伪造真实检查 |
| `quality` | **展示质量元数据**：sync_incomplete（可空、可选）反映 ARB-7 checkpoints 与完成证据，collected_at（可空）、stale_after_seconds、expected_price_interval_seconds（可空）。ARB-7 查询尚无采集时间，不从价格时间/结算时间推造 |
| `forecasts/pnl/book` | ARB-7 未提供，真实包必须为空。仅 synthetic-fixture 可包含 ARB-13 合成研究序列；禁止混入真实来源 |
| `source/provenance` | local-sync 为本地存储导出；recorded-sync 为真实生产响应经 ARB-7 离线重放，附录制时间和原文件 SHA-256；synthetic-fixture 仅离线测试显式启用 |

Protocol V1/V2、CLOB V2、Data API v2 和本展示 schema v3 是不同版本轴。CTF 与 V2 position 可同时存在；共享解析器只选市场协议对应账本；`selectedAsset` 校验并读取后端 asset_selection，不从 tokens 重建或回退。未知协议、重复 ID/outcome/index 或缺失匹配资产会被隔离，价格显示 —。schema 拒绝非字符串 ID、非法概率、非有限数、错误时间、关联冲突、旧字段和未知展示包络字段。

## 数据质量与边界

空数组是合法数据；缺失价格/结算显示 — 或未知，空校准/PnL/盘口显示空态；单侧空盘口不计算价差/深度。结算 raw 仅保留，不作为 HTML 渲染。不将终局 payout 引入更早预测输入；forecast 必须 predicted_at < resolved_at。

合成回归按展示包络 as_of 作为确定参考时刻；真实 local-sync / recorded-sync 按服务端 loaded_at 判断新鲜度（包内 as_of 保留原值，旧包不会永久显示新鲜）：采集时间未知则提示未知，超阈值提示 stale，未来采集时间提示时钟异常。价格更新时间超过其桶精度加预期更新间隔则提示延迟；未配置间隔时不推断延迟。这些是展示策略，不是后端完整性保证；游标/分页结束也不代表覆盖整个生命周期。

geoblock blocked=true 只允许继续展示公共只读数据；error 或没有检查结果保持拒绝。地区未受限也不会开放交易。所有真实交易权限仍由现有 `arb_research.compliance` 决定，前端不替代 guard。本票未修改该 guard。

## 迁移与验证

展示 schema 2 → 3：market 新增必需的 asset_selection（selected-assets.v1），asset_id 在整个 JSON/TS/SSR 路径保持字符串。旧展示包络需重新组合，旧 ARB-7 本地 metadata 应先重新 sync，不能在前端从 tokens 自动补视图。隔离视图保留 reason，资产与盘口不显示可消费数据。共享实现和字段语义见 [只读数据层](polymarket-data.md#共享所选资产视图arb-27)。

旧展示 v1 的 `market.protocol_version`、统一 `market.token_id` 与非十进制 fixture-token 已移除；改用 ARB-7 `event.markets[].protocol/tokens`。不提供自动猜测 v1→v2 的迁移；旧持久化包络应重新生成。ARB-26 已扩展 source，真实包先验证本地响应再组合展示包络。上游 Gamma 的 clobTokenIds/positionIds 与 Data envelope 必须先经 ARB-7 适配，不能直接传给本 schema。

后端新增/改名、可空性或版本变更时，同步 schema、fixture 和本文；严格校验会显式暴露漂移。测试保持离线：`tests/polymarket-contract.spec.ts` 验证协议选择、精度、空值、stale/延迟、缺失结算、geoblock 与 SSR 提示；现有 Polymarket smoke 验证三视图、回放和无外部请求。

验证命令：`npm run typecheck`、`npm run build`、`npm run smoke:web`、`SMOKE_PREVIEW=1 npm run smoke:web`；后端全量回归在 `research/` 执行 `uv run --locked pytest`。


## 默认真实包与本地导出（ARB-26）

`/polymarket` 的 TanStack Start GET server function 只读服务器文件，不请求上游。默认使用仓库内 `apps/web/src/features/polymarket/fixtures/recorded-sync.json`：它由 ARB-7 的真实公开生产响应录制（2026-10-29 16:41:16 UTC）重放得到，事件 27824、首个市场 553810，选中 outcome_index=0。包内包含所选市场两个 token 共 528 个历史价格点、真实结算和当时的 geoblock（blocked=true）；它是历史录制，不是实时行情。SHA-256 保存在 provenance 中。前端继续遵守市场显式协议，不把历史 CTF 改称 V2 position。

从仓库根目录复现默认包（`--data-dir` 必须尚不存在；离线，绝不访问上游）：

```bash
uv run --project research --locked python -m arb_research.polymarket_dashboard \
  --recording research/tests/fixtures/polymarket/recorded-events.json \
  --data-dir .state/dashboard-replay-new --event 27824 \
  --output .state/polymarket-dashboard.json
ARB_POLYMARKET_DASHBOARD_FILE="$PWD/.state/polymarket-dashboard.json" npm run dev
```

使用自己的 ARB-7 本地 sync，仍从仓库根目录执行：

```bash
mkdir -p .state
uv run --project research --locked polymarket-data --data-dir .state/polymarket \
  sync --event 27824 > .state/polymarket-sync-result.json
uv run --project research --locked python -m arb_research.polymarket_dashboard \
  --data-dir .state/polymarket --event 27824 --market 553810 \
  --sync-result .state/polymarket-sync-result.json \
  --output .state/polymarket-dashboard.json
ARB_POLYMARKET_DASHBOARD_FILE="$PWD/.state/polymarket-dashboard.json" npm run dev
```

只有显式 `sync` 联网，遵守既有显式代理、限流和 ARB-6 guard。导出只以 SQLite mode=ro 读取 manifest，再通过既有 DuckDB 查询已提交 Parquet；不创建/迁移数据源、不修改采集器核心。导出在同一个 SQLite 只读事务中固定 manifest 快照，确保元数据、结算和已提交 Parquet 清单来自同一数据库快照；Parquet 页保持不可变。导出同时持有 ARB-7 sync.lock 的只读共享锁；采集中拒绝导出，不改变锁文件内容。中断后待续游标、缺失或未对齐水位通过 quality.sync_incomplete 暴露为 partial；ARB-7 未持久化同步完成回执：普通本地导出即使水位对齐也保持 sync_incomplete=null（未知），避免漏掉首个价格页提交前失败的刷新；只有本进程成功完成的隔离离线重放可标 false。水位对齐和重放成功均不保证覆盖全部市场生命周期。导出原子替换输出文件；下一次页面加载重新读取。默认每市场至多 10000 个点，`--limit 1..10000` 可调，保留 `next_offset` 以暴露截断。导出路径禁止位于源数据目录内（含解析后的符号链接别名），也不能覆盖输入录制或 sync 回执。

`--sync-result` 仅取对应 event_id 的 compliance；未提供则为 null。ARB-7 目前不持久化采集完成时间，因此默认 collected_at=null；只有明确知道采集时间时才传 `--collected-at 2026-10-30T04:00:00Z`。不从导出时间、mtime、结算时间或价格时间推造采集时间。录制重放保留原录制时间和原地区检查时间，绝不伪装本轮在线检查。上游 raw 不作为 HTML 执行。

`ARB_POLYMARKET_DASHBOARD_FILE` 是**服务器专用**绝对路径配置，不使用 VITE_ 前缀、不接受浏览器参数；未设置时使用随构建发布的真实录制。显式指定文件缺失或无效时显示错误空态，不回退到录制或合成数据。JSON 文件最多 8 MiB，经 zod 严格校验。合成包默认拒绝；仅离线测试可同时设置 `ARB_POLYMARKET_ALLOW_SYNTHETIC=1`，页面仍明确标为合成。

| 条件 | 展示 |
| --- | --- |
| 文件不存在 / 不可读 / JSON 或 schema 无效 | 缺包或契约错误空态，无假数据回退 |
| prices.data=[] | 价格 —，暂无历史，不补零 |
| collected_at=null / 超过阈值 / 晚于参考时间 | 时间未知 / stale / 时钟异常 |
| prices.next_offset 非 null / sync_incomplete=true / null | partial，分页未读完 / 同步中断待续 / 完成状态未知 |
| 所选资产相邻桶间隔超过前桶精度 + 配置容差 | gap，不插值；精度 0 的点不据此推断缺桶 |
| resolutions 缺失 / payouts=null | 未知 / —，closed 不推断终局 |
| geoblock blocked / error / 未提供 / 未受限 | 对应提示，全部保持只读与交易拒绝 |
| forecasts / pnl / book 为空 | 校准、PnL、盘口分别显示无数据，不冒充上游能力 |

验证：`npm run smoke:web` 覆盖默认真实包；`SMOKE_SYNTHETIC=1 npm run smoke:web -- tests/polymarket.spec.ts tests/polymarket-contract.spec.ts` 显式运行合成图表/盘口回放回归；`SMOKE_PREVIEW=1 npm run smoke:web` 验证生产 SSR。Python `test_polymarket_dashboard.py` 从原录制重建并逐字段比对随仓库发布的包，拦截真实 HTTP，验证只读 manifest、分页和缺失存储行为。

并行开发机器若 3000 端口已占用，使用 `SMOKE_PORT=32626 npm run smoke:web`；测试仅允许配置的服务 origin，不放宽外部请求边界。
