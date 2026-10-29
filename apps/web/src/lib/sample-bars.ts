// Reproducible offline OHLCV. Timestamps are epoch milliseconds, as Vela expects.
export function sampleBars(count: number) {
  return Array.from({ length: count }, (_, index) => {
    const open = 100 + index * 0.12 + Math.sin(index * 0.45) * 2
    const close = open + Math.sin(index * 1.7) * 1.1
    return {
      time: Date.UTC(2026, 0, 1) + index * 3_600_000,
      open,
      high: Math.max(open, close) + 0.8,
      low: Math.min(open, close) - 0.8,
      close,
      volume: 1000 + (index % 7) * 170,
    }
  })
}
