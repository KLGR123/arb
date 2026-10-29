## 回测页 MVP（ARB-3）

`/backtest` 是独立 TanStack Start 文件路由。配置、run 和反馈全部由路由组件自己的 React state 持有；离开路由即销毁，不读取或修改盯盘 workspace。仅显式保存的配置会写入浏览器 `localStorage` 的 `arb.backtest.preset.v1`；不会自动加载，不持久化结果。每次保存覆盖一个 preset，可跨刷新加载；没有跨浏览器同步。无法使用浏览器存储时显示失败提示，仍可创建 stub run。

桌面宽度 1280px 起为配置 / Vela / 结果三栏，窄屏顺序堆叠。UI 复用地基的 shadcn 组件，运行按钮采用 [beUI button-base](https://beui.dev/r/button-base.json) 的 registry 源码及 Motion；相关源码位于 `components/motion/button/base.tsx`、`lib/ease.ts` 和 `lib/hooks/use-hover-capable.ts`。沿用现有 `utils.ts`，为按钮增加键盘 focus 样式；组件尊重系统减少动画设置。

## 数据与执行边界

`features/backtest/model.ts` 目前是 **UI 专用演示目录**：两个合成、不可变、已发布的 fixture id。`Published` 只表示演示目录发布状态，不代表生产 Dataset 服务已完成发布。目录包含覆盖区间、根数、来源和 `as_of`。不接受手填 id；preset 加载与运行前均重新检查 id 是否仍在已发布且冻结的白名单中。切换 Dataset 会重置区间，其他参数保留。

快线/慢线需为 1–1000 的整数且快线小于慢线；费用、滑点为 0–10000 bps；资金需大于 0 且不超过 1 万亿；日期必须有效并落在快照覆盖范围内。无效配置不能保存或运行。载入损坏、旧版本或无效 Dataset 的 preset 不改变现有配置。

Vela 使用 `data: []`、`live: false`，并注入固定返回空数组的离线 feed（0.9 版默认 feed 会将空数组视为待解析的 provider 请求）。仅在浏览器挂载后动态导入，卸载时销毁实例，没有行情 provider。表单在客户端接管后启用，避免 SSR 控件在事件绑定前接受操作。图表只是空态，不暗示已经加载所选 Dataset。结果显示 `—`，不会把未计算的收益或成交数展示成零。

## ARB-4 适配入口

`createStubRun` 是本地 adapter，**没有 HTTP 请求、队列或引擎执行**。每次点击生成 `stub-<UUID>`，记录提交时的配置快照。后续编辑不修改旧 run；界面会提示需要重新创建。切换引擎同样生成独立 run；本票不模拟 fast → realistic 的阶段升级。

与 ARB-4 已知要求对应：创建返回 `run_id`，结果模型预留 `metrics`、`equity`、`fills`，同时保留 `source` 与 `as_of`。当前 ARB-4 尚未提供已合入的字段 schema，下面是 UI adapter 的字段约定；接入时只需在此边界映射真实 wire schema，不应直接把 stub 当 API 实现。

```json
{
  "dataset_id": "demo-synthetic-1h-v1",
  "engine": "fast",
  "strategy": {"id": "sma_cross", "parameters": {"fast_window": 10, "slow_window": 30}},
  "execution": {"fee_bps": 10, "slippage_bps": 5, "fill_policy": "next_bar_open"},
  "start": "2026-01-01T00:00:00Z",
  "end": "2026-01-31T23:59:59Z",
  "initial_capital": 10000,
  "currency": "USD"
}
```

日期按 UTC 日闭区间转换。费用是单边比例，10 bps = 0.1%，滑点是相对成交价的比例偏移；当前只记录参数，不计算费用或撮合。真实费用/滑点语义、工具与币种限制由 ARB-4 合约确认并在 adapter 映射。

stub 响应为 `{ run_id, status: "queued", source: "ui_stub", as_of, request, metrics: {}, equity: [], fills: [] }`。`queued` 仅对应未来创建接口的确认形状；UI 明确显示“配置已记录，未执行”，没有真实排队任务。`as_of` 是 stub 创建时刻，不是行情新鲜度。

真实接入需将此 adapter 改为 `POST /backtests`，随后通过 `GET /backtests/:id` 读取状态和结果，并替换为服务端已发布 Dataset 目录。fast 草稿和 realistic 结果应按服务端同一 `run_id` 的阶段/来源增量消费。不要在 UI 另生成第二个服务端 run；也不要把演示 id 自动发送给生产服务。Vela 回放、标注与倍速由 ARB-5 接入。

## 验证

`npm run build`、`npm run typecheck`、`npm run smoke:web`。Playwright 同时覆盖桌面与手机：SSR、Vela 挂载、Dataset 切换、双引擎、唯一 run id、提交配置快照、preset 全字段跨刷新往返、无效参数/恶意 preset、存储不可用、导航状态隔离和外部网络请求为零。
