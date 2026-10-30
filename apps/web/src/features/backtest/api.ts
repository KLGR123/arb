import { z } from 'zod'
import type { CreateBacktestRequest } from './model'

const time = z.iso.datetime({ offset: true })
const finite = z.number().finite()
const engine = z.enum(['fast', 'realistic'])
const source = z.object({
  package: z.string(),
  version: z.string(),
  adapter: z.string(),
})
export const datasetSchema = z
  .object({
    id: z.string().min(1),
    status: z.literal('published'),
    frozen: z.literal(true),
    source: z.string(),
    as_of: time,
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
    start: time,
    end: time,
    bar_count: z.number().int().positive(),
    interval: z.literal('1h'),
    currency: z.literal('USD'),
    symbol: z.string(),
  })
  .transform((d) => ({
    ...d,
    start: d.start.slice(0, 10),
    end: d.end.slice(0, 10),
  }))
export type Dataset = z.infer<typeof datasetSchema>
const metricsSchema = z.object({
  initial_capital: finite,
  final_equity: finite,
  total_return: finite,
  max_drawdown: finite,
  fill_count: z.number().int().nonnegative(),
  fees_paid: finite,
})
const equitySchema = z.array(z.object({ timestamp: time, value: finite }))
const fillsSchema = z.array(
  z.object({
    timestamp: time,
    side: z.enum(['buy', 'sell']),
    quantity: finite.nonnegative(),
    price: finite.positive(),
    fee: finite.nonnegative(),
    currency: z.literal('USD'),
  }),
)
const resultSchema = z.object({
  engine,
  phase: z.enum(['draft', 'final']),
  source,
  as_of: time,
  computed_at: time,
  metrics: metricsSchema,
  equity: equitySchema,
  fills: fillsSchema,
})
export type BacktestResult = z.infer<typeof resultSchema>
const stageSchema = z.object({
  status: z.enum(['queued', 'running', 'succeeded', 'failed']),
  error: z.string().nullable(),
  result: resultSchema.nullable(),
})
const requestSchema = z.object({
  dataset_id: z.string(),
  engine,
  strategy: z.object({
    id: z.literal('sma_cross'),
    parameters: z.object({ fast_window: finite, slow_window: finite }),
  }),
  execution: z.object({
    fee_bps: finite,
    slippage_bps: finite,
    fill_policy: z.literal('next_bar_open'),
  }),
  start: time,
  end: time,
  initial_capital: finite,
  currency: z.literal('USD'),
})
export const runSchema = z.object({
  run_id: z.uuid(),
  created_at: time,
  request: requestSchema,
  requested_engine: engine,
  status: z.enum([
    'queued',
    'running',
    'draft',
    'succeeded',
    'partial',
    'failed',
  ]),
  dataset: datasetSchema,
  source: z.union([z.literal('arb.backtests'), source]),
  as_of: time,
  phase: z.enum(['draft', 'final']).nullable(),
  metrics: z.union([metricsSchema, z.object({}).strict()]),
  equity: equitySchema,
  fills: fillsSchema,
  stages: z.object({ fast: stageSchema, realistic: stageSchema.optional() }),
})
export type BacktestRun = z.infer<typeof runSchema>
export const barsSchema = z.object({
  run_id: z.uuid(),
  source: z.string(),
  as_of: time,
  sha256: z.string(),
  bars: z.array(
    z.object({
      timestamp: time,
      close_timestamp: time,
      open: finite.positive(),
      high: finite.positive(),
      low: finite.positive(),
      close: finite.positive(),
      volume: finite.nonnegative(),
    }),
  ),
})
export type BacktestBars = z.infer<typeof barsSchema>

// Same-origin path is proxied only to the local research API. No live provider.
const base = '/api/research'
async function read<T>(
  path: string,
  schema: z.ZodType<T>,
  signal: AbortSignal,
  body?: CreateBacktestRequest,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${base}${path}`, {
      signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]),
      cache: 'no-store',
      ...(body
        ? {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          }
        : {}),
    })
  } catch (cause) {
    if (signal.aborted) throw cause
    throw new Error(
      body
        ? '创建请求未确认：API 不可达或超时。任务可能已入队，请先检查服务端记录，再决定是否重试。'
        : '回测 API 不可达或超时，请检查本地 FastAPI 与代理配置。',
    )
  }
  if (!response.ok) {
    if (response.status === 422) {
      const payload = await response.json().catch(() => null)
      const detail =
        typeof payload?.detail === 'string'
          ? payload.detail
          : Array.isArray(payload?.detail)
            ? payload.detail.map((e: { msg?: string }) => e.msg).join('；')
            : ''
      throw new Error(`API 校验失败（422）${detail ? `：${detail}` : '。'}`)
    }
    throw new Error(
      `回测 API 请求失败（${response.status}），请检查服务是否可用。`,
    )
  }
  const parsed = schema.safeParse(await response.json().catch(() => null))
  if (!parsed.success)
    throw new Error('回测 API 响应格式无效，未显示替代数据。')
  return parsed.data
}
export const getDatasets = (signal: AbortSignal) =>
  read('/datasets', z.object({ datasets: z.array(datasetSchema) }), signal)
export const createRun = (body: CreateBacktestRequest, signal: AbortSignal) =>
  read('/backtests', runSchema, signal, body)
export const getRun = (id: string, signal: AbortSignal) =>
  read(`/backtests/${encodeURIComponent(id)}`, runSchema, signal)
export const getBars = (id: string, signal: AbortSignal) =>
  read(`/backtests/${encodeURIComponent(id)}/bars`, barsSchema, signal)
export const isTerminal = (run: BacktestRun) =>
  ['succeeded', 'partial', 'failed'].includes(run.status)
export function selectedResult(run: BacktestRun): BacktestResult | undefined {
  return run.stages.realistic?.result ?? run.stages.fast.result ?? undefined
}
