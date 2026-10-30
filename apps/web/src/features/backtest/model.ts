import type { Dataset } from './api'

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
  datasetId: 'demo-synthetic-1h-v1',
  engine: 'fast',
  fastWindow: 10,
  slowWindow: 30,
  feeBps: 10,
  slippageBps: 5,
  start: '2026-01-01',
  end: '2026-01-31',
  initialCapital: 10000,
}

export function validateConfig(
  value: unknown,
  datasets: Dataset[],
): string | undefined {
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
  if (c.slippageBps >= 10000) return '滑点必须小于 10000 bps。'
  if (
    typeof c.initialCapital !== 'number' ||
    !Number.isFinite(c.initialCapital) ||
    c.initialCapital < 0.01 ||
    c.initialCapital > 1e12
  )
    return '初始资金需至少 0.01 USD 且不超过 1 万亿。'
  if (!/^\d+(?:\.\d{1,2})?$/.test(String(c.initialCapital)))
    return '初始资金必须为整分（0.01 USD）。'
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

// ARB-4 request contract; the API remains authoritative for validation.
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

export function toRequest(
  config: BacktestConfig,
  datasets: Dataset[],
): CreateBacktestRequest {
  const error = validateConfig(config, datasets)
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

export const presetKey = 'arb.backtest.preset.v1'

export function parsePreset(raw: string, datasets: Dataset[]): BacktestConfig {
  const envelope = JSON.parse(raw)
  if (envelope?.version !== 1) throw new Error('预设版本不支持。')
  const error = validateConfig(envelope.config, datasets)
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
