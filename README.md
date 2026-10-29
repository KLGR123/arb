# arb

crypto / Polymarket 研究产品的最小地基。目前提供 TanStack Start SSR 示例页、shadcn/ui 组件和 Vela 假数据图，以及两个独立 Python 引擎 smoke。没有业务策略、真实行情、下单服务或密钥配置。

## 目录约定

```text
apps/web/                  TanStack Start + React + TypeScript
  src/routes/              文件路由（__root.tsx 为文档壳，index.tsx 为示例页）
  src/components/ui/       shadcn CLI 生成的源码，可直接修改
  src/components/          产品组件，含浏览器端 Vela 包装
  src/lib/                 前端工具和固定合成数据
research/                  独立 Python 项目及 uv.lock
  src/arb_research/         双引擎依赖 smoke；不是编排接口
tests/                     浏览器集成 smoke
```

## Web 本地启动

需要 Node.js 22.12+ 和 npm（已在 Node 22.22.3 验证）。在仓库根目录执行：

```bash
npm ci
npm run dev
# 打开 http://127.0.0.1:3000/
```

页面展示 shadcn Button / Badge / Card 和 `@luxalgo/vela` 合成小时 K 线，按钮可切换 48/96 根数据。数据时间从 2026-01-01 UTC 开始，数值不代表任何真实资产。页面不会请求行情服务，Vela 通过客户端动态导入避免 SSR 访问 DOM；切换数据和卸载时销毁图表实例。Vela™ 可见署名保留。

```bash
npm run build       # 生成 apps/web/dist/{client,server}
npm run typecheck  # 首次先 build，生成 routeTree.gen.ts
npm run preview    # 本地预览生产 SSR 构建
```

`preview` 用于本地验证；正式部署适配器、域名和服务托管留待部署任务。Vela 自身独立 chunk 约 1.1 MB（gzip 约 308 KB），当前最小示例接受这项体积警告。

Tailwind v4 使用 CSS 主题变量。`apps/web/components.json` 已配置 shadcn 别名和公开 `@beui` registry，可按需要加入 Motion 组件；当前示例只用 shadcn，不额外引入 Motion：

```bash
cd apps/web
npx shadcn@latest add @beui/button
```

## 研究侧本地启动

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，不需要本地 Rust 编译器（使用 NautilusTrader 预编译 wheel）。在仓库根目录执行：

```bash
cd research
uv sync --locked
uv run --locked python -m arb_research.smoke
# 也可逐个验证
uv run --locked python -m arb_research.smoke --engine fast
uv run --locked python -m arb_research.smoke --engine realistic
```

与 PieQuant 双引擎命名一致：`fast` → VectorBT，`realistic` → NautilusTrader（Rust 核心 + Python API）。这里仅声明依赖并验证加载/计算；没有 Dataset freeze、队列或完整执行契约，也不比较两个 smoke 的收益。

- VectorBT 0.28.5：四个固定价格 `[100,101,102,103]`，验证内置持有计算将 1000 变为 1030。
- NautilusTrader 1.231.0：内存模拟场所接收四根合成 K 线，只计数，断言全部处理且订单数为零。
- 固定 Python 3.12、pandas 2.x、VectorBT 0.28.5、NautilusTrader 1.x 作为本票验证基线；VectorBT 1.2 要求 pandas 3，留待一起升级验证。Plotly 限定 `<7`，因为 VectorBT 0.28 的主题仍使用 Plotly 7 已移除的 `scattermapbox`。

每个成功 smoke 输出一行 JSON；异常会让命令以非零状态退出。安装需要下载依赖，之后 smoke 不联网、不下载数据、不读密钥。所有交易密钥与交易配置只放 NAS secret，不放仓库或前端环境变量。

## 验证

```bash
npm run build
npm run typecheck
npx playwright install chromium
npm run smoke:web
SMOKE_PREVIEW=1 npm run smoke:web
cd research
uv run --locked python -m arb_research.smoke
```

Playwright 会自动启动并关闭本地服务，验证无 JavaScript 时的 SSR 内容、桌面/手机图表挂载、两次切换后的实例清理及无外部请求。浏览器所需系统库可在 Linux 用 `npx playwright install --with-deps chromium` 安装。后续 CI 可复用以上命令；本票未新增 GitHub Actions 工作流（运行凭据缺少 `workflow` 权限）。

上游参考：[TanStack Start](https://tanstack.com/start/latest/docs/framework/react/build-from-scratch)、[shadcn/ui](https://ui.shadcn.com/docs)、[beUI registry](https://beui.dev/docs/ai-agents)、[Vela](https://docs.luxalgo.com/vela/user/quickstart)、[VectorBT](https://vectorbt.dev/)、[NautilusTrader](https://nautilustrader.io/docs/)。实现 API 以锁定依赖的类型和源码为准；Nautilus 在线 latest 文档可能已指向 2.x。
