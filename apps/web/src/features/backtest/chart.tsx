import { useEffect, useRef, useState } from 'react'
import type { Vela, LineLikeSeries } from '@luxalgo/vela'
import type { NativeIndicatorContext } from '@luxalgo/vela/plugin'
import { Button } from '@/components/ui/button'
import { bars, recording, replayFrame, resultFor } from './replay'
import type { Engine } from './model'

export function BacktestChart() {
  const container = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Vela | null>(null)
  const [engine, setEngine] = useState<Engine>('realistic')
  const [status, setStatus] = useState('loading')
  const [count, setCount] = useState(32)
  const [hover, setHover] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [annotations, setAnnotations] = useState(true)
  const annotationsRef = useRef(annotations)
  annotationsRef.current = annotations
  const refreshRef = useRef<(() => void) | null>(null)
  const result = resultFor(engine)
  const frame = replayFrame(result, count)
  const selected = Math.max(0, Math.min(hover ?? count - 1, count - 1))
  const bar = bars[selected]
  const selectedEquity = frame.equity[selected]?.value

  useEffect(() => {
    let disposed = false
    let chart: Vela | undefined
    let unregister: (() => void) | undefined
    setStatus('loading')
    setPlaying(false)
    setHover(null)
    setCount(32)
    async function mount() {
      try {
        const [{ Vela }, plugin] = await Promise.all([
          import('@luxalgo/vela'),
          import('@luxalgo/vela/plugin'),
        ])
        if (disposed || !container.current) return
        const suffix = crypto.randomUUID()
        const types = [`arb-trades-${suffix}`, `arb-equity-${suffix}`]
        const refreshers = new Set<() => void>()
        types.forEach((type, index) => {
          plugin.registerNativeIndicator({
            type,
            title: index ? 'Equity · USD' : '成交 / 持仓 / 分析参考线',
            overlay: !index,
            paneHint: index ? 'new' : 'price',
            inputsSchema: () => [],
            defaultInputs: () => ({}),
            create() {
              let context: NativeIndicatorContext
              function emit() {
                const visible = context.bars()
                if (!visible.length) return
                const f = replayFrame(result, visible.length)
                const line = (
                  id: string,
                  title: string,
                  values: (number | null)[],
                  color: string,
                ): LineLikeSeries => ({
                  id,
                  title,
                  paneId: '',
                  kind: 'line',
                  points: f.positions.map((p, i) => ({
                    time: p.time,
                    value: values[i],
                  })),
                  style: { color, width: 1.5, lineStyle: 'solid' },
                })
                if (index) {
                  context.emit({
                    series: [
                      {
                        id: 'equity',
                        title: 'Equity USD',
                        paneId: '',
                        kind: 'line',
                        points: f.equity,
                        style: {
                          color: '#a3e635',
                          width: 2,
                          lineStyle: 'solid',
                        },
                      },
                    ],
                  })
                  return
                }
                const show = annotationsRef.current
                context.emit({
                  series: [
                    line(
                      'position',
                      '持仓均价',
                      f.positions.map((p) => p.average),
                      '#38bdf8',
                    ),
                    line(
                      'stop',
                      '止损参考 −2%',
                      f.positions.map((p) =>
                        show && p.average ? p.average * 0.98 : null,
                      ),
                      '#fb7185',
                    ),
                    line(
                      'target',
                      '止盈参考 +4%',
                      f.positions.map((p) =>
                        show && p.average ? p.average * 1.04 : null,
                      ),
                      '#fbbf24',
                    ),
                  ],
                  // Vela 0.9's native renderer ignores generic MarkerSeries.
                  // Drawing labels are supported in both canvas and WebGL backends.
                  labels: f.fills.map((fill, i) => ({
                    id: `fill-${i}`,
                    paneId: '',
                    xloc: 'bar_time',
                    x: Date.parse(fill.timestamp),
                    y: fill.price,
                    yloc: fill.side === 'buy' ? 'belowbar' : 'abovebar',
                    style: fill.side === 'buy' ? 'label_up' : 'label_down',
                    color: fill.side === 'buy' ? '#a3e635' : '#fb7185',
                    textColor: '#101114',
                    size: 'tiny',
                    textAlign: 'center',
                    fontFamily: 'monospace',
                    text: `${fill.side === 'buy' ? '买' : '卖'} ${fill.price.toFixed(2)}`,
                    tooltip: `${fill.timestamp} · ${fill.quantity} @ ${fill.price} · fee ${fill.fee} USD`,
                  })),
                  backgrounds: f.positions
                    .filter((p) => p.quantity > 0)
                    .map((p) => ({
                      id: `position-${p.time}`,
                      paneId: '',
                      from: p.time,
                      to: p.time + 3600000,
                      color: '#38bdf812',
                    })),
                })
                container.current?.setAttribute(
                  'data-fill-labels',
                  String(chart?.inspect().totals.labels ?? 0),
                )
              }
              return {
                start(ctx) {
                  context = ctx
                  refreshers.add(emit)
                  emit()
                },
                onBars: emit,
                onViewport() {},
                setInputs: emit,
                suspend() {
                  refreshers.delete(emit)
                },
                resume() {
                  refreshers.add(emit)
                  emit()
                },
                stop() {
                  refreshers.delete(emit)
                },
              }
            },
          })
        })
        unregister = () => types.forEach(plugin.unregisterNativeIndicator)
        chart = new Vela(
          container.current,
          {
            data: bars.map((b) => ({ ...b, time: Date.parse(b.timestamp) })),
            timeframe: '1h',
            theme: 'dark',
            live: false,
          },
          {
            dataFeed: {
              load: async () =>
                bars.map((b) => ({ ...b, time: Date.parse(b.timestamp) })),
              subscribe: () => () => {},
            },
          },
        )
        await chart.ready()
        if (disposed) return
        await chart.replay.start({ from: Date.parse(bars[31].timestamp) })
        if (disposed) return
        chart.addNativeIndicator(types[0])
        chart.addNativeIndicator(types[1])
        refreshRef.current = () => refreshers.forEach((refresh) => refresh())
        const sync = (frameViewport = false) => {
          if (disposed || !chart) return
          const state = chart.replay.state
          const revealed = state.active
            ? bars.length - state.remaining
            : bars.length
          setCount(revealed)
          setPlaying(state.playing)
          setHover(null)
          if (frameViewport) {
            // Keep the revealed data in view instead of Vela's default 192-bar
            // window, which otherwise leaves a nearly blank initial replay.
            chart.setVisibleRange({
              from: Date.parse(
                bars[state.active ? Math.max(0, revealed - 96) : 0].timestamp,
              ),
              to:
                Date.parse(bars[Math.max(32, revealed) - 1].timestamp) +
                3 * 3600000,
            })
          }
        }
        chart.on('replay:start', () => sync(true))
        chart.on('replay:step', () => sync(true))
        chart.on('replay:play', () => sync())
        chart.on('replay:pause', () => sync())
        chart.on('replay:end', () => sync(true))
        sync(true)
        chart.renderer.onCrosshairMove(({ time }) => {
          if (!disposed) {
            const index =
              time == null
                ? -1
                : bars.findIndex((b) => Date.parse(b.timestamp) === time)
            setHover(index < 0 ? null : index)
          }
        })
        chartRef.current = chart
        setStatus('ready')
      } catch {
        if (!disposed) setStatus('error')
      }
    }
    void mount()
    return () => {
      disposed = true
      chartRef.current = null
      refreshRef.current = null
      chart?.destroy()
      unregister?.()
    }
  }, [engine, result])

  const ready = status === 'ready'
  async function seek(next: number) {
    const chart = chartRef.current
    if (!chart) return
    try {
      if (next === bars.length) chart.replay.stop()
      else
        await chart.replay.start({ from: Date.parse(bars[next - 1].timestamp) })
    } catch {
      setStatus('error')
    }
  }
  return (
    <div>
      <div className="grid gap-3 border-b p-4 text-xs">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="font-medium text-primary">回放 / 标注 · 非实盘</span>
          <label>
            录制阶段{' '}
            <select
              aria-label="录制阶段"
              className="rounded border bg-background p-2"
              value={engine}
              onChange={(e) => setEngine(e.target.value as Engine)}
            >
              <option value="realistic">realistic · final</option>
              <option value="fast">fast · draft</option>
            </select>
          </label>
        </div>
        <p className="break-all text-[10px] text-muted-foreground">
          录制 run: {recording.run.run_id}
          <br />
          {recording.run.request.dataset_id} · {result.source.package}{' '}
          {result.source.version}
          <br />
          as_of: {recording.run.as_of} · 336 根合成小时线
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!ready}
            onClick={() => void seek(1)}
          >
            回到起点
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!ready || playing || count === bars.length}
            onClick={() => chartRef.current?.replay.step()}
          >
            下一根
          </Button>
          <Button
            size="sm"
            disabled={!ready || count === bars.length}
            onClick={() => {
              if (playing) chartRef.current?.replay.pause()
              else chartRef.current?.replay.play(1000 / speed)
            }}
          >
            {playing ? '暂停回放' : '播放回放'}
          </Button>
          <label>
            倍速{' '}
            <select
              aria-label="回放倍速"
              className="rounded border bg-background p-2"
              value={speed}
              onChange={(e) => {
                const value = Number(e.target.value)
                setSpeed(value)
                if (playing) chartRef.current?.replay.play(1000 / value)
              }}
            >
              {[1, 4, 16].map((s) => (
                <option key={s} value={s}>
                  {s}×
                </option>
              ))}
            </select>
          </label>
          <Button
            size="sm"
            variant="ghost"
            disabled={!ready}
            onClick={() => void seek(bars.length)}
          >
            显示完整 run
          </Button>
        </div>
        <label className="flex items-center gap-3">
          回放进度
          <input
            className="min-w-0 flex-1 accent-lime-400"
            aria-label="回放进度"
            type="range"
            min="1"
            max={bars.length}
            value={count}
            disabled={!ready}
            onChange={(e) => void seek(Number(e.target.value))}
          />
          <span data-testid="replay-count">
            {count} / {bars.length}
          </span>
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={annotations}
            onChange={(e) => {
              setAnnotations(e.target.checked)
              annotationsRef.current = e.target.checked
              refreshRef.current?.()
            }}
          />
          止损 / 止盈分析参考线
        </label>
        <p className="text-[10px] text-muted-foreground">
          蓝色：持仓均价与持仓区间；红色 −2% / 黄色
          +4%：均价参考线，未参与引擎撮合。买卖箭头为实际模拟成交。
        </p>
      </div>
      {status === 'error' && (
        <p role="alert" className="p-4 text-sm text-destructive">
          回放图表加载失败，请刷新重试。
        </p>
      )}
      <div
        ref={container}
        data-testid="backtest-chart"
        data-status={status}
        role="img"
        aria-label="Vela 价格、成交、持仓与净值共享时间轴"
        className="h-[560px] w-full xl:h-[640px]"
      />
      <div
        className="grid gap-2 border-t p-4 font-mono text-[10px]"
        data-testid="replay-readout"
        data-time={bar.timestamp}
      >
        <p>
          {bar.timestamp} · UTC · {hover == null ? '回放游标' : '图表光标'}
        </p>
        <p>
          O {bar.open.toFixed(2)} · H {bar.high.toFixed(2)} · L{' '}
          {bar.low.toFixed(2)} · C {bar.close.toFixed(2)} · V{' '}
          {bar.volume.toFixed(0)}
        </p>
        <p data-testid="replay-equity" data-time={bar.timestamp}>
          Equity {selectedEquity?.toFixed(2) ?? '—'} USD
        </p>
        <p data-testid="replay-position">
          当前持仓 {frame.quantity.toFixed(6)} · 均价{' '}
          {frame.quantity ? frame.average.toFixed(2) : '—'}
        </p>
        <p>价格、成交量、Equity 共用 UTC 横轴；移动光标或缩放时同步。</p>
      </div>
      <div className="max-h-48 overflow-auto border-t">
        <table className="w-full text-left text-[10px]">
          <caption className="p-3 text-left">
            当前阶段已回放成交{' '}
            <span data-testid="replay-fills">{frame.fills.length}</span> 笔
          </caption>
          <thead>
            <tr>
              {['时间 UTC', '方向', '数量', '价格 / USD'].map((s) => (
                <th
                  key={s}
                  className="px-3 pb-2 font-normal text-muted-foreground"
                >
                  {s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {frame.fills.map((fill, i) => (
              <tr key={i} className="border-t">
                <td className="p-3">{fill.timestamp.slice(5, 16)}</td>
                <td
                  className={
                    fill.side === 'buy' ? 'text-lime-400' : 'text-rose-400'
                  }
                >
                  {fill.side}
                </td>
                <td>{fill.quantity.toFixed(4)}</td>
                <td>{fill.price.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
