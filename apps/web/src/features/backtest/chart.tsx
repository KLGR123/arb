import { useEffect, useRef, useState } from 'react'
import { ChartNoAxesCombined } from 'lucide-react'

export function BacktestChart() {
  const container = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState('loading')
  useEffect(() => {
    let disposed = false
    let chart: import('@luxalgo/vela').Vela | undefined
    async function mount() {
      try {
        const { Vela } = await import('@luxalgo/vela')
        if (disposed || !container.current) return
        // Vela 0.9 treats an empty data array as a provider lookup. Supply an
        // explicit empty offline feed so ready() resolves without a provider.
        chart = new Vela(
          container.current,
          { data: [], timeframe: '1h', theme: 'dark', live: false },
          {
            dataFeed: { load: async () => [], subscribe: () => () => {} },
          },
        )
        await chart.ready()
        if (!disposed) setStatus('ready')
      } catch {
        if (!disposed) setStatus('error')
      }
    }
    void mount()
    return () => {
      disposed = true
      chart?.destroy()
    }
  }, [])
  return (
    <div className="relative overflow-hidden">
      <div
        ref={container}
        data-testid="backtest-chart"
        data-status={status}
        aria-label="回测 Vela 图表空态"
        role="img"
        className="h-[400px] w-full xl:h-[510px]"
      />
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6">
        <div className="max-w-xs rounded-xl border bg-card/95 px-7 py-6 text-center shadow-lg">
          <ChartNoAxesCombined className="mx-auto mb-4 size-7 text-primary" />
          <p className="text-sm font-medium">
            {status === 'error' ? '图表暂时不可用' : '为下一次验证，留出空间。'}
          </p>
          <p className="mt-2 text-xs leading-6 text-muted-foreground">
            {status === 'error'
              ? '请刷新页面重试。配置与预设仍可使用。'
              : '当前为 Vela 空态。回放与交易标注将在接入结果后显示。'}
          </p>
        </div>
      </div>
    </div>
  )
}
