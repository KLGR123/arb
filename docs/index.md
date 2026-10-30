# Docs Index

## 目的

本文档是 `arb` 仓库文档系统的权威入口。
当前事实、运行手册与协作规则从这里进入。

## Start Here

1. `AGENTS.md`
2. `README.md`
3. `docs/runbooks/commit-policy.md`

## Runbooks

- `docs/runbooks/commit-policy.md`：何时 `commit-ready` / `merge-ready`、Conventional Commits、staging 卫生

## 产品

- `docs/compliance.md`：只读红线、交易拒绝开关、地区检测、审计与统一 guard 接入
- `docs/polymarket-dashboard.md`：Polymarket 三视图、合成 fixture、前端契约与后端对齐假设
- `docs/polymarket-data.md`：Polymarket 元数据、价格与结算同步、Parquet/DuckDB、只读 API 与 CLI
- `docs/orderbook-recorder.md`：只读 CLOB market 录制、JSONL 原始事件、历史 L2 重建、缺口报告与 NAS 运行
- `docs/backtest-ui.md`：回测页 MVP、演示 Dataset、浏览器 preset 与 ARB-4 adapter 边界
- `docs/backtest-replay.md`：Vela 录制 run 回放、标注、共轴净值、阶段隔离与 fixture 复录
- `docs/backtest-api.md`：双引擎异步 API、冻结 Dataset、费用滑点、持久队列与 ARB-5 回放事件
- `docs/nautilus-polymarket-validation.md`：1.231.0 Polymarket 适配器、离线机制验证、历史接口失败点与协议抽样
- [Polymarket 协议与 SDK 迁移评估](polymarket-protocol-v2.md)：CLOB / Protocol / Data V2 时间线、模块影响与后续验收清单
- `docs/nautilus-data-v2-loader.md`：只读 Data API v2 历史加载、Nautilus 字段映射、真实已结算事件回放与缓存对齐
- `docs/mm-backtest.md`：离线做市、L2 排队近似、返佣/奖励、PnL 分解与逆向选择统计
- `docs/negrisk-scanner.md`：只读 negRisk 扫描、逐档深度、费用、JSON v1 合约与真实扫描验证边界
- `docs/crypto-updown-pricing.md`：60 个已结算五分钟盘的 CEX TWAP 代理、公允概率、费用与延迟研究
- `docs/resolution-whale-features.md`：结算争议与大户统计、真实样本、可得时间约束、Parquet schema 与离线读取
- `docs/llm-forecast-eval-v0.md`：LLM 真实前瞻评估、时间截断、Brier 优势区间、纸面下注、实录与 mock 重放
- `docs/llm-ablation-v1.md`：非加密已结算市场的价格提示消融、配对高 effort 实验、费用与离线重放
- `docs/cross-venue-kalshi.md`：Kalshi × Polymarket 只读配对、Codex 规则缓存、费用与风险及真实验证边界

## 规则

1. 当前产品与启动说明以根目录 `README.md` 为准。
2. 提交决策以 `docs/runbooks/commit-policy.md` 为准；宣称完成或准备 commit 前必须检查该页。
3. 任务状态写入当前 `ARB-*` ticket / GitHub Issue 或明确 handoff，不为普通任务强制创建第二套执行计划系统。
