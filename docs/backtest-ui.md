## 回测工作台（ARB-25）

`/backtest` 默认连接 ARB-4 FastAPI：读取 `GET /datasets` 的 published / frozen 目录，提交 `POST /backtests`，保留服务端 UUID，并每秒串行查询 `GET /backtests/:id`。没有 stub、demo 开关或失败回退。目录或响应不符合 Zod 契约时显示错误，不允许借用 UI 演示目录提交。当前服务端目录只有两份不可变合成小时线 fixture，不是真实行情或生产 Dataset 发布服务。

运行结果保留 `source`、`as_of` 和阶段信息；`as_of` 是 Dataset 发布时间。右栏消费已完成阶段的收益、回撤、费用、净值与成交；没有计算 Sharpe。主图使用当前 run 保存的 `/bars` 和阶段结果回放，见 [回放说明](backtest-replay.md)。realistic 请求先得到 fast 草稿，再得到 final；部分失败保留可用草稿并显示各阶段错误码。图表阶段选择可以查看同一 UUID 的各阶段，不累加成交。

目录、请求和运行状态独立于首页盯盘 workspace。表单编辑不改变旧 run；再次提交创建新 run，不隐式升级原 run。加载预设清除当前结果。离开页面会中止请求与轮询；不取消服务端 job。刷新不恢复结果，可通过 API 查询已保存的 UUID。

## 连接本地服务

从仓库根目录，在三个终端分别启动：

```bash
# 终端一
cd research
uv sync --locked
uv run --locked uvicorn arb_research.api:create_app --factory --host 127.0.0.1 --port 8000

# 终端二
cd research
uv run --locked python -m arb_research.worker

# 终端三
npm ci
npm run dev
```

打开 `http://127.0.0.1:3000/backtest`。API 与 worker 必须使用同一个工作目录或同一个 `ARB_BACKTEST_DB` 绝对路径。没有 worker 时 run 会保持 queued，页面不会虚报结果。

Vite dev 和 `npm run preview` 都将同源 `/api/research` 转发到 `http://127.0.0.1:8000`。其他本地端口可在启动前设置 `ARB_BACKTEST_API_URL=http://127.0.0.1:8001`，或写入 `apps/web/.env`（参照 `.env.example`）。修改后重启 web。此变量只用于 Vite 服务端配置，不打包给浏览器；不放凭据、交易配置或公开行情代理。运行页面不调用任何 live provider。

独立部署构建产物时，需要部署层提供相同的 `/api/research` 反向代理（移除该前缀后转发 FastAPI）；Vite 的开发代理不会写入构建产物。此研究 API 未实现认证或租户隔离，应保持本机 loopback 使用。

## 校验与错误恢复

日期按 UTC 日闭区间发送，需落在服务端目录覆盖内；快慢线为 1–1000 的整数且 fast < slow；费用为 0–10000 bps；滑点为 0–10000 bps 的左闭右开区间；资金为 0.01–1e12 USD 且必须为整分。完整 bar 数需大于 slow_window，由 API 做最终校验，422 原因直接显示。请求字段、费用和引擎语义见 [API 合约](backtest-api.md)。

目录错误可点“重试目录”；查询或 bars 加载错误保留 UUID，“重试查询”只执行 GET。创建失败不会自动重试 POST：网络断开或超时可能发生在服务端入队之后，应先核对服务端记录再手动重建。failed、partial、空净值、无成交和等待结果分别显示；轮询在终态或请求错误时停止。每次请求 15 秒超时，无并发轮询。

预设仅在显式保存时写入浏览器 `localStorage` 的 `arb.backtest.preset.v1`，不会自动加载或保存结果。加载时重新检查服务端 frozen 目录和参数，非法、损坏或不兼容预设不改变当前表单。旧预设若包含不足一分资金或 10000 bps 滑点，需要修改后重新保存。浏览器存储失败不影响 API 提交。

## 验证

```bash
npm run typecheck
npm run build
npm run smoke:backtest
# 可选：使用构建产物执行相同验证
SMOKE_PREVIEW=1 npm run smoke:backtest
```

`smoke:backtest` 启动独立 loopback FastAPI、worker、随机端口 web 服务与临时 SQLite 数据库，运行 Playwright 后在同一个前台调用内清理服务。真实 job 用两引擎执行 frozen fixture，并验证 UUID、336 根 bars、净值与成交回放。其余浏览器测试用实际 API 录制响应覆盖双引擎、状态流转、校验、不可达、空结果、重试、停止轮询、preset、桌面与手机布局。浏览器阻止外部网络请求，不需要密钥。仅运行 `smoke:web` 时，真实服务测试跳过，其余测试仍使用录制 fixture。
