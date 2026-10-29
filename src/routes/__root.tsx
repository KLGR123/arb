import type { ReactNode } from 'react'
import { createRootRoute, HeadContent, Link, Outlet, Scripts } from '@tanstack/react-router'
import styles from '@/styles.css?url'

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: 'utf-8' },
      { name: 'viewport', content: 'width=device-width, initial-scale=1' },
      { title: 'arb · 开发起点' },
      { name: 'description', content: 'arb 的 TanStack Start 与 shadcn/ui 开发骨架。' },
    ],
    links: [{ rel: 'stylesheet', href: styles }],
  }),
  component: () => <Outlet />,
  shellComponent: RootDocument,
  notFoundComponent: () => (
    <main className="mx-auto max-w-3xl px-6 py-24">
      <h1 className="text-3xl font-semibold">页面不存在</h1>
      <Link to="/" className="mt-6 inline-block underline">返回首页</Link>
    </main>
  ),
})

function RootDocument({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head><HeadContent /></head>
      <body>
        <div className="mx-auto max-w-5xl px-6">
          <header className="flex items-center justify-between border-b py-6">
            <Link to="/" className="text-xl font-bold tracking-tight" aria-label="arb 首页">arb<span className="text-emerald-600">.</span></Link>
            <nav aria-label="主导航" className="flex gap-6 text-sm text-muted-foreground">
              <Link to="/" activeProps={{ className: 'text-foreground' }} activeOptions={{ exact: true }}>概览</Link>
              <Link to="/example" activeProps={{ className: 'text-foreground' }}>组件示例</Link>
            </nav>
          </header>
          {children}
          <footer className="border-t py-6 text-xs text-muted-foreground">arb / 项目骨架 · TanStack Start + shadcn/ui</footer>
        </div>
        <Scripts />
      </body>
    </html>
  )
}
