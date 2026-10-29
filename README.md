# arb

基于 **TanStack Start（React）+ TypeScript + Tailwind CSS + shadcn/ui** 的项目骨架。已预留 beUI registry，按需安装组件。当前仅包含基础页面和交互示例，不包含 crypto / Polymarket 业务、钱包连接或交易功能。

## 本地启动

使用 Node.js **22.12+**（建议使用 `.nvmrc` 中的版本）和 npm 10+：

```sh
nvm use
npm ci
npm run dev
```

打开 <http://localhost:3000>。首次安装也可以使用 `npm install`；仓库提交 `package-lock.json`，统一使用 npm，避免混用 lockfile。启动不需要任何环境变量或密钥。

## 页面与验证

- `/`：基础栈介绍，使用 shadcn 的 Button、Card 和 Badge。
- `/example`：组件示例；点击“增加计数”更新本地状态，“重置”归零。刷新后重置。
- 未匹配路由：返回 404 页面，并提供返回首页的链接。

```sh
npm run typecheck  # 先生成文件路由，再进行严格 TypeScript 检查
npm run build      # 构建客户端与 SSR 服务端代码
npm run preview    # 本地预览构建结果（先 build）
```

`dev` 和 `preview` 默认使用 3000 端口。端口占用时可传入 `-- --port 3001 --strictPort`。预览命令用于本地检查；部署适配器和生产环境运维不在本次骨架范围内。

## 目录约定

```text
src/
  routes/
    __root.tsx       # HTML 文档、全局导航、样式与 404
    index.tsx        # 首页 /
    example.tsx      # 组件示例 /example
  components/ui/     # shadcn / beUI 安装的组件源码
  lib/utils.ts       # cn() 样式合并工具
  router.tsx         # 每个请求创建独立 router，注册类型
  routeTree.gen.ts   # 自动生成的路由树，禁止手工修改
  styles.css         # Tailwind 与 shadcn 主题变量
components.json      # shadcn CLI、目录别名与 registry 配置
vite.config.ts       # Tailwind、TanStack Start、React 插件
```

新增页面放在 `src/routes/`，文件名遵循 [TanStack 文件路由约定](https://tanstack.com/router/latest/docs/framework/react/routing/file-based-routing)。路由树在 `dev`、`build` 和 `typecheck` 时自动更新，并随代码提交。跨页面组件放在 `src/components/`，通用工具放在 `src/lib/`。`@/` 指向 `src/`。

## 添加 UI 组件

shadcn 组件以源码形式安装，可直接在仓库中修改：

```sh
npx shadcn@latest add dialog
```

`components.json` 已配置 `@beui` registry，主题使用与 beUI 兼容的 shadcn 语义变量。后续需要动效组件时，从 [beUI 组件目录](https://beui.dev/components) 选择组件并运行其安装命令，例如：

```sh
npx shadcn@latest add @beui/digit-swap
```

本次示例使用 shadcn，尚未引入 beUI 动效源码或 Motion。新增组件后检查生成文件、依赖及许可证，再运行类型检查和构建。组件初始化方式参考 [shadcn 的 TanStack Start 指南](https://ui.shadcn.com/docs/installation/tanstack) 与 [beUI 主题指南](https://beui.dev/docs/theme)。

## 配置与密钥

不要把密钥、助记词、交易凭据或真实账户配置提交到仓库。未来业务所需的敏感配置仅存 NAS secret，并通过服务端读取；`VITE_*` 会进入客户端代码，不能承载秘密。`.env` / `.env.*` 已忽略；如需说明变量，仅提交不含真实值的 `.env.example`。
