# Polymarket 研究看板

ARB-13 提供独立 `/polymarket` 路由：首页的「Polymarket 看板」进入。页面只读 / 纸面研究，无下单、钱包或认证入口，不请求任何外部接口。TanStack Start 负责 SSR；复用 shadcn Card / Badge / Button 与 beUI Motion 回放按钮。Vela 仅在浏览器挂载 PnL 时间曲线，切换视图或卸载时销毁实例并注销 native indicator。

## 三个视图

- **概率校准**：100 道合成二元已结算题，按预测概率十等分桶；横轴是桶内平均预测，纵轴是实际结算频率。Brier 为逐题 `mean((p-y)^2)`，市场分数用同时点 `q` 计算；优势为市场分数减模型分数。空桶不绘点，曲线连接非空桶，`p=1` 进入最后一桶。未计算置信区间，不据此宣称模型能力。
- **PnL 分析**：16 个每日 UTC 累计样本。Vela 主线为策略净收益、灰色虚线为独立合成基准。可切换分解堆叠：交易 / 返佣 / 奖励 / 结算跳变 / 费用，正项向上、负项向下，白线是代数和；展开每日表可查看所有数值。起点为零，不是账户净值；pUSD 按约等于 USD 展示。
- **盘口回放**：一个 YES token、12 个每分钟完整 L2 快照。深度图按最优价格向外累计数量；逐档表与当前时间同步。热力图展示完整录制区间，颜色强度按每档份数归一化，点击时间列、滑块、逐帧、1× / 4× 播放均定位同一游标；播放每秒推进一帧（4× 为每 250 ms 一帧），不是实时市场速度。到末帧停止；离开视图清理计时器，再进入回到第一帧。

概率和深度的横轴不是时间，使用原生 SVG；PnL 时间图使用 `@luxalgo/vela`。SVG 带描述，数据同时提供表格；页签支持左右键、Home、End，原生 range 与按钮支持键盘操作。桌面图表加侧栏，移动端纵向排列，长表在容器内横向滚动。

## 前端契约草案与适配假设

类型位于 `apps/web/src/features/polymarket/model.ts`，仓库内 JSON 位于 `fixtures/dashboard.json`。这是独立的展示契约 v1，**不是任何上游 API 已承诺的响应**；数据全部为合成 fixture，无真实市场数据或 LLM 评估结果。后端替换时应写 adapter 并校验外部 JSON，不能把网络响应直接断言为 `Dashboard`。

Issue 内「票 2 / 6 / 7」指本批序号，对应实际 ARB-7（数据层）、ARB-11（预测评估）、ARB-12（做市回测）；盘口来源另对齐 ARB-8。

| 模块 | 当前 TS / JSON 字段 | 后续对齐与假设 |
| --- | --- | --- |
| 包络 | `schema_version=1`, `source=synthetic-fixture`, `as_of`, `currency=pUSD` | 时间均为 RFC 3339 UTC；`as_of` 为快照生成时间；未来来源应在 adapter 中扩展为真实来源，不能复用 synthetic 标签 |
| 市场（ARB-7） | `market.event_id`, `market_id`, `token_id`, `question`, `outcome`, `protocol_version=v2`, `neg_risk` | event → market → token；fixture id 不可用于请求；outcome 为 YES 标签，协议单独标识。未来由数据层映射 token 标识、精度及 negRisk |
| 校准（ARB-11） | `forecasts[].market_id`, `predicted_at`, `resolved_at`, `p`, `q`, `outcome` | `p,q∈[0,1]`，`outcome∈{0,1}`；预测时点早于结算；q 必须与预测时点一致；当前题库和盘口市场相互独立。未接模型、检索、bootstrap CI 或投注模拟 |
| PnL（ARB-12） | `pnl[].timestamp`, `trading`, `rebates`, `rewards`, `settlement`, `fees`, `benchmark` | 金额为 pUSD 的**累计**数值。费用为负；净收益为五项代数和，返佣与奖励不能再计入交易项。结算单列以兼容做市框架；若上游给每步增量需先累计，若只有最终分解不能伪造历史曲线 |
| 盘口（ARB-8） | `book[].timestamp`, `bids/asks[].price`, `size` | 单 token 完整快照；价格为每份 pUSD，数量为份；按价格排序后累计；增量事件须在 adapter 中先重建、处理删除及缺口，不能直接作为完整快照 |

不依赖尚未合入的数据层 / 评估 / 做市模块；本路由没有敏感写路径，无须伪造 geoblock 或 guard 状态。未来接真实只读 API 时，应复用统一限流与退避，并保持测试离线。

## 验证与截图

```bash
npm ci
npm run build
npm run typecheck
npm run smoke:web
SMOKE_PREVIEW=1 npm run smoke:web
```

`tests/polymarket.spec.ts` 覆盖分数定义、概率边界、空桶、带符号 PnL、深度累计、SSR、离线请求边界、三视图、Vela 重挂载、时间定位 / 播放终止、页签键盘操作及响应式溢出。Playwright 在桌面与 Pixel 7 尺寸生成校准、PnL、分解堆叠、盘口截图；PR 引用 `docs/images/polymarket/` 中的已验证截图。
