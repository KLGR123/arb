import recording from './fixtures/recorded-run.json'
import type { Engine } from './model'

// Unmodified GET /backtests/:id and /bars responses from a real two-engine run.
export { recording }
export const bars = recording.bars.bars
export type RecordedResult = typeof recording.run.stages.fast.result
export function resultFor(engine: Engine): RecordedResult {
  return recording.run.stages[engine].result
}

// Reveal whole closed bars. Fills occur at open; equity belongs to close_timestamp.
export function replayFrame(result: RecordedResult, count: number) {
  let quantity = 0
  let average = 0
  const fills = result.fills.filter(
    (f) => Date.parse(f.timestamp) <= Date.parse(bars[count - 1].timestamp),
  )
  const positions = bars.slice(0, count).map((bar) => {
    for (const fill of fills.filter((f) => f.timestamp === bar.timestamp)) {
      if (fill.side === 'buy') {
        average =
          (average * quantity + fill.price * fill.quantity) /
          (quantity + fill.quantity)
        quantity += fill.quantity
      } else {
        quantity = Math.max(0, quantity - fill.quantity)
        if (quantity < 1e-6) {
          quantity = 0
          average = 0
        }
      }
    }
    return {
      time: Date.parse(bar.timestamp),
      quantity,
      average: quantity ? average : null,
    }
  })
  const values = new Map(
    result.equity.map((e) => [Date.parse(e.timestamp), e.value]),
  )
  const equity = bars.slice(0, count).map((bar) => ({
    time: Date.parse(bar.timestamp),
    value: values.get(Date.parse(bar.close_timestamp)) ?? null,
  }))
  return { fills, positions, equity, quantity, average }
}
