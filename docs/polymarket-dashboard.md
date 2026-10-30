# Polymarket 研究看板

ARB-13 提供独立 `/polymarket` 路由：首页的「Polymarket 看板」进入。页面只读 / 纸面研究，无下单、钱包或认证入口，不请求任何外部接口。TanStack Start 负责 SSR；复用 shadcn Card / Badge / Button 与 beUI Motion 回放按钮。Vela 仅在浏览器挂载 PnL 时间曲线，切换视图或卸载时销毁实例并注销 native indicator。

## 三个合成回归视图

以下是 `SMOKE_SYNTHETIC=1` 离线回归的交互能力；默认真实包缺少这些上游字段，因此显示空态。

- **概率校准**：100 道合成二元已结算题，按预测概率十等分桶；横轴是桶内平均预测，纵轴是实际结算频率。Brier 为逐题 `mean((p-y)^2)`，市场分数用同时点 `q` 计算；优势为市场分数减模型分数。空桶不绘点，曲线连接非空桶，`p=1` 进入最后一桶。未计算置信区间，不据此宣称模型能力。
- **PnL 分析**：16 个每日 UTC 累计样本。Vela 主线为策略净收益、灰色虚线为独立合成基准。可切换分解堆叠：交易 / 返佣 / 奖励 / 结算跳变 / 费用，正项向上、负项向下，白线是代数和；展开每日表可查看所有数值。起点为零，不是账户净值；pUSD 按约等于 USD 展示。
- **盘口回放**：一个 YES token、12 个每分钟完整 L2 快照。深度图按最优价格向外累计数量；逐档表与当前时间同步。热力图展示完整录制区间，颜色强度按每档份数归一化，点击时间列、滑块、逐帧、1× / 4× 播放均定位同一游标；播放每秒推进一帧（4× 为每 250 ms 一帧），不是实时市场速度。到末帧停止；离开视图清理计时器，再进入回到第一帧。

概率和深度的横轴不是时间，使用原生 SVG；PnL 时间图使用 `@luxalgo/vela`。SVG 带描述，数据同时提供表格；页签支持左右键、Home、End，原生 range 与按钮支持键盘操作。桌面图表加侧栏，移动端纵向排列，长表在容器内横向滚动。

## 前端契约与适配

ARB-24 建立展示包络，ARB-27 升级为 v3（共享 asset_selection），运行时 schema、后端响应类型与身份选择器位于 `apps/web/src/features/polymarket/contract.ts`。展示包经 server function 读取，并在服务端与路由入口用 zod 校验后进入组件。完整字段映射、版本边界和破坏性变更规则见 [前端数据契约](frontend-data-contract.md)。

页面默认使用真实 ARB-7 生产录制的本地重放包，也可配置本地 sync 导出文件。事件/市场、历史价格与结算沿用 ARB-7 本地字段；V2 资产显示为 position_id，原有 protocol_version 和统一 token_id 展示已移除。地区状态未提供时明确提示不可用，不能当成未受限。缺失价格/结算保留为空，真实数据新鲜度按服务端 loaded_at 判断，原 as_of 保留。

校准、PnL 和盘口的合成序列仅用于显式离线测试；真实包中这些字段为空。未来接 ARB-11/12/8 必须经过对应 adapter，不把 Data API 价格桶当作 L2。空序列显示空态，单侧空盘口不计算价差/深度；不伪造历史点、零价格或零收益。

## 验证与截图

```bash
npm ci
npm run build
npm run typecheck
npm run smoke:web
SMOKE_PREVIEW=1 npm run smoke:web
```

`tests/polymarket.spec.ts` 覆盖分数定义、概率边界、空桶、带符号 PnL、深度累计、SSR、离线请求边界、三视图、Vela 重挂载、时间定位 / 播放终止、页签键盘操作及响应式溢出。Playwright 在桌面与 Pixel 7 尺寸生成校准、PnL、分解堆叠、盘口截图；PR 引用 `docs/images/polymarket/` 中的已验证截图。


## ARB-26：默认本地真实数据

默认演示来源已切换为真实 ARB-7 录制重放包；自有 sync 包通过服务器环境变量 `ARB_POLYMARKET_DASHBOARD_FILE` 指定。默认不再载入合成研究序列，ARB-7 未提供的校准/PnL/L2 视图显示空态。来源、采集时间、as_of、地区状态和价格缺口直接来自包络；真实包新鲜度以加载时间判断。完整复现、导出与空态矩阵见 [前端数据契约](frontend-data-contract.md#默认真实包与本地导出arb-26)。旧的合成视图截图只作设计与离线回归参考。
