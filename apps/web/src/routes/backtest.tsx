import { createFileRoute, Link } from '@tanstack/react-router'
import {
  ArrowLeft,
  ArrowUpRight,
  ChartCandlestick,
  CircleDashed,
  Database,
  FlaskConical,
  Play,
  Save,
  SlidersHorizontal,
  Upload,
} from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Button as MotionButton } from '@/components/motion/button/base'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { BacktestChart } from '@/features/backtest/chart'
import {
  createStubRun,
  datasets,
  defaultConfig,
  parsePreset,
  presetKey,
  validateConfig,
  type BacktestConfig,
  type BacktestRun,
} from '@/features/backtest/model'

export const Route = createFileRoute('/backtest')({
  head: () => ({ meta: [{ title: '回测工作台 · arb' }] }),
  component: Backtest,
})

const control =
  'h-10 w-full min-w-0 rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-50'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid min-w-0 gap-2 text-xs text-muted-foreground">
      <span>{label}</span>
      {children}
    </label>
  )
}

function Backtest() {
  // Deliberately route-local. No singleton store, live workspace, or provider.
  const [config, setConfig] = useState<BacktestConfig>({ ...defaultConfig })
  const [run, setRun] = useState<BacktestRun>()
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [changed, setChanged] = useState(false)
  const [hydrated, setHydrated] = useState(false)
  useEffect(() => {
    setHydrated(true)
  }, [])
  const dataset = datasets.find((d) => d.id === config.datasetId)!
  const validation = validateConfig(config)
  function update<K extends keyof BacktestConfig>(
    key: K,
    value: BacktestConfig[K],
  ) {
    setConfig((previous) => ({ ...previous, [key]: value }))
    setChanged(true)
    setNotice('')
    setError('')
  }
  function numberField(
    key:
      'fastWindow' | 'slowWindow' | 'feeBps' | 'slippageBps' | 'initialCapital',
    label: string,
    min: number,
    max: number,
    step: number | 'any' = 1,
  ) {
    return (
      <Field label={label}>
        <input
          className={control}
          type="number"
          value={Number.isFinite(config[key]) ? config[key] : ''}
          min={min}
          max={max}
          step={step}
          required
          onChange={(event) => update(key, event.currentTarget.valueAsNumber)}
        />
      </Field>
    )
  }
  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    if (validation || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      setRun(await createStubRun({ ...config }))
      setChanged(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '创建失败，请重试。')
    } finally {
      setBusy(false)
    }
  }
  function savePreset() {
    setNotice('')
    if (validation) {
      setError(validation)
      return
    }
    try {
      localStorage.setItem(presetKey, JSON.stringify({ version: 1, config }))
      setError('')
      setNotice('预设已保存到当前浏览器。')
    } catch {
      setError('浏览器存储不可用，预设未保存。')
    }
  }
  function loadPreset() {
    setNotice('')
    try {
      const raw = localStorage.getItem(presetKey)
      if (!raw) {
        setError('当前浏览器还没有保存的预设。')
        return
      }
      const loaded = parsePreset(raw)
      setConfig(loaded)
      setRun(undefined)
      setChanged(false)
      setError('')
      setNotice('已加载预设；可创建新的 stub run。')
    } catch {
      setError('预设无法加载：数据已损坏、版本不支持或 Dataset 不再有效。')
    }
  }
  return (
    <div className="mx-auto max-w-[1800px] px-4 sm:px-7">
      <header className="flex min-h-20 flex-wrap items-center justify-between gap-4 border-b py-4">
        <div className="flex items-center gap-6">
          <Link
            to="/"
            aria-label="arb 首页"
            className="flex items-center gap-2 text-xl font-semibold"
          >
            <ChartCandlestick className="size-7 text-primary" />
            arb
          </Link>
          <span className="border-l pl-6 text-sm text-muted-foreground">
            研究工作台
          </span>
        </div>
        <Badge variant="outline" className="gap-2">
          <span className="size-1.5 rounded-full bg-primary" />
          离线回测 · 回放 / 标注
        </Badge>
      </header>
      <main className="py-7 sm:py-9">
        <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="mb-2 font-mono text-[10px] tracking-[0.22em] text-primary">
              RESEARCH / BACKTEST
            </p>
            <h1 className="text-2xl font-semibold tracking-tight">
              回测工作台
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              固定数据，独立验证。给每一个想法留下一次可追溯的运行。
            </p>
          </div>
          <Link
            to="/"
            className="inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={14} />
            返回研究首页
          </Link>
        </div>
        <div className="grid items-start gap-5 xl:grid-cols-[290px_minmax(0,1fr)_300px]">
          <Card className="min-w-0 gap-0 py-0">
            <CardHeader className="border-b px-5 py-4">
              <CardTitle className="flex items-center gap-2 text-sm">
                <SlidersHorizontal size={16} className="text-primary" />
                运行配置
              </CardTitle>
            </CardHeader>
            <CardContent className="p-5">
              <form onSubmit={submit} className="grid gap-6">
                <fieldset
                  className="grid min-w-0 gap-3"
                  disabled={!hydrated || busy}
                >
                  <legend className="mb-3 text-xs font-medium">
                    01 <span className="ml-2">数据快照</span>
                  </legend>
                  <Field label="已发布 Dataset">
                    <select
                      className={control}
                      value={config.datasetId}
                      onChange={(event) => {
                        const next = datasets.find(
                          (d) => d.id === event.target.value,
                        )
                        if (next) {
                          setConfig((c) => ({
                            ...c,
                            datasetId: next.id,
                            start: next.start,
                            end: next.end,
                          }))
                          setChanged(true)
                          setNotice('')
                          setError('')
                        }
                      }}
                    >
                      {datasets.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <div className="rounded-md border border-dashed p-3 text-[11px] leading-5 text-muted-foreground">
                    <div className="flex items-center gap-1.5 text-primary">
                      <Database size={12} />
                      Published · Frozen · 演示目录
                    </div>
                    <p
                      className="mt-1 break-all font-mono"
                      data-testid="dataset-id"
                    >
                      {dataset.id}
                    </p>
                    <p>{dataset.bars} 根合成 K 线 · UTC</p>
                    <p>
                      {dataset.start} → {dataset.end}
                    </p>
                  </div>
                </fieldset>
                <fieldset
                  className="grid min-w-0 gap-3 border-t pt-5"
                  disabled={!hydrated || busy}
                >
                  <legend className="float-left mb-3 w-full text-xs font-medium">
                    02 <span className="ml-2">引擎与策略</span>
                  </legend>
                  <div
                    className="grid grid-cols-2 gap-2"
                    role="group"
                    aria-label="回测引擎"
                  >
                    {(['fast', 'realistic'] as const).map((engine) => (
                      <Button
                        key={engine}
                        type="button"
                        variant={
                          config.engine === engine ? 'secondary' : 'outline'
                        }
                        aria-pressed={config.engine === engine}
                        onClick={() => update('engine', engine)}
                      >
                        {engine}
                      </Button>
                    ))}
                  </div>
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid="engine-name"
                  >
                    {config.engine === 'fast'
                      ? 'VectorBT · 快速向量化验证'
                      : 'NautilusTrader · 事件驱动模拟'}
                  </p>
                  <Field label="策略">
                    <select
                      className={control}
                      value="sma_cross"
                      onChange={() => {}}
                    >
                      <option value="sma_cross">双均线交叉 · SMA</option>
                    </select>
                  </Field>
                  <div className="grid grid-cols-2 gap-3">
                    {numberField('fastWindow', '快线窗口（根）', 1, 1000)}
                    {numberField('slowWindow', '慢线窗口（根）', 1, 1000)}
                  </div>
                </fieldset>
                <fieldset
                  className="grid min-w-0 gap-3 border-t pt-5"
                  disabled={!hydrated || busy}
                >
                  <legend className="float-left mb-3 w-full text-xs font-medium">
                    03 <span className="ml-2">费用与区间</span>
                  </legend>
                  <div className="grid grid-cols-2 gap-3">
                    {numberField('feeBps', '单边费用（bps）', 0, 10000, 'any')}
                    {numberField('slippageBps', '滑点（bps）', 0, 10000, 'any')}
                  </div>
                  <p className="text-[11px] leading-5 text-muted-foreground">
                    1 bps = 0.01%。下一根开盘价撮合；仅为 stub
                    配置，不执行交易。
                  </p>
                  <Field label="开始日期（UTC）">
                    <input
                      className={control}
                      type="date"
                      value={config.start}
                      min={dataset.start}
                      max={dataset.end}
                      required
                      onChange={(e) => update('start', e.target.value)}
                    />
                  </Field>
                  <Field label="结束日期（UTC）">
                    <input
                      className={control}
                      type="date"
                      value={config.end}
                      min={dataset.start}
                      max={dataset.end}
                      required
                      onChange={(e) => update('end', e.target.value)}
                    />
                  </Field>
                  {numberField(
                    'initialCapital',
                    '初始资金（USD）',
                    0,
                    1e12,
                    'any',
                  )}
                </fieldset>
                {validation && (
                  <p
                    role="alert"
                    className="text-xs leading-5 text-destructive"
                  >
                    {validation}
                  </p>
                )}
                <div className="grid gap-3 border-t pt-5">
                  <MotionButton
                    type="submit"
                    disabled={!hydrated || busy || !!validation}
                    className="w-full rounded-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                  >
                    <Play size={14} />
                    {busy ? '正在创建…' : '创建 stub run'}
                  </MotionButton>
                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={!hydrated || busy}
                      onClick={savePreset}
                    >
                      <Save size={13} />
                      保存预设
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={!hydrated || busy}
                      onClick={loadPreset}
                    >
                      <Upload size={13} />
                      加载预设
                    </Button>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    预设仅存当前浏览器，同名保存会覆盖。
                  </p>
                  <p role="status" className="text-xs leading-5 text-primary">
                    {notice}
                  </p>
                  {error && (
                    <p
                      role="alert"
                      className="text-xs leading-5 text-destructive"
                    >
                      {error}
                    </p>
                  )}
                </div>
              </form>
            </CardContent>
          </Card>
          <section aria-label="回测主图" className="grid min-w-0 gap-5">
            <Card className="gap-0 overflow-hidden py-0">
              <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3 border-b px-5 py-4">
                <div>
                  <CardTitle className="text-sm">价格与策略</CardTitle>
                  <p className="mt-1.5 font-mono text-[10px] text-muted-foreground">
                    RECORDED SYNTHETIC · 1H / UTC
                  </p>
                </div>
                <Badge variant="secondary">1H · 回放</Badge>
              </CardHeader>
              <CardContent className="p-0">
                <BacktestChart />
              </CardContent>
              <div className="flex flex-wrap items-center justify-between gap-2 border-t px-5 py-3 text-[10px] text-muted-foreground">
                <span className="font-mono">FROZEN DATA ONLY</span>
                <a
                  href="https://github.com/LuxAlgo/Vela"
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1"
                >
                  Vela™ by LuxAlgo
                  <ArrowUpRight size={11} />
                </a>
              </div>
            </Card>
            <div className="flex gap-3 rounded-lg border border-dashed p-5">
              <FlaskConical className="mt-0.5 size-5 shrink-0 text-primary" />
              <div>
                <p className="text-sm font-medium">回看一次完整的引擎运行。</p>
                <p className="mt-2 text-xs leading-6 text-muted-foreground">
                  主图加载 ARB-4 双引擎录制结果，独立于左侧配置。切换录制阶段可比较同一 run 的草稿和最终结果。左侧创建 stub run 仅记录配置，不会重新计算或替换这份录制。
                </p>
              </div>
            </div>
          </section>
          <section aria-label="回测结果" className="grid min-w-0 gap-5">
            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-5 py-4">
                <CardTitle className="flex items-center justify-between text-sm">
                  配置记录
                  <Badge variant="outline">{run ? 'STUB' : '未运行'}</Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-5">
                <div aria-live="polite">
                  {run ? (
                    <>
                      <p className="text-[10px] tracking-widest text-muted-foreground">
                        RUN ID
                      </p>
                      <p
                        data-testid="run-id"
                        className="mt-2 break-all font-mono text-xs leading-5 text-primary"
                      >
                        {run.run_id}
                      </p>
                      <p className="mt-3 text-xs">
                        {run.request.engine} · 配置已记录，未执行
                      </p>
                      <p className="mt-2 break-all font-mono text-[10px] leading-5 text-muted-foreground">
                        {run.request.dataset_id}
                        <br />
                        source: {run.source}
                        <br />
                        as_of: {run.as_of}
                      </p>
                      {changed && (
                        <p className="mt-3 text-xs text-amber-300">
                          配置已更改。再次创建 run 以记录新配置。
                        </p>
                      )}
                    </>
                  ) : (
                    <div className="py-3 text-center">
                      <CircleDashed className="mx-auto mb-3 size-6 text-muted-foreground" />
                      <p className="text-xs leading-6 text-muted-foreground">
                        配置完成后创建一次运行，
                        <br />
                        在这里查看 run id。
                      </p>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-5 py-4">
                <CardTitle className="text-sm">
                  配置运行净值{' '}
                  <span className="ml-2 font-mono text-[10px] font-normal text-muted-foreground">
                    EQUITY
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-5">
                <div className="flex h-28 items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground">
                  等待回测净值
                </div>
                <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-5">
                  {['总收益', '最大回撤', 'Sharpe', '成交数'].map((label) => (
                    <div key={label}>
                      <dt className="text-[11px] text-muted-foreground">
                        {label}
                      </dt>
                      <dd className="mt-1.5 font-mono text-xl">—</dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            </Card>
            <Card className="gap-0 overflow-hidden py-0">
              <CardHeader className="border-b px-5 py-4">
                <CardTitle className="text-sm">配置运行成交</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <table className="w-full text-left text-[11px]">
                  <caption className="sr-only">回测成交表</caption>
                  <thead className="border-b text-muted-foreground">
                    <tr>
                      <th className="px-4 py-3 font-normal">时间</th>
                      <th className="px-2 font-normal">方向</th>
                      <th className="px-4 font-normal">价格</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td
                        colSpan={3}
                        className="px-4 py-7 text-center text-muted-foreground"
                      >
                        暂无成交数据
                      </td>
                    </tr>
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </section>
        </div>
      </main>
      <footer className="flex flex-wrap justify-between gap-3 border-t py-5 text-[11px] text-muted-foreground">
        <span>arb / backtest workspace</span>
        <span>演示快照 · 无 live provider · 不下单</span>
      </footer>
    </div>
  )
}
