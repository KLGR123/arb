import { createFileRoute, Link } from '@tanstack/react-router'
import {
  ArrowLeft,
  ChartCandlestick,
  ChartNoAxesCombined,
  Crosshair,
  FlaskConical,
  Layers3,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { BookView, CalibrationView, PnlView } from '@/features/polymarket/views'
import { DataStatus } from '@/features/polymarket/data-status'
import { dashboardResultSchema, type Dashboard } from '@/features/polymarket/contract'
import { getDashboard } from '@/features/polymarket/load-dashboard'

export const Route = createFileRoute('/polymarket')({
  head: () => ({ meta: [{ title: 'Polymarket 研究看板 · arb' }] }),
  loader: async () => dashboardResultSchema.parse(JSON.parse(await getDashboard())),
  component: Polymarket,
})
const views = [
  { id: 'calibration', label: '概率校准', icon: Crosshair, number: '01' },
  { id: 'pnl', label: 'PnL 分析', icon: ChartNoAxesCombined, number: '02' },
  { id: 'book', label: '盘口回放', icon: Layers3, number: '03' },
] as const
type View = (typeof views)[number]['id']

function Polymarket() {
  const result = Route.useLoaderData()
  if (result.status !== 'ready') return (
    <main className="mx-auto max-w-4xl p-8">
      <h1 className="text-2xl">Polymarket 研究看板</h1>
      <p role="status" className="mt-6">{result.message}</p>
      <p className="mt-3">只读 / 纸面研究 · 交易保持拒绝</p>
      <Link to="/">研究首页</Link>
    </main>
  )
  return <DashboardPage data={result.data} referenceTime={result.loaded_at} />
}

function DashboardPage({ data: dashboard, referenceTime }: { data: Dashboard; referenceTime: string }) {
  const [view, setView] = useState<View>('calibration')
  const [hydrated, setHydrated] = useState(false)
  useEffect(() => setHydrated(true), [])
  return (
    <div className="mx-auto max-w-[1440px] px-4 sm:px-8 lg:px-10">
      <header className="flex min-h-20 flex-wrap items-center justify-between gap-3 border-b py-4">
        <div className="flex items-center gap-5 sm:gap-8">
          <Link
            to="/"
            aria-label="arb 首页"
            className="flex items-center gap-2.5 text-xl font-semibold"
          >
            <span className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <ChartCandlestick size={19} />
            </span>
            arb
          </Link>
          <span className="border-l pl-5 font-mono text-[10px] tracking-widest text-muted-foreground sm:pl-8">
            PREDICTION LAB
          </span>
        </div>
        <Badge variant="outline" className="gap-2 py-1.5">
          <span className="size-1.5 rounded-full bg-primary" />
          只读 / 纸面研究
        </Badge>
      </header>
      <main className="pb-10 pt-7 sm:pt-9">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div>
            <p className="mb-3 font-mono text-[10px] tracking-[.22em] text-primary">
              RESEARCH / POLYMARKET
            </p>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Polymarket 研究看板
              <span className="ml-3 align-middle font-mono text-[10px] font-normal text-muted-foreground">
                v0.1
              </span>
            </h1>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              衡量预测，拆解收益，回看流动性。
            </p>
          </div>
          <Link
            to="/"
            className="mt-1 flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={13} />
            研究首页
          </Link>
        </div>
        <div className="mt-6 flex gap-3 rounded-lg border border-primary/20 bg-primary/5 px-4 py-3.5">
          <FlaskConical size={17} className="mt-0.5 shrink-0 text-primary" />
          <p className="text-xs leading-6 text-muted-foreground">
            <span className="font-medium text-primary">
              {dashboard.source === 'synthetic-fixture' ? '合成 fixture · 离线演示' : dashboard.source === 'recorded-sync' ? '真实公共数据录制 · ARB-7 本地回放' : 'ARB-7 本地 sync · 只读快照'}
            </span>
            <span className="mx-2">/</span>
            {dashboard.source === 'synthetic-fixture' ? '全部题目、盘口与损益均为演示数据。' : '历史快照，非实时行情。ARB-7 未提供预测、纸面损益及 L2 盘口，对应视图保持空态。'}无下单功能。
          </p>
        </div>
        <DataStatus data={dashboard} referenceTime={dashboard.source === 'synthetic-fixture' ? dashboard.as_of : referenceTime} />
        <div
          role="tablist"
          aria-label="研究视图"
          className="mb-6 mt-7 grid grid-cols-3 gap-1 border-b"
        >
          {views.map(({ id, label, icon: Icon, number }, index) => (
            <button
              key={id}
              id={`tab-${id}`}
              role="tab"
              disabled={!hydrated}
              aria-selected={view === id}
              aria-controls={`panel-${id}`}
              tabIndex={view === id ? 0 : -1}
              onClick={() => setView(id)}
              onKeyDown={(e) => {
                const next =
                  e.key === 'ArrowRight'
                    ? (index + 1) % views.length
                    : e.key === 'ArrowLeft'
                      ? (index + views.length - 1) % views.length
                      : e.key === 'Home'
                        ? 0
                        : e.key === 'End'
                          ? views.length - 1
                          : null
                if (next != null) {
                  e.preventDefault()
                  setView(views[next].id)
                  document.getElementById(`tab-${views[next].id}`)?.focus()
                }
              }}
              className={`-mb-px flex items-center justify-center gap-2 border-b-2 px-2 pb-4 pt-2 text-xs outline-offset-4 focus-visible:outline-2 focus-visible:outline-primary sm:justify-start sm:px-5 sm:text-sm ${view === id ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground'}`}
            >
              <Icon size={16} />
              <span>{label}</span>
              <span className="ml-auto hidden font-mono text-[10px] opacity-50 sm:inline">
                {number}
              </span>
            </button>
          ))}
        </div>
        <section
          id={`panel-${view}`}
          role="tabpanel"
          aria-labelledby={`tab-${view}`}
          tabIndex={0}
          className="outline-offset-4 focus-visible:outline-2 focus-visible:outline-primary"
        >
          {view === 'calibration' ? (
            <CalibrationView data={dashboard} />
          ) : view === 'pnl' ? (
            <PnlView data={dashboard} />
          ) : (
            <BookView data={dashboard} />
          )}
        </section>
      </main>
      <footer className="flex flex-wrap justify-between gap-3 border-t py-5 font-mono text-[10px] leading-5 text-muted-foreground">
        <span>
          arb / prediction research · display schema v{dashboard.schema_version}
        </span>
        <span>
          source: {dashboard.source} · as_of: {dashboard.as_of}
        </span>
      </footer>
    </div>
  )
}
