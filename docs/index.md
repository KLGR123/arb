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
- `docs/backtest-ui.md`：回测页 MVP、演示 Dataset、浏览器 preset 与 ARB-4 adapter 边界
- `docs/backtest-replay.md`：Vela 录制 run 回放、标注、共轴净值、阶段隔离与 fixture 复录
- `docs/backtest-api.md`：双引擎异步 API、冻结 Dataset、费用滑点、持久队列与 ARB-5 回放事件
- `docs/nautilus-polymarket-validation.md`：1.231.0 Polymarket 适配器、离线机制验证、历史接口失败点与协议抽样
- `docs/mm-backtest.md`：离线做市、L2 排队近似、返佣/奖励、PnL 分解与逆向选择统计

## 规则

1. 当前产品与启动说明以根目录 `README.md` 为准。
2. 提交决策以 `docs/runbooks/commit-policy.md` 为准；宣称完成或准备 commit 前必须检查该页。
3. 任务状态写入当前 `ARB-*` ticket / GitHub Issue 或明确 handoff，不为普通任务强制创建第二套执行计划系统。
