import { useEffect, useRef, useState } from 'react'
import { sampleBars } from '@/lib/sample-bars'

export function SampleChart({ count }: { count: number }) {
  const container = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    let disposed = false
    let chart: import('@luxalgo/vela').Vela | undefined
    setStatus('loading')

    async function mount() {
      try {
        // Vela touches browser APIs. Import it only after React mounts, never in SSR.
        const { Vela } = await import('@luxalgo/vela')
        if (disposed || !container.current) return
        chart = new Vela(container.current, {
          data: sampleBars(count),
          timeframe: '1h',
          theme: 'dark',
          live: false,
        })
        await chart.ready()
        if (!disposed) setStatus('ready')
      } catch (error) {
        if (!disposed) {
          console.error('Vela chart initialization failed', error)
          setStatus('error')
        }
      }
    }
    void mount()
    return () => {
      disposed = true
      chart?.destroy()
    }
  }, [count])

  return (
    <div className="relative">
      <div ref={container} role="img" aria-label={`${count} 根合成小时 K 线，非真实行情`}
        data-testid="vela-chart" data-status={status} className="h-[380px] w-full sm:h-[460px]" />
      <p role="status" className={status === 'ready' ? 'sr-only' : 'px-4 py-2 text-sm text-muted-foreground'}>
        {status === 'error' ? '图表加载失败，请刷新页面重试。' : status === 'ready' ? '示例图表已就绪' : '正在加载示例图表…'}
      </p>
    </div>
  )
}
