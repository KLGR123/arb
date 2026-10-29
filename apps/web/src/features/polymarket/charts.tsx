import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Vela } from '@luxalgo/vela'
import {
  calibration,
  colors,
  components,
  depth,
  labels,
  percent,
  pnlLayers,
  totalPnl,
  type BookSnapshot,
  type Forecast,
  type PnlPoint,
} from './model'

// Probability and price-depth axes are numeric, not time axes. Use SVG for these;
// Vela owns the PnL time series below.
const width = 720,
  height = 330,
  left = 58,
  right = 690,
  top = 22,
  bottom = 280
function Plot({ label, children }: { label: string; children: ReactNode }) {
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label}
      className="w-full text-[20px] text-muted-foreground sm:text-[11px]"
    >
      {children}
    </svg>
  )
}
function Grid({
  min = 0,
  max = 1,
  format = percent,
}: {
  min?: number
  max?: number
  format?: (n: number) => string
}) {
  return (
    <>
      {Array.from({ length: 6 }, (_, i) => {
        const y = bottom - (i * (bottom - top)) / 5
        return (
          <g key={i}>
            <line
              x1={left}
              x2={right}
              y1={y}
              y2={y}
              stroke="currentColor"
              opacity=".15"
            />
            <text x={left - 12} y={y + 4} fill="currentColor" textAnchor="end">
              {format(min + ((max - min) * i) / 5)}
            </text>
          </g>
        )
      })}
    </>
  )
}
export function CalibrationChart({ forecasts }: { forecasts: Forecast[] }) {
  const bins = calibration(forecasts).filter((b) => b.count)
  const x = (p: number) => left + p * (right - left)
  const y = (p: number) => bottom - p * (bottom - top)
  return (
    <Plot label="校准曲线：横轴预测概率，纵轴实际频率；虚线表示完美校准">
      <Grid />
      <path
        d={`M ${left} ${bottom} L ${right} ${top}`}
        stroke="#94a3b8"
        strokeDasharray="5 6"
        fill="none"
        opacity=".5"
      />
      <polyline
        points={bins.map((b) => `${x(b.predicted!)},${y(b.actual!)}`).join(' ')}
        stroke="#34d399"
        strokeWidth="2.5"
        fill="none"
      />
      {bins.map((b) => (
        <circle
          key={b.lower}
          cx={x(b.predicted!)}
          cy={y(b.actual!)}
          r="5"
          fill="#34d399"
          stroke="#14201d"
          strokeWidth="2"
        >
          <title>{`${percent(b.predicted!)} 预测 / ${percent(b.actual!)} 实际 / ${b.count} 题`}</title>
        </circle>
      ))}
      {[0, 0.2, 0.4, 0.6, 0.8, 1].map((n) => (
        <text
          key={n}
          x={x(n)}
          y={bottom + 24}
          fill="currentColor"
          textAnchor="middle"
        >
          {percent(n)}
        </text>
      ))}
      <text x={left} y="12" fill="currentColor">
        实际频率
      </text>
      <text x={right} y="326" fill="currentColor" textAnchor="end">
        预测概率 →
      </text>
    </Plot>
  )
}

export function PnlChart({ points }: { points: PnlPoint[] }) {
  const container = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState('loading')
  useEffect(() => {
    let disposed = false
    let chart: Vela | undefined
    let unregister: (() => void) | undefined
    async function mount() {
      try {
        const [{ Vela }, plugin] = await Promise.all([
          import('@luxalgo/vela'),
          import('@luxalgo/vela/plugin'),
        ])
        if (disposed || !container.current) return
        const type = `arb-paper-benchmark-${crypto.randomUUID()}`
        plugin.registerNativeIndicator({
          type,
          title: '基准 · 累计 pUSD',
          overlay: true,
          paneHint: 'price',
          inputsSchema: () => [],
          defaultInputs: () => ({}),
          create() {
            return {
              start(context) {
                context.emit({
                  series: [
                    {
                      id: 'benchmark',
                      title: '基准',
                      paneId: '',
                      kind: 'line',
                      points: points.map((p) => ({
                        time: Date.parse(p.timestamp),
                        value: p.benchmark,
                      })),
                      style: {
                        color: '#94a3b8',
                        width: 2,
                        lineStyle: 'dashed',
                      },
                    },
                  ],
                })
              },
              onBars() {},
              onViewport() {},
              setInputs() {},
              suspend() {},
              resume() {},
              stop() {},
            }
          },
        })
        unregister = () => plugin.unregisterNativeIndicator(type)
        const bars = points.map((p) => {
          const value = totalPnl(p)
          return {
            time: Date.parse(p.timestamp),
            open: value,
            high: value,
            low: value,
            close: value,
            volume: 0,
          }
        })
        chart = new Vela(
          container.current,
          {
            data: bars,
            timeframe: '1D',
            theme: 'dark',
            live: false,
            priceStyle: 'line',
            volume: false,
            drawings: false,
            animations: false,
          },
          {
            dataFeed: { load: async () => bars, subscribe: () => () => {} },
          },
        )
        await chart.ready()
        if (disposed) return
        chart.addNativeIndicator(type)
        chart.setVisibleRange({
          from: bars[0].time,
          to: bars[bars.length - 1].time + 86400000,
        })
        setStatus('ready')
      } catch {
        if (!disposed) setStatus('error')
      }
    }
    void mount()
    return () => {
      disposed = true
      chart?.destroy()
      unregister?.()
    }
  }, [points])
  return (
    <>
      {status === 'error' && (
        <p role="alert" className="p-4 text-sm text-destructive">
          PnL 图表加载失败，请刷新重试。数值仍可在明细表中查看。
        </p>
      )}
      <div
        ref={container}
        data-testid="polymarket-pnl-chart"
        data-status={status}
        role="img"
        aria-label="Vela 策略与基准累计 PnL，UTC 时间轴"
        className="h-[330px] w-full sm:h-[390px]"
      />
      <p className="border-t px-5 py-3 text-[11px] text-muted-foreground">
        Vela™ by LuxAlgo · UTC · 策略净收益为主线，灰色虚线为固定基准。
      </p>
    </>
  )
}

// Diverging stack: positive components accumulate above zero; costs and losses
// accumulate below zero. Net PnL is their algebraic sum, not the stack's top.
export function PnlStack({ points }: { points: PnlPoint[] }) {
  const positive = points.map((p) =>
    components.reduce((s, k) => s + Math.max(0, p[k]), 0),
  )
  const negative = points.map((p) =>
    components.reduce((s, k) => s + Math.min(0, p[k]), 0),
  )
  const min = Math.min(...negative, 0) - 10,
    max = Math.max(...positive, 1) + 10
  const x = (i: number) => left + (i / (points.length - 1)) * (right - left)
  const y = (v: number) => bottom - ((v - min) / (max - min)) * (bottom - top)
  return (
    <Plot label="PnL 分解堆叠图：正收益在零线上方，费用与结算损失在下方，白线为净收益">
      <Grid min={min} max={max} format={(n) => n.toFixed(0)} />
      {pnlLayers(points).map(({ key, sign, base, top: layerTop }) => {
        const polygon = [
          ...layerTop.map((v, i) => `${x(i)},${y(v)}`),
          ...base.map((v, i) => `${x(i)},${y(v)}`).reverse(),
        ]
        return (
          <polygon
            key={`${key}-${sign}`}
            points={polygon.join(' ')}
            fill={colors[key]}
            opacity=".65"
          >
            <title>{labels[key]}</title>
          </polygon>
        )
      })}
      <line
        x1={left}
        x2={right}
        y1={y(0)}
        y2={y(0)}
        stroke="#94a3b8"
        opacity=".6"
      />
      <polyline
        points={points.map((p, i) => `${x(i)},${y(totalPnl(p))}`).join(' ')}
        fill="none"
        stroke="#f8fafc"
        strokeWidth="2"
      />
      {[0, 5, 10, 15]
        .filter((i) => i < points.length)
        .map((i) => (
          <text
            key={i}
            x={x(i)}
            y={bottom + 24}
            fill="currentColor"
            textAnchor="middle"
          >
            {points[i].timestamp.slice(5, 10)}
          </text>
        ))}
      <text x={left} y="12" fill="currentColor">
        累计 pUSD
      </text>
      <text x={right} y="326" fill="currentColor" textAnchor="end">
        UTC →
      </text>
    </Plot>
  )
}

export function DepthChart({ snapshot }: { snapshot: BookSnapshot }) {
  const bids = depth(snapshot.bids, 'bid'),
    asks = depth(snapshot.asks, 'ask')
  const low = Math.min(...bids.map((b) => b.price)) - 0.01
  const high = Math.max(...asks.map((a) => a.price)) + 0.01
  const max =
    Math.max(
      ...bids.map((b) => b.cumulative),
      ...asks.map((a) => a.cumulative),
    ) * 1.1
  const x = (p: number) => left + ((p - low) / (high - low)) * (right - left)
  const y = (s: number) => bottom - (s / max) * (bottom - top)
  const mid = (bids[0].price + asks[0].price) / 2
  return (
    <Plot
      label={`L2 累计深度：买盘绿色，卖盘粉色，中间价 ${mid.toFixed(2)} pUSD；数量单位为份`}
    >
      <Grid max={max} format={(n) => n.toFixed(0)} />
      {[
        { levels: bids, color: '#34d399' },
        { levels: asks, color: '#fb7185' },
      ].map(({ levels, color }) => {
        let path = `M ${x(levels[0].price)} ${bottom}`
        levels.forEach((l) => {
          path += ` H ${x(l.price)} V ${y(l.cumulative)}`
        })
        return (
          <g key={color}>
            <path d={path} fill="none" stroke={color} strokeWidth="2.5" />
            <path d={`${path} V ${bottom} Z`} fill={color} opacity=".09" />
          </g>
        )
      })}
      <line
        x1={x(mid)}
        x2={x(mid)}
        y1={top}
        y2={bottom}
        stroke="#94a3b8"
        strokeDasharray="4 5"
      />
      {Array.from({ length: 5 }, (_, i) => low + ((high - low) * i) / 4).map(
        (p) => (
          <text
            key={p}
            x={x(p)}
            y={bottom + 24}
            fill="currentColor"
            textAnchor="middle"
          >
            {p.toFixed(2)}
          </text>
        ),
      )}
      <text x={left} y="12" fill="currentColor">
        累计数量 / 份
      </text>
      <text x={right} y="326" fill="currentColor" textAnchor="end">
        YES 价格 / pUSD →
      </text>
    </Plot>
  )
}
