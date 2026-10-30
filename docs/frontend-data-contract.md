# 前端数据契约（ARB-24）

展示契约 **schema_version=2**，基于 ARB-7 `research/src/arb_research/polymarket/models.py`、`polymarket/api.py`、ARB-6 `compliance.py` 与 [ARB-18 协议评估](polymarket-protocol-v2.md)（2026-10-30）。实现：`apps/web/src/features/polymarket/contract.ts`，zod 运行时校验并推导 TS 类型。这里只消费仓库已有本地契约；没有新增上游请求、认证或交易入口。

## 来源与命名

| 展示字段 / schema | 来源与语义 |
| --- | --- |
| `event` / eventSchema、marketSchema、tokenSchema | ARB-7 本地事件/市场响应。`id/event_id/condition_id/protocol/tokens` 原样保留。nullable 标签、规则、closed、neg_risk 不补默认真假值 |
| `market_id/outcome_index` | UI 选择器；必须引用 event 内的市场。按市场显式 protocol 匹配 tokens，再按 outcome_index 选资产 |
| `prices` / historySchema | ARB-7 本地 prices 响应。`next_offset` 是本地分页，不是上游 `/v2` envelope 的 cursor；非 null 表示尚未读完 |
| `prices.data[].token_id` | 后端通用资产键，V1 为 CTF token，V2 为 position。十进制字符串，禁止 JS Number 中转。UI 按协议显示 token_id / position_id |
| `timestamp/price/resolution_seconds` | Unix **秒**、pUSD/share 的 [0,1] 展示浮点、桶精度秒数（0 为精确点）。渲染图表时才转换为毫秒；不声称精确会计或可成交报价 |
| `resolutions` / resolutionResultSchema | 本地单市场结算查询：data + missing_market_ids。status 保留新生命周期值，只有 resolved 表示终局；closed 不推断结算 |
| `payouts`、`price/proposed_price/reproposed_price` | payouts 已由 SDK 归一化为 pUSD/share，可为 null 或分拆结果。其余字段保留原始刻度字符串，不能作为价格或补 payouts=0 |
| `compliance` / complianceSchema | ARB-6 status / ARB-7 sync 输出，允许 null（未提供）。trading_allowed 固定 false；geoblock 含 checked_at/error/cached。fixture 为 null，不伪造真实检查 |
| `quality` | **UI 自有元数据**：collected_at（可空）、stale_after_seconds、expected_price_interval_seconds（可空）。ARB-7 查询尚无采集时间，不从价格时间/结算时间推造 |
| `forecasts/pnl/book` | 保留 ARB-13 合成研究序列，不是上游响应。forecasts 为已结算二元评价样本；PnL 为累计分项，fees ≤ 0；book 为重建后的完整 L2 快照 |

Protocol V1/V2、CLOB V2、Data API v2 和本展示 schema v2 是不同版本轴。CTF 与 V2 position 可同时存在；`selectedAsset` 只选市场协议对应账本，不在缺失时退回另一种 ID。未知协议、重复 ID/outcome/index 或缺失匹配资产会被隔离，价格显示 —。schema 拒绝非字符串 ID、非法概率、非有限数、错误时间、关联冲突、旧字段和未知展示包络字段。

## 数据质量与边界

空数组是合法数据；缺失价格/结算显示 — 或未知，空校准/PnL/盘口显示空态；单侧空盘口不计算价差/深度。结算 raw 仅保留，不作为 HTML 渲染。不将终局 payout 引入更早预测输入；forecast 必须 predicted_at < resolved_at。

新鲜度按展示包络 as_of 作为确定的参考时刻（离线回放无需随墙钟变 stale）：采集时间未知则提示未知，超阈值提示 stale，未来采集时间提示时钟异常。价格更新时间超过其桶精度加预期更新间隔则提示延迟；未配置间隔时不推断延迟。这些是展示策略，不是后端完整性保证；游标/分页结束也不代表覆盖整个生命周期。

geoblock blocked=true 只允许继续展示公共只读数据；error 或没有检查结果保持拒绝。地区未受限也不会开放交易。所有真实交易权限仍由现有 `arb_research.compliance` 决定，前端不替代 guard。本票未修改该 guard。

## 迁移与验证

旧展示 v1 的 `market.protocol_version`、统一 `market.token_id` 与非十进制 fixture-token 已移除；改用 ARB-7 `event.markets[].protocol/tokens`。不提供自动猜测 v1→v2 的迁移；旧持久化包络应重新生成。未来接真实数据需扩展 source（当前仅 synthetic-fixture）、记录真实质量元数据，并先验证本地响应再组合展示包络。上游 Gamma 的 clobTokenIds/positionIds 与 Data envelope 必须先经 ARB-7 适配，不能直接传给本 schema。

后端新增/改名、可空性或版本变更时，同步 schema、fixture 和本文；严格校验会显式暴露漂移。测试保持离线：`tests/polymarket-contract.spec.ts` 验证协议选择、精度、空值、stale/延迟、缺失结算、geoblock 与 SSR 提示；现有 Polymarket smoke 验证三视图、回放和无外部请求。

验证命令：`npm run typecheck`、`npm run build`、`npm run smoke:web`、`SMOKE_PREVIEW=1 npm run smoke:web`；后端全量回归在 `research/` 执行 `uv run --locked pytest`。
