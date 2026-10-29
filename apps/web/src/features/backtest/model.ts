// UI-owned frozen fixtures. These IDs are published only in this demo catalog;
// a production registry must replace the catalog before enabling real runs.
export const datasets = [
  {
    id: 'demo-synthetic-1h-v1',
    name: 'Synthetic · 1H',
    status: 'published',
    frozen: true,
    start: '2026-01-01',
    end: '2026-01-31',
    bars: 744,
    source: 'synthetic fixture',
    as_of: '2026-02-01T00:00:00Z',
  },
  {
    id: 'demo-synthetic-1h-v2',
    name: 'Synthetic · 1H / short',
    status: 'published',
    frozen: true,
    start: '2026-01-01',
    end: '2026-01-14',
    bars: 336,
    source: 'synthetic fixture',
    as_of: '2026-02-01T00:00:00Z',
  },
] as const

export type Engine = 'fast' | 'realistic'
export type BacktestConfig = {
  datasetId: string
  engine: Engine
  fastWindow: number
  slowWindow: number
  feeBps: number
  slippageBps: number
  start: string
  end: string
  initialCapital: number
}

export const defaultConfig: BacktestConfig = {
  datasetId: datasets[0].id,
  engine: 'fast',
  fastWindow: 10,
  slowWindow: 30,
  feeBps: 10,
  slippageBps: 5,
  start: datasets[0].start,
  end: datasets[0].end,
  initialCapital: 10000,
}

export function validateConfig(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return '配置格式无效。'
  const c = value as BacktestConfig
  const dataset = datasets.find(
    (d) => d.id === c.datasetId && d.status === 'published' && d.frozen,
  )
  if (!dataset) return '请选择目录中已发布并冻结的 Dataset id。'
  if (c.engine !== 'fast' && c.engine !== 'realistic')
    return '引擎必须为 fast 或 realistic。'
  if (
    ![c.fastWindow, c.slowWindow].every(
      (n) => Number.isInteger(n) && n >= 1 && n <= 1000,
    ) ||
    c.fastWindow >= c.slowWindow
  )
    return '均线窗口需为 1–1000 的整数，且快线小于慢线。'
  if (
    ![c.feeBps, c.slippageBps].every(
      (n) =>
        typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 10000,
    )
  )
    return '费用与滑点需在 0–10000 bps 之间。'
  if (
    typeof c.initialCapital !== 'number' ||
    !Number.isFinite(c.initialCapital) ||
    c.initialCapital <= 0 ||
    c.initialCapital > 1e12
  )
    return '初始资金需大于 0 且不超过 1 万亿。'
  if (
    ![c.start, c.end].every(
      (date) =>
        typeof date === 'string' &&
        /^\d{4}-\d{2}-\d{2}$/.test(date) &&
        Number.isFinite(Date.parse(date)) &&
        new Date(date).toISOString().slice(0, 10) === date,
    )
  )
    return '请选择有效日期。'
  if (c.start > c.end || c.start < dataset.start || c.end > dataset.end)
    return '区间需在 Dataset 覆盖范围内，且开始不晚于结束。'
}

// This is a UI adapter seam, not an HTTP route or a job implementation.
export type CreateBacktestRequest = {
  dataset_id: string
  engine: Engine
  strategy: {
    id: 'sma_cross'
    parameters: { fast_window: number; slow_window: number }
  }
  execution: {
    fee_bps: number
    slippage_bps: number
    fill_policy: 'next_bar_open'
  }
  start: string
  end: string
  initial_capital: number
  currency: 'USD'
}

export type BacktestRun = {
  run_id: string
  status: 'queued'
  source: 'ui_stub'
  as_of: string
  request: CreateBacktestRequest
  metrics: Record<string, number>
  equity: { timestamp: string; value: number }[]
  fills: { timestamp: string; side: string; quantity: number; price: number }[]
}

export function toRequest(config: BacktestConfig): CreateBacktestRequest {
  const error = validateConfig(config)
  if (error) throw new Error(error)
  return {
    dataset_id: config.datasetId,
    engine: config.engine,
    strategy: {
      id: 'sma_cross',
      parameters: {
        fast_window: config.fastWindow,
        slow_window: config.slowWindow,
      },
    },
    execution: {
      fee_bps: config.feeBps,
      slippage_bps: config.slippageBps,
      fill_policy: 'next_bar_open',
    },
    start: `${config.start}T00:00:00Z`,
    end: `${config.end}T23:59:59Z`,
    initial_capital: config.initialCapital,
    currency: 'USD',
  }
}

export async function createStubRun(
  config: BacktestConfig,
): Promise<BacktestRun> {
  const request = toRequest(config)
  return {
    run_id: `stub-${crypto.randomUUID()}`,
    status: 'queued',
    source: 'ui_stub',
    as_of: new Date().toISOString(),
    request,
    metrics: {},
    equity: [],
    fills: [],
  }
}

export const presetKey = 'arb.backtest.preset.v1'

export function parsePreset(raw: string): BacktestConfig {
  const envelope = JSON.parse(raw)
  if (envelope?.version !== 1) throw new Error('预设版本不支持。')
  const error = validateConfig(envelope.config)
  if (error) throw new Error(error)
  // Copy only known fields: never carry arbitrary persisted data into a run.
  const c = envelope.config as BacktestConfig
  return {
    datasetId: c.datasetId,
    engine: c.engine,
    fastWindow: c.fastWindow,
    slowWindow: c.slowWindow,
    feeBps: c.feeBps,
    slippageBps: c.slippageBps,
    start: c.start,
    end: c.end,
    initialCapital: c.initialCapital,
  }
}
