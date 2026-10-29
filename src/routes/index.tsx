import { createFileRoute, Link } from '@tanstack/react-router'
import { Badge } from '@/components/ui/badge'
import { buttonVariants } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

export const Route = createFileRoute('/')({ component: Home })

const foundations = [
  { number: '01', title: '类型安全的路由', text: 'TanStack Start + React，以文件路由组织页面，支持服务端渲染。' },
  { number: '02', title: '可扩展的界面', text: 'Tailwind CSS + shadcn/ui，组件源码随项目维护；beUI 按需引入。' },
  { number: '03', title: '清晰的起点', text: '先把基础打稳，再逐步构建 crypto 与 Polymarket 产品。' },
]

function Home() {
  return (
    <main className="py-16 sm:py-24">
      <Badge variant="outline" className="mb-8 gap-2 rounded-full px-3 py-1">
        <span className="size-1.5 rounded-full bg-emerald-600" aria-hidden="true" />开发骨架
      </Badge>
      <p className="mb-4 text-xs font-medium tracking-[0.2em] text-muted-foreground">A STARTING POINT FOR ARB</p>
      <h1 className="max-w-3xl text-4xl leading-tight font-semibold tracking-tight sm:text-6xl">从一个清晰的起点，<br /><span className="text-muted-foreground">构建下一步。</span></h1>
      <p className="mt-6 max-w-xl text-base leading-8 text-muted-foreground">路由、服务端渲染和 UI 组件已经就位。这里是 arb 的开发起点，业务功能将从这里逐步展开。</p>
      <div className="mt-9 flex flex-wrap gap-3">
        <Link to="/example" className={buttonVariants()}>查看组件示例 <span aria-hidden="true">↗</span></Link>
        <a href="https://github.com/KLGR123/arb" className={buttonVariants({ variant: 'outline' })}>打开仓库</a>
      </div>
      <div className="mt-20 grid gap-4 md:grid-cols-3">
        {foundations.map((item) => (
          <Card key={item.number} className="shadow-none">
            <CardHeader>
              <p className="mb-5 font-mono text-xs text-muted-foreground">{item.number} / FOUNDATION</p>
              <CardTitle>{item.title}</CardTitle>
              <CardDescription className="leading-7">{item.text}</CardDescription>
            </CardHeader>
            <CardContent><div className="h-1 w-8 rounded bg-emerald-600/70" /></CardContent>
          </Card>
        ))}
      </div>
    </main>
  )
}
