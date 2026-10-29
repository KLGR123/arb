import fixture from './fixtures/dashboard.json'

// Frontend draft contract; adapters for ARB-7/8/11/12 are documented separately.
export interface Forecast {
  market_id: string
  predicted_at: string
  resolved_at: string
  p: number
  q: number
  outcome: 0 | 1
}
export const components = [
  'trading',
  'rebates',
  'rewards',
  'settlement',
  'fees',
] as const
export type Component = (typeof components)[number]
export type PnlPoint = Record<Component, number> & {
  timestamp: string
  benchmark: number
}
export interface Level {
  price: number
  size: number
}
export interface BookSnapshot {
  timestamp: string
  bids: Level[]
  asks: Level[]
}
export interface Dashboard {
  schema_version: 1
  source: 'synthetic-fixture'
  as_of: string
  currency: 'pUSD'
  market: {
    event_id: string
    market_id: string
    token_id: string
    question: string
    outcome: string
    protocol_version: 'v2'
    neg_risk: boolean
  }
  forecasts: Forecast[]
  pnl: PnlPoint[]
  book: BookSnapshot[]
}
// JSON imports widen literals; keeping the contract check explicit avoids a cast.
export const dashboard: Dashboard = {
  ...fixture,
  schema_version: 1,
  source: 'synthetic-fixture',
  currency: 'pUSD',
  market: { ...fixture.market, protocol_version: 'v2' },
  forecasts: fixture.forecasts.map((f) => {
    if (f.outcome !== 0 && f.outcome !== 1)
      throw new Error('Expected binary outcome')
    return { ...f, outcome: f.outcome }
  }),
}

export const labels: Record<Component, string> = {
  trading: '交易',
  rebates: '返佣',
  rewards: '奖励',
  settlement: '结算跳变',
  fees: '费用',
}
export const colors: Record<Component, string> = {
  trading: '#34d399',
  rebates: '#38bdf8',
  rewards: '#a78bfa',
  settlement: '#fbbf24',
  fees: '#fb7185',
}
export function totalPnl(point: PnlPoint) {
  return components.reduce((sum, key) => sum + point[key], 0)
}
export function pnlLayers(points: PnlPoint[]) {
  const up = points.map(() => 0),
    down = points.map(() => 0)
  return components.flatMap((key) =>
    (['positive', 'negative'] as const).map((sign) => {
      const base = [...(sign === 'positive' ? up : down)]
      const top = points.map((p, i) =>
        sign === 'positive'
          ? (up[i] += Math.max(0, p[key]))
          : (down[i] += Math.min(0, p[key])),
      )
      return { key, sign, base, top }
    }),
  )
}
export function calibration(forecasts: Forecast[]) {
  return Array.from({ length: 10 }, (_, index) => {
    const rows = forecasts.filter(
      (f) => Math.min(9, Math.floor(f.p * 10)) === index,
    )
    return {
      lower: index / 10,
      upper: (index + 1) / 10,
      count: rows.length,
      predicted: rows.length
        ? rows.reduce((s, f) => s + f.p, 0) / rows.length
        : null,
      actual: rows.length
        ? rows.reduce((s, f) => s + f.outcome, 0) / rows.length
        : null,
    }
  })
}
export function brier(forecasts: Forecast[], key: 'p' | 'q' = 'p') {
  return forecasts.length
    ? forecasts.reduce((sum, f) => sum + (f[key] - f.outcome) ** 2, 0) /
        forecasts.length
    : null
}
export function depth(levels: Level[], side: 'bid' | 'ask') {
  let cumulative = 0
  return [...levels]
    .sort((a, b) => (side === 'bid' ? b.price - a.price : a.price - b.price))
    .map((level) => ({ ...level, cumulative: (cumulative += level.size) }))
}
export const percent = (n: number) => `${(n * 100).toFixed(0)}%`
export const money = (n: number, digits = 2) =>
  `${n >= 0 ? '+' : '−'}${Math.abs(n).toFixed(digits)}`
export const utcTime = (s: string) => s.slice(11, 19)
