import { z } from 'zod'

// Local ARB-7 responses, not raw Gamma/Data API payloads. No coercion: IDs must
// never pass through JS Number and null must never become a zero price/payout.
const text = z.string().min(1)
const decimalId = z
  .string()
  .regex(/^\d+$/, 'Expected a decimal asset ID string')
const time = z.iso.datetime({ offset: true })
const finite = z.number().finite()
const probability = finite.min(0).max(1)
const protocol = z.enum(['v1', 'v2', 'unknown'])
const tag = z.strictObject({
  id: text,
  label: z.string().nullable(),
  slug: z.string().nullable(),
})
export const tokenSchema = z.strictObject({
  id: decimalId,
  outcome: text,
  outcome_index: z.number().int().nonnegative(),
  protocol,
})
export const marketSchema = z.strictObject({
  id: text,
  event_id: text,
  condition_id: text,
  slug: z.string().nullable(),
  question: z.string().nullable(),
  rules: z.string().nullable(),
  resolution_source: z.string().nullable(),
  closed: z.boolean().nullable(),
  protocol,
  neg_risk: z.boolean().nullable(),
  neg_risk_group: z.string().nullable(),
  tags: z.array(tag),
  tokens: z.array(tokenSchema),
})
export const eventSchema = z
  .strictObject({
    id: text,
    slug: z.string().nullable(),
    title: z.string().nullable(),
    description: z.string().nullable(),
    closed: z.boolean().nullable(),
    tags: z.array(tag),
    markets: z.array(marketSchema),
  })
  .superRefine((event, ctx) => {
    if (
      event.markets.some((m) => m.event_id !== event.id) ||
      new Set(event.markets.map((m) => m.id)).size !== event.markets.length
    )
      ctx.addIssue({
        code: 'custom',
        message: 'Inconsistent event/market identity',
      })
  })
export const priceRowSchema = z.strictObject({
  event_id: text,
  market_id: text,
  // ARB-7's token_id is the selected protocol's asset ID, including V2 positions.
  token_id: decimalId,
  timestamp: z.number().int().positive().max(253402300799), // Unix seconds, through year 9999
  price: probability,
  resolution_seconds: z.number().int().nonnegative(),
})
export const historySchema = z.strictObject({
  data: z.array(priceRowSchema),
  next_offset: z.number().int().nonnegative().nullable(), // local API offset, NOT upstream cursor
})
export const resolutionSchema = z.strictObject({
  market_id: text,
  condition_id: text,
  status: text, // preserve new lifecycle values; only "resolved" implies finality
  payouts: z.array(probability).nullable(),
  split_50_50: z.boolean(),
  was_disputed: z.boolean(),
  extended_review: z.boolean(),
  proposed_price: z.string().nullable(), // raw numeric string; NOT pUSD/share
  reproposed_price: z.string().nullable(),
  price: z.string().nullable(),
  resolved_at: time.nullable(),
  last_updated_at: time,
  raw: z.record(z.string(), z.unknown()),
})
export const resolutionResultSchema = z.strictObject({
  data: z.array(resolutionSchema),
  missing_market_ids: z.array(text),
})
export const complianceSchema = z.strictObject({
  trading_enabled: z.boolean(),
  trading_allowed: z.literal(false),
  allowlist_configured: z.literal(false),
  mode: z.literal('read_only'),
  geoblock: z.strictObject({
    blocked: z.boolean(),
    country: z.string().nullable(),
    region: z.string().nullable(),
    checked_at: time,
    error: z.string().nullable(),
    cached: z.boolean(),
  }),
})

export const components = [
  'trading',
  'rebates',
  'rewards',
  'settlement',
  'fees',
] as const
export type Component = (typeof components)[number]
const forecastSchema = z
  .strictObject({
    market_id: text,
    predicted_at: time,
    resolved_at: time,
    p: probability,
    q: probability,
    outcome: z.union([z.literal(0), z.literal(1)]),
  })
  .refine(
    (f) => Date.parse(f.predicted_at) < Date.parse(f.resolved_at),
    'Prediction must precede resolution',
  )
const pnlSchema = z.strictObject({
  timestamp: time,
  benchmark: finite,
  trading: finite,
  rebates: finite,
  rewards: finite,
  settlement: finite,
  fees: finite.max(0),
})
const levelSchema = z.strictObject({
  price: probability,
  size: finite.positive(),
})
const bookSchema = z
  .strictObject({
    timestamp: time,
    bids: z.array(levelSchema),
    asks: z.array(levelSchema),
  })
  .superRefine((book, ctx) => {
    for (const side of [book.bids, book.asks]) {
      if (new Set(side.map((l) => l.price)).size !== side.length)
        ctx.addIssue({ code: 'custom', message: 'Duplicate book price level' })
    }
    if (
      book.bids.length &&
      book.asks.length &&
      Math.max(...book.bids.map((l) => l.price)) >=
        Math.min(...book.asks.map((l) => l.price))
    )
      ctx.addIssue({ code: 'custom', message: 'Crossed book snapshot' })
  })

// Display contract v2 composes backend objects without renaming their fields.
// Freshness metadata is UI-owned: ARB-7 does not yet expose collection times.
export const dashboardSchema = z
  .strictObject({
    schema_version: z.literal(2),
    source: z.literal('synthetic-fixture'),
    as_of: time,
    currency: z.literal('pUSD'),
    event: eventSchema,
    market_id: text,
    outcome_index: z.number().int().nonnegative(),
    prices: historySchema,
    resolutions: resolutionResultSchema,
    compliance: complianceSchema.nullable(),
    quality: z.strictObject({
      collected_at: time.nullable(),
      stale_after_seconds: z.number().int().positive(),
      expected_price_interval_seconds: z.number().int().positive().nullable(),
    }),
    forecasts: z.array(forecastSchema),
    pnl: z.array(pnlSchema),
    book: z.array(bookSchema),
  })
  .superRefine((data, ctx) => {
    const market = data.event.markets.find((m) => m.id === data.market_id)
    if (!market) {
      ctx.addIssue({
        code: 'custom',
        message: 'Selected market is absent from event',
      })
      return
    }
    if (
      data.prices.data.some(
        (p) =>
          p.event_id !== data.event.id ||
          p.market_id !== market.id ||
          !market.tokens.some((t) => t.id === p.token_id),
      )
    )
      ctx.addIssue({
        code: 'custom',
        message: 'Price identity does not match selected market',
      })
    const resolutions = data.resolutions
    if (
      resolutions.data.some(
        (r) =>
          r.market_id !== market.id || r.condition_id !== market.condition_id,
      ) ||
      resolutions.missing_market_ids.some((id) => id !== market.id) ||
      resolutions.data.length > 1 ||
      (resolutions.data.length > 0 &&
        resolutions.missing_market_ids.includes(market.id))
    )
      ctx.addIssue({
        code: 'custom',
        message: 'Inconsistent selected-market resolutions',
      })
    for (const rows of [data.pnl, data.book]) {
      if (
        rows.some(
          (row, i) =>
            i > 0 &&
            Date.parse(row.timestamp) <= Date.parse(rows[i - 1].timestamp),
        )
      )
        ctx.addIssue({
          code: 'custom',
          message: 'Chart times must be strictly increasing',
        })
    }
  })

export type Market = z.infer<typeof marketSchema>
export type Event = z.infer<typeof eventSchema>
export type PriceRow = z.infer<typeof priceRowSchema>
export type Resolution = z.infer<typeof resolutionSchema>
export type Compliance = z.infer<typeof complianceSchema>
export type Forecast = z.infer<typeof forecastSchema>
export type PnlPoint = z.infer<typeof pnlSchema>
export type Level = z.infer<typeof levelSchema>
export type BookSnapshot = z.infer<typeof bookSchema>
export type Dashboard = z.infer<typeof dashboardSchema>

export function selectedAsset(market: Market, outcomeIndex: number) {
  if (market.protocol === 'unknown') return null
  const tokens = market.tokens.filter((t) => t.protocol === market.protocol)
  // Historical CTF IDs may coexist with positions. Never fall back across ledgers.
  if (
    !tokens.length ||
    new Set(tokens.map((t) => t.id)).size !== tokens.length ||
    new Set(tokens.map((t) => t.outcome_index)).size !== tokens.length ||
    new Set(tokens.map((t) => t.outcome.toLowerCase())).size !== tokens.length
  )
    return null
  return tokens.find((t) => t.outcome_index === outcomeIndex) ?? null
}

export function marketFor(data: Dashboard) {
  return data.event.markets.find((m) => m.id === data.market_id)!
}

export function latestPrice(data: Dashboard) {
  const asset = selectedAsset(marketFor(data), data.outcome_index)
  if (!asset) return null
  return data.prices.data
    .filter((p) => p.token_id === asset.id)
    .reduce<PriceRow | null>(
      (latest, p) => (!latest || p.timestamp > latest.timestamp ? p : latest),
      null,
    )
}

export function qualityMessages(data: Dashboard, referenceTime = data.as_of) {
  const messages: string[] = []
  const reference = Date.parse(referenceTime)
  const collected = data.quality.collected_at
  if (!collected) messages.push('采集时间未知，无法判断新鲜度。')
  else if (Date.parse(collected) > reference)
    messages.push('采集时间晚于快照时间，请核对时钟。')
  else if (
    reference - Date.parse(collected) >
    data.quality.stale_after_seconds * 1000
  )
    messages.push('数据已过期（stale），仅供历史研究。')
  if (!selectedAsset(marketFor(data), data.outcome_index))
    messages.push('协议或 outcome 身份不完整，已隔离资产，无法展示价格。')
  const price = latestPrice(data)
  if (!price) messages.push('价格缺失，显示 —，不按零处理。')
  else if (
    data.quality.expected_price_interval_seconds !== null &&
    reference / 1000 - price.timestamp - price.resolution_seconds >
      data.quality.expected_price_interval_seconds
  )
    messages.push('价格更新延迟；历史价格桶不是实时可执行报价。')
  if (data.prices.next_offset !== null)
    messages.push('价格分页未读完，当前数据不完整。')
  if (!data.resolutions.data.length)
    messages.push('结算数据缺失；closed 不代表已结算。')
  const geo = data.compliance?.geoblock
  if (!geo || geo.error) messages.push('地区检查未提供或不可用，交易保持拒绝。')
  else if (geo.blocked)
    messages.push('地区限制（geoblock）：公共数据只读，交易保持拒绝。')
  else messages.push('地区检查未受限；当前仍为只读，交易保持拒绝。')
  return messages
}
