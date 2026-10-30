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
import { useBacktest } from '@/features/backtest/use-backtest'
import { selectedResult } from '@/features/backtest/api'
import { BacktestChart } from '@/features/backtest/chart'
import {
  defaultConfig,
  parsePreset,
  presetKey,
  validateConfig,
  type BacktestConfig,
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
  const api = useBacktest()
  const { datasets, run, snapshot, busy } = api
  const result = run && selectedResult(run)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [changed, setChanged] = useState(false)
  const [hydrated, setHydrated] = useState(false)
  useEffect(() => {
    setHydrated(true)
  }, [])
  useEffect(() => {
    if (!datasets.length) return
    setConfig((previous) =>
      datasets.some((d) => d.id === previous.datasetId)
        ? previous
        : {
            ...previous,
            datasetId: datasets[0].id,
            start: datasets[0].start,
            end: datasets[0].end,
          },
    )
  }, [datasets])
  const dataset = datasets.find((d) => d.id === config.datasetId)
  const validation = api.catalogLoading
    ? undefined
    : validateConfig(config, datasets)
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
      | 'fastWindow'
      | 'slowWindow'
      | 'feeBps'
      | 'slippageBps'
      | 'initialCapital',
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
    setError('')
    setNotice('')
    if (await api.submit({ ...config })) setChanged(false)
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
      const loaded = parsePreset(raw, datasets)
      setConfig(loaded)
      api.clear()
      setChanged(false)
      setError('')
      setNotice('已加载预设；可创建新的回测 run。')
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
              {api.catalogLoading && (
                <p role="status" className="mb-4 text-xs">
                  正在读取 frozen Dataset 目录…
                </p>
              )}
              {api.catalogError && (
                <div className="mb-4 text-xs">
                  <p role="alert">{api.catalogError}</p>
                  <Button type="button" onClick={api.reloadCatalog}>
                    重试目录
                  </Button>
                </div>
              )}
              <form onSubmit={submit} className="grid gap-6">
                <fieldset
                  className="grid min-w-0 gap-3"
                  disabled={
                    !hydrated || busy || api.catalogLoading || !datasets.length
                  }
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
                          {d.symbol} · {d.id}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <div className="rounded-md border border-dashed p-3 text-[11px] leading-5 text-muted-foreground">
                    <div className="flex items-center gap-1.5 text-primary">
                      <Database size={12} />
                      Published · Frozen · API 目录
                    </div>
                    <p
                      className="mt-1 break-all font-mono"
                      data-testid="dataset-id"
                    >
                      {dataset?.id ?? '等待 API 目录'}
                    </p>
                    <p>{dataset?.bar_count ?? '—'} 根合成 K 线 · UTC</p>
                    <p>
                      {dataset?.start ?? '—'} → {dataset?.end ?? '—'}
                    </p>
                  </div>
                </fieldset>
                <fieldset
                  className="grid min-w-0 gap-3 border-t pt-5"
                  disabled={
                    !hydrated || busy || api.catalogLoading || !datasets.length
                  }
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
                  disabled={
                    !hydrated || busy || api.catalogLoading || !datasets.length
                  }
                >
                  <legend className="float-left mb-3 w-full text-xs font-medium">
                    03 <span className="ml-2">费用与区间</span>
                  </legend>
                  <div className="grid grid-cols-2 gap-3">
                    {numberField('feeBps', '单边费用（bps）', 0, 10000, 'any')}
                    {numberField('slippageBps', '滑点（bps）', 0, 10000, 'any')}
                  </div>
                  <p className="text-[11px] leading-5 text-muted-foreground">
                    1 bps = 0.01%。使用冻结数据，下一根开盘价模拟撮合。
                  </p>
                  <Field label="开始日期（UTC）">
                    <input
                      className={control}
                      type="date"
                      value={config.start}
                      min={dataset?.start}
                      max={dataset?.end}
                      required
                      onChange={(e) => update('start', e.target.value)}
                    />
                  </Field>
                  <Field label="结束日期（UTC）">
                    <input
                      className={control}
                      type="date"
                      value={config.end}
                      min={dataset?.start}
                      max={dataset?.end}
                      required
                      onChange={(e) => update('end', e.target.value)}
                    />
                  </Field>
                  {numberField(
                    'initialCapital',
                    '初始资金（USD）',
                    0.01,
                    1e12,
                    0.01,
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
                    disabled={
                      !hydrated ||
                      busy ||
                      api.catalogLoading ||
                      !datasets.length ||
                      !!validation
                    }
                    className="w-full rounded-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                  >
                    <Play size={14} />
                    {busy ? '正在创建…' : '创建回测 run'}
                  </MotionButton>
                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={
                        !hydrated ||
                        busy ||
                        api.catalogLoading ||
                        !datasets.length
                      }
                      onClick={savePreset}
                    >
                      <Save size={13} />
                      保存预设
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={
                        !hydrated ||
                        busy ||
                        api.catalogLoading ||
                        !datasets.length
                      }
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
                    FROZEN DATASET · 1H / UTC
                  </p>
                </div>
                <Badge variant="secondary">1H · 回放</Badge>
              </CardHeader>
              <CardContent className="p-0">
                {run &&
                result &&
                result.equity.length > 0 &&
                snapshot &&
                snapshot.bars.length > 0 ? (
                  <BacktestChart
                    key={run.run_id}
                    run={run}
                    snapshot={snapshot}
                  />
                ) : (
                  <p
                    className="p-8 text-sm text-muted-foreground"
                    data-testid="chart-empty"
                  >
                    {run
                      ? '等待运行结果与冻结 bars；无可回放数据。'
                      : '创建运行后，在这里回放该 run 的冻结数据。'}
                  </p>
                )}
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
                <p className="text-sm font-medium">回放当前运行的冻结数据。</p>
                <p className="mt-2 text-xs leading-6 text-muted-foreground">
                  创建后自动查询队列状态。fast 草稿与 realistic
                  最终结果属于同一个
                  run；主图可切换已完成阶段。离开页面停止查询，不取消服务端任务。
                </p>
              </div>
            </div>
          </section>
          <section aria-label="回测结果" className="grid min-w-0 gap-5">
            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-5 py-4">
                <CardTitle className="flex items-center justify-between text-sm">
                  运行状态
                  <Badge variant="outline">{run ? run.status : '未运行'}</Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-5">
                {api.error && (
                  <div className="mb-4 text-xs">
                    <p role="alert">{api.error}</p>
                    {run && (
                      <Button type="button" onClick={api.refresh}>
                        重试查询
                      </Button>
                    )}
                  </div>
                )}
                {run && ['failed', 'partial'].includes(run.status) && (
                  <p role="alert" className="mb-4 text-xs text-destructive">
                    {run.status === 'partial'
                      ? '部分阶段失败，显示已完成阶段结果。'
                      : '运行失败，无可用结果。'}
                    {Object.entries(run.stages)
                      .filter(([, stage]) => stage?.error)
                      .map(([name, stage]) => `${name}: ${stage?.error}`)
                      .join('；')}
                  </p>
                )}
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
                        {run.requested_engine} · {run.status} ·{' '}
                        {run.phase ?? '等待结果'}
                      </p>
                      <p className="mt-2 break-all font-mono text-[10px] leading-5 text-muted-foreground">
                        {run.request.dataset_id}
                        <br />
                        source:{' '}
                        {typeof run.source === 'string'
                          ? run.source
                          : `${run.source.package} ${run.source.version}`}
                        <br />
                        as_of: {run.as_of}
                      </p>
                      {changed && (
                        <p className="mt-3 text-xs text-amber-300">
                          配置已更改。再次创建 run 以执行新配置。
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
                  运行净值{' '}
                  <span className="ml-2 font-mono text-[10px] font-normal text-muted-foreground">
                    EQUITY
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-5">
                <div className="flex h-28 items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground">
                  {result?.equity.length
                    ? `最终净值 ${result.metrics.final_equity.toFixed(2)} USD · ${result.equity.length} 个净值点`
                    : run && ['succeeded', 'partial'].includes(run.status)
                      ? '运行已结束，无净值数据'
                      : '等待回测净值'}
                </div>
                <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-5">
                  {[
                    [
                      '总收益',
                      result
                        ? `${(result.metrics.total_return * 100).toFixed(2)}%`
                        : '—',
                    ],
                    [
                      '最大回撤',
                      result
                        ? `${(result.metrics.max_drawdown * 100).toFixed(2)}%`
                        : '—',
                    ],
                    [
                      '费用（USD）',
                      result?.metrics.fees_paid.toFixed(2) ?? '—',
                    ],
                    ['成交数', result?.metrics.fill_count ?? '—'],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-[11px] text-muted-foreground">
                        {label}
                      </dt>
                      <dd
                        className="mt-1.5 font-mono text-xl"
                        data-testid={`metric-${label}`}
                      >
                        {value}
                      </dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            </Card>
            <Card className="gap-0 overflow-hidden py-0">
              <CardHeader className="border-b px-5 py-4">
                <CardTitle className="text-sm">运行成交</CardTitle>
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
                    {result?.fills.length ? (
                      result.fills.map((fill, index) => (
                        <tr key={index} className="border-b">
                          <td className="px-4 py-3">
                            {fill.timestamp.slice(5, 16)}
                          </td>
                          <td className="px-2">{fill.side}</td>
                          <td className="px-4">{fill.price.toFixed(2)}</td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td
                          colSpan={3}
                          className="px-4 py-7 text-center text-muted-foreground"
                        >
                          {result ? '本阶段无成交' : '暂无成交数据'}
                        </td>
                      </tr>
                    )}
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
