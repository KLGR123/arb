import type { BacktestBars, BacktestResult } from './api'

// Reveal whole closed bars. Fills occur at open; equity belongs to close_timestamp.
export function replayFrame(
  result: BacktestResult,
  count: number,
  frameBars: BacktestBars['bars'],
) {
  let quantity = 0
  let average = 0
  const fills = result.fills.filter(
    (f) =>
      Date.parse(f.timestamp) <= Date.parse(frameBars[count - 1].timestamp),
  )
  const positions = frameBars.slice(0, count).map((bar) => {
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
  const equity = frameBars.slice(0, count).map((bar) => ({
    time: Date.parse(bar.timestamp),
    value: values.get(Date.parse(bar.close_timestamp)) ?? null,
  }))
  return { fills, positions, equity, quantity, average }
}
