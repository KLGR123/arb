import { useEffect, useState, type ReactNode } from 'react'
import { Pause, Play, RotateCcw, SkipForward } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Button as MotionButton } from '@/components/motion/button/base'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { CalibrationChart, DepthChart, PnlChart, PnlStack } from './charts'
import {
  brier,
  calibration,
  colors,
  components,
  dashboard,
  depth,
  labels,
  marketFor,
  selectedAsset,
  money,
  percent,
  totalPnl,
  utcTime,
} from './model'

export function Panel({
  title,
  note,
  children,
}: {
  title: string
  note?: string
  children: ReactNode
}) {
  return (
    <Card className="min-w-0 gap-0 overflow-hidden py-0 shadow-none">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 border-b px-5 py-4">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        {note && (
          <span className="font-mono text-[10px] text-muted-foreground">
            {note}
          </span>
        )}
      </CardHeader>
      <CardContent className="p-0">{children}</CardContent>
    </Card>
  )
}
export function Stat({
  label,
  value,
  detail,
  accent = false,
}: {
  label: string
  value: string
  detail: string
  accent?: boolean
}) {
  return (
    <Card className="gap-0 p-5 shadow-none">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p
        className={`mt-3 font-mono text-2xl tracking-tight ${accent ? 'text-primary' : ''}`}
      >
        {value}
      </p>
      <p className="mt-2 text-[11px] leading-5 text-muted-foreground">
        {detail}
      </p>
    </Card>
  )
}
export function CalibrationView({
  data = dashboard,
}: {
  data?: typeof dashboard
}) {
  const { forecasts } = data
  const modelScore = brier(forecasts),
    marketScore = brier(forecasts, 'q')
  const bins = calibration(forecasts)
  return (
    <div className="grid gap-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="模型 Brier"
          value={modelScore?.toFixed(4) ?? '—'}
          detail="越低越好 · 二元结算值 0 / 1"
          accent
        />
        <Stat
          label="市场 Brier"
          value={marketScore?.toFixed(4) ?? '—'}
          detail="预测时点市场价 q · 同一批题目"
        />
        <Stat
          label="相对市场优势"
          value={
            marketScore === null || modelScore === null
              ? '—'
              : money(marketScore - modelScore, 4)
          }
          detail="市场 Brier − 模型 Brier · 未估计置信区间"
        />
        <Stat
          label="已结算样本"
          value={String(forecasts.length)}
          detail="全部为合成题目 · 不代表预测能力"
        />
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
        <Panel title="预测与现实，有多接近？" note="CALIBRATION / 10 BINS">
          <div className="flex flex-wrap gap-5 px-5 pt-5 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-2">
              <span className="size-2 rounded-full bg-emerald-400" />
              模型校准
            </span>
            <span>┄ 完美校准</span>
          </div>
          <div className="p-3 sm:px-5">
            <CalibrationChart forecasts={forecasts} />
          </div>
          <p className="border-t px-5 py-4 text-xs leading-6 text-muted-foreground">
            每个点代表一个概率区间。越接近对角线，预测概率与实际结算频率越一致。图表按每题等权计算，分桶仅用于展示。
          </p>
        </Panel>
        <Panel title="分桶明细" note="P → Y">
          <table className="w-full text-right font-mono text-[11px]">
            <caption className="sr-only">
              预测概率分桶、平均预测、实际频率与样本数
            </caption>
            <thead className="text-muted-foreground">
              <tr>
                {['区间', '预测', '实际', '题数'].map((s) => (
                  <th key={s} className="px-3 py-3 font-normal">
                    {s}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bins.map((b, i) => (
                <tr key={i} className="border-t">
                  <td className="px-3 py-3 text-muted-foreground">
                    {percent(b.lower)}–{percent(b.upper)}
                  </td>
                  <td className="px-3 py-3">
                    {b.predicted == null ? '—' : percent(b.predicted)}
                  </td>
                  <td className="px-3 py-3 text-primary">
                    {b.actual == null ? '—' : percent(b.actual)}
                  </td>
                  <td className="px-3 py-3">{b.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  )
}

export function PnlView({ data = dashboard }: { data?: typeof dashboard }) {
  const [stacked, setStacked] = useState(false)
  const points = data.pnl,
    last = points[points.length - 1],
    net = last ? totalPnl(last) : null
  if (!last || net === null)
    return <p role="status">暂无 PnL 数据，未按零收益处理。</p>
  return (
    <div className="grid gap-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="策略累计净收益"
          value={money(net)}
          detail="pUSD · 已包含返佣、奖励、结算与费用"
          accent
        />
        <Stat
          label="固定基准"
          value={money(last.benchmark)}
          detail="pUSD · 独立合成曲线，非真实持仓"
        />
        <Stat
          label="超额收益"
          value={money(net - last.benchmark)}
          detail="pUSD · 策略净收益 − 固定基准"
        />
        <Stat
          label="研究区间"
          value={`${points.length} 天`}
          detail={`${points[0].timestamp.slice(0, 10)} → ${last.timestamp.slice(0, 10)} · UTC 每日累计值`}
        />
      </div>
      <Panel title="收益轨迹" note="CUMULATIVE / pUSD">
        <div className="flex flex-wrap items-center justify-between gap-4 px-5 py-4">
          <div className="flex gap-4 text-[11px] text-muted-foreground">
            <span className="text-blue-400">● 策略净收益</span>
            <span>┄ 固定基准</span>
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={stacked}
              onChange={(e) => setStacked(e.target.checked)}
              className="accent-emerald-400"
            />
            显示 PnL 分解堆叠
          </label>
        </div>
        {stacked ? (
          <div className="px-3 pb-3 sm:px-5">
            <PnlStack points={points} />
          </div>
        ) : (
          <PnlChart points={points} />
        )}
        {stacked && (
          <div className="flex flex-wrap gap-4 border-t px-5 py-4 text-[11px] text-muted-foreground">
            {components.map((key) => (
              <span key={key} className="flex items-center gap-2">
                <span
                  className="size-2 rounded-sm"
                  style={{ background: colors[key] }}
                />
                {labels[key]}
              </span>
            ))}
            <span>白线：净收益</span>
          </div>
        )}
      </Panel>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
        <Panel title="净收益构成" note="FINAL SNAPSHOT">
          <div className="grid grid-cols-2 gap-5 p-5 sm:grid-cols-5">
            {components.map((key) => (
              <div key={key}>
                <p className="flex items-center gap-2 text-[11px] text-muted-foreground">
                  <span
                    className="size-1.5 rounded-full"
                    style={{ background: colors[key] }}
                  />
                  {labels[key]}
                </p>
                <p
                  className="mt-2 font-mono text-lg"
                  style={{ color: colors[key] }}
                >
                  {money(last[key])}
                </p>
              </div>
            ))}
          </div>
          <details className="border-t px-5 py-4 text-xs text-muted-foreground">
            <summary className="cursor-pointer">查看每日累计明细</summary>
            <div className="mt-4 overflow-auto">
              <table className="w-full min-w-[600px] text-right font-mono text-[11px]">
                <caption className="sr-only">
                  每日 UTC 累计 PnL，单位 pUSD
                </caption>
                <thead>
                  <tr>
                    {[
                      'UTC 日期',
                      ...components.map((k) => labels[k]),
                      '净收益',
                      '基准',
                    ].map((s) => (
                      <th key={s} className="p-2 font-normal">
                        {s}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {points.map((p) => (
                    <tr key={p.timestamp} className="border-t">
                      <td className="p-2">{p.timestamp.slice(0, 10)}</td>
                      {components.map((key) => (
                        <td key={key} className="p-2">
                          {money(p[key])}
                        </td>
                      ))}
                      <td className="p-2">{money(totalPnl(p))}</td>
                      <td className="p-2">{money(p.benchmark)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </Panel>
        <Panel title="阅读口径">
          <div className="grid gap-3 p-5 text-xs leading-6 text-muted-foreground">
            <p>所有数值为累计损益，起始值为 0；pUSD 按约等于 USD 展示。</p>
            <p>
              堆叠图把正项放在零线上方、负项放在下方。白线是各分项相加后的净收益。
            </p>
            <p>合成返佣与奖励仅为展示数据，不是收益承诺或官方费率。</p>
          </div>
        </Panel>
      </div>
    </div>
  )
}

function Heatmap({
  index,
  seek,
  snapshots,
}: {
  index: number
  seek: (n: number) => void
  snapshots: typeof dashboard.book
}) {
  const prices = [
    ...new Set(
      snapshots.flatMap((s) => [...s.bids, ...s.asks].map((l) => l.price)),
    ),
  ].sort((a, b) => b - a)
  const max = Math.max(
    ...snapshots.flatMap((s) => [...s.bids, ...s.asks].map((l) => l.size)),
  )
  return (
    <div className="p-5">
      <p className="mb-4 text-[11px] leading-5 text-muted-foreground">
        完整录制区间 ·
        颜色越亮，挂单量越大。点击任一时间列定位快照；边框标记当前时间。
      </p>
      <div className="flex gap-2">
        <div
          className="grid w-9 shrink-0 font-mono text-[9px] text-muted-foreground"
          style={{ gridTemplateRows: `repeat(${prices.length}, 14px)` }}
        >
          {prices.map((p, i) => (
            <span key={p}>{i % 3 === 0 ? p.toFixed(2) : ''}</span>
          ))}
        </div>
        <div
          className="grid min-w-0 flex-1 gap-0.5"
          style={{
            gridTemplateColumns: `repeat(${snapshots.length}, minmax(0, 1fr))`,
          }}
        >
          {snapshots.map((s, i) => (
            <button
              type="button"
              key={s.timestamp}
              aria-label={`定位 ${utcTime(s.timestamp)} UTC`}
              aria-pressed={i === index}
              onClick={() => seek(i)}
              className={`grid gap-px rounded-sm outline-offset-2 focus-visible:outline-2 focus-visible:outline-primary ${i === index ? 'ring-1 ring-primary ring-offset-2 ring-offset-card' : ''}`}
            >
              {prices.map((p) => {
                const bid = s.bids.find((l) => l.price === p),
                  ask = s.asks.find((l) => l.price === p)
                const size = bid?.size ?? ask?.size ?? 0
                return (
                  <span
                    key={p}
                    className="h-[13px] rounded-[1px]"
                    style={{
                      background: size
                        ? `rgba(${bid ? '52,211,153' : '251,113,133'},${0.12 + (0.78 * size) / max})`
                        : '#ffffff06',
                    }}
                  />
                )
              })}
            </button>
          ))}
        </div>
      </div>
      <div className="ml-11 mt-3 flex justify-between font-mono text-[10px] text-muted-foreground">
        <span>{utcTime(snapshots[0].timestamp)}</span>
        <span>{snapshots[0].timestamp.slice(0, 10)} / UTC →</span>
        <span>{utcTime(snapshots[snapshots.length - 1].timestamp)}</span>
      </div>
      <p className="mt-4 text-[11px] text-muted-foreground">
        <span className="text-emerald-400">● 买盘</span>
        <span className="ml-4 text-rose-400">● 卖盘</span>
        <span className="ml-4">每格：单价格档数量 / 份</span>
      </p>
    </div>
  )
}

export function BookView({ data = dashboard }: { data?: typeof dashboard }) {
  const [index, setIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const snapshots = data.book,
    lastIndex = snapshots.length - 1
  const snapshot = snapshots[index]
  useEffect(() => {
    if (!playing) return
    if (index >= lastIndex) {
      setPlaying(false)
      return
    }
    const timer = window.setTimeout(
      () => setIndex((i) => Math.min(lastIndex, i + 1)),
      1000 / speed,
    )
    return () => window.clearTimeout(timer)
  }, [playing, speed, index, lastIndex])
  if (!snapshot) return <p role="status">暂无盘口快照。</p>
  const asset = selectedAsset(marketFor(data), data.outcome_index)
  if (!asset) return <p role="status">协议或 outcome 身份不完整，盘口已隔离。</p>
  const outcome = asset.outcome
  const bids = depth(snapshot.bids, 'bid'),
    asks = depth(snapshot.asks, 'ask')
  function seek(next: number) {
    setPlaying(false)
    setIndex(next)
  }
  return (
    <div className="grid gap-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label={`最优买价 / ${outcome}`}
          value={bids[0]?.price.toFixed(2) ?? '—'}
          detail="pUSD / 份 · 合成 L2 快照"
          accent
        />
        <Stat
          label={`最优卖价 / ${outcome}`}
          value={asks[0]?.price.toFixed(2) ?? '—'}
          detail="pUSD / 份 · 非可执行报价"
        />
        <Stat
          label="买卖价差"
          value={
            asks.length && bids.length
              ? (asks[0].price - bids[0].price).toFixed(2)
              : '—'
          }
          detail="pUSD / 份 · ask − bid"
        />
        <Stat
          label="快照时间 / UTC"
          value={utcTime(snapshot.timestamp)}
          detail={`${snapshot.timestamp.slice(0, 10)} · ${index + 1} / ${snapshots.length} 帧`}
        />
      </div>
      <Panel title="盘口回放" note={`FROZEN / ${outcome} / L2`}>
        <div className="flex flex-wrap items-center gap-3 p-4">
          <MotionButton
            size="sm"
            disabled={index === lastIndex}
            onClick={() => setPlaying((p) => !p)}
            className="rounded-md focus-visible:outline-2 focus-visible:outline-primary"
          >
            {playing ? <Pause size={13} /> : <Play size={13} />}
            {playing ? '暂停回放' : '播放回放'}
          </MotionButton>
          <Button
            size="sm"
            variant="outline"
            disabled={playing || index === lastIndex}
            onClick={() => seek(index + 1)}
          >
            <SkipForward size={13} />
            下一帧
          </Button>
          <Button size="sm" variant="ghost" onClick={() => seek(0)}>
            <RotateCcw size={13} />
            回到起点
          </Button>
          <label className="flex items-center gap-2 text-xs">
            倍速
            <select
              aria-label="盘口回放倍速"
              className="rounded border bg-background p-2"
              value={speed}
              onChange={(e) => setSpeed(Number(e.target.value))}
            >
              <option value="1">1×</option>
              <option value="4">4×</option>
            </select>
          </label>
          <span
            className="font-mono text-[11px] text-muted-foreground"
            data-testid="book-frame"
            aria-live="polite"
          >
            {index + 1} / {snapshots.length} · {utcTime(snapshot.timestamp)} UTC
            {index === lastIndex ? ' · 回放结束' : ''}
          </span>
        </div>
        <label className="flex items-center gap-4 border-t px-5 py-4 text-xs text-muted-foreground">
          时间定位
          <input
            aria-label="盘口回放进度"
            className="min-w-0 flex-1 accent-emerald-400"
            type="range"
            min="0"
            max={lastIndex}
            value={index}
            onChange={(e) => seek(Number(e.target.value))}
          />
        </label>
      </Panel>
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="grid min-w-0 gap-5">
          <Panel
            title="L2 累计深度"
            note={utcTime(snapshot.timestamp) + ' UTC'}
          >
            <div className="p-3">
              {snapshot.bids.length && snapshot.asks.length ? (
                <DepthChart snapshot={snapshot} outcome={outcome} />
              ) : (
                <p role="status">盘口单侧或双侧为空，无法计算价差与深度。</p>
              )}
            </div>
          </Panel>
          <Panel title="流动性热力图" note="PRICE × TIME">
            <Heatmap index={index} seek={seek} snapshots={snapshots} />
          </Panel>
        </div>
        <Panel title="逐档盘口" note="SIZE / SHARES">
          <table
            className="w-full text-right font-mono text-[11px]"
            data-testid="book-levels"
          >
            <caption className="px-4 py-3 text-left font-sans text-[11px] text-muted-foreground">
              当前帧完整快照 · {utcTime(snapshot.timestamp)} UTC
            </caption>
            <thead className="text-muted-foreground">
              <tr>
                {['方向', '价格', '数量', '累计'].map((s) => (
                  <th key={s} className="px-3 py-3 font-normal">
                    {s}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[
                {
                  rows: [...asks].reverse(),
                  side: '卖',
                  color: 'text-rose-400',
                },
                { rows: bids, side: '买', color: 'text-emerald-400' },
              ].map(({ rows, side, color }) =>
                rows.map((l, i) => (
                  <tr
                    key={`${side}-${l.price}`}
                    className={`border-t ${color}`}
                  >
                    <td className="px-3 py-3">{i === 0 ? side : ''}</td>
                    <td className="px-3 py-3">{l.price.toFixed(2)}</td>
                    <td className="px-3 py-3">{l.size}</td>
                    <td className="px-3 py-3 text-muted-foreground">
                      {l.cumulative}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  )
}
