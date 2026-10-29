import { createFileRoute } from '@tanstack/react-router'
import { ArrowUpRight, ChartCandlestick, FlaskConical, Layers3 } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { SampleChart } from '@/components/sample-chart'

export const Route = createFileRoute('/')({ component: Foundation })

function Foundation() {
  const [count, setCount] = useState(48)
  return (
    <div className="mx-auto max-w-6xl px-5 sm:px-8">
      <header className="flex h-20 items-center justify-between border-b">
        <a href="/" aria-label="arb 首页" className="flex items-center gap-3 text-xl font-semibold tracking-tight">
          <span className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground"><ChartCandlestick size={19} /></span>
          arb<span className="ml-2 text-xs font-normal tracking-widest text-muted-foreground">RESEARCH</span>
        </a>
        <Badge variant="outline" className="gap-2"><span className="size-1.5 rounded-full bg-primary" />离线示例</Badge>
      </header>
      <main className="py-12 sm:py-16">
        <p className="mb-4 font-mono text-xs tracking-[0.2em] text-primary">FOUNDATION / 001</p>
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">研究，从这里开始。</h1>
        <p className="mt-4 max-w-xl text-sm leading-7 text-muted-foreground">一个轻量的研究起点。先看数据，再验证想法。当前图表使用固定合成数据，研究引擎可在本地独立运行 smoke。</p>

        <Card className="mt-9 gap-0 overflow-hidden py-0">
          <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-4 border-b px-5 py-5">
            <div>
              <CardTitle className="text-base">合成行情 <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">DEMO · 1H</span></CardTitle>
              <CardDescription className="mt-2">2026-01-01 UTC 起 · {count} 根小时 K 线 · 非真实价格</CardDescription>
            </div>
            <div className="flex gap-1" aria-label="示例数据长度">
              {[48, 96].map((value) => <Button key={value} size="sm" variant={count === value ? 'secondary' : 'ghost'} aria-pressed={count === value} onClick={() => setCount(value)}>{value} 小时</Button>)}
            </div>
          </CardHeader>
          <CardContent className="p-0"><SampleChart count={count} /></CardContent>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t px-5 py-3 font-mono text-xs text-muted-foreground">
            <span>STATIC DATA / NO PROVIDER</span>
            <a href="https://github.com/LuxAlgo/Vela" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-foreground">Vela™ by LuxAlgo<ArrowUpRight size={12} /></a>
          </div>
        </Card>

        <section aria-label="研究引擎" className="mt-6 grid gap-4 sm:grid-cols-2">
          <Card>
            <CardHeader>
              <div className="mb-3 flex items-center justify-between"><FlaskConical className="size-5 text-primary" /><Badge variant="secondary">fast</Badge></div>
              <CardTitle>VectorBT</CardTitle>
              <CardDescription className="leading-6">向量化研究与快速验证。独立 Python smoke 使用合成价格完成最小计算。</CardDescription>
            </CardHeader>
            <CardContent className="font-mono text-xs text-muted-foreground">uv run python -m arb_research.smoke --engine fast</CardContent>
          </Card>
          <Card>
            <CardHeader>
              <div className="mb-3 flex items-center justify-between"><Layers3 className="size-5 text-primary" /><Badge variant="secondary">realistic</Badge></div>
              <CardTitle>NautilusTrader</CardTitle>
              <CardDescription className="leading-6">Rust 核心与 Python API。独立 smoke 验证合成 K 线能被事件驱动引擎处理。</CardDescription>
            </CardHeader>
            <CardContent className="font-mono text-xs text-muted-foreground">uv run python -m arb_research.smoke --engine realistic</CardContent>
          </Card>
        </section>
      </main>
      <footer className="flex flex-wrap justify-between gap-3 border-t py-6 text-xs text-muted-foreground"><span>arb / research foundation</span><span>TanStack Start · shadcn/ui · Vela</span></footer>
    </div>
  )
}
