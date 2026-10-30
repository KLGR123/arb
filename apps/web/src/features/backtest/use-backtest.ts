import { useEffect, useRef, useState } from 'react'
import {
  createRun,
  getBars,
  getDatasets,
  getRun,
  isTerminal,
  type BacktestBars,
  type BacktestRun,
  type Dataset,
} from './api'
import { toRequest, type BacktestConfig } from './model'

export function useBacktest() {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [catalogError, setCatalogError] = useState('')
  const [catalogVersion, setCatalogVersion] = useState(0)
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [run, setRun] = useState<BacktestRun>()
  const [snapshot, setSnapshot] = useState<BacktestBars>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const active = useRef<AbortController | null>(null)
  const posting = useRef(false)
  useEffect(() => () => active.current?.abort(), [])
  useEffect(() => {
    const controller = new AbortController()
    setCatalogLoading(true)
    setCatalogError('')
    getDatasets(controller.signal)
      .then(({ datasets }) => {
        if (controller.signal.aborted) return
        setDatasets(datasets)
        if (!datasets.length)
          setCatalogError('没有可用的已发布 frozen Dataset。')
      })
      .catch((cause: Error) => {
        if (!controller.signal.aborted) setCatalogError(cause.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setCatalogLoading(false)
      })
    return () => controller.abort()
  }, [catalogVersion])

  const id = run?.run_id
  useEffect(() => {
    if (!id) return
    const controller = new AbortController()
    active.current = controller
    let timer: ReturnType<typeof setTimeout> | undefined
    setError('')
    async function poll() {
      try {
        const next = await getRun(id!, controller.signal)
        if (controller.signal.aborted) return
        if (next.run_id !== id) throw new Error('API 返回的 run id 不匹配。')
        setRun((previous) =>
          JSON.stringify(previous) === JSON.stringify(next) ? previous : next,
        )
        if (!isTerminal(next)) timer = setTimeout(() => void poll(), 1000)
      } catch (cause) {
        if (!controller.signal.aborted) setError((cause as Error).message)
      }
    }
    async function load() {
      try {
        const bars = await getBars(id!, controller.signal)
        if (controller.signal.aborted) return
        if (bars.run_id !== id)
          throw new Error('API 返回的 bars run id 不匹配。')
        setSnapshot(bars)
        await poll()
      } catch (cause) {
        if (!controller.signal.aborted) setError((cause as Error).message)
      }
    }
    void load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [id, retry])

  async function submit(config: BacktestConfig) {
    if (posting.current) return false
    posting.current = true
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setRun(undefined)
    setSnapshot(undefined)
    setError('')
    try {
      const created = await createRun(
        toRequest(config, datasets),
        controller.signal,
      )
      if (controller.signal.aborted) return false
      setRun(created)
      return true
    } catch (cause) {
      if (!controller.signal.aborted) setError((cause as Error).message)
      return false
    } finally {
      posting.current = false
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  function clear() {
    active.current?.abort()
    setRun(undefined)
    setSnapshot(undefined)
    setError('')
  }
  return {
    datasets,
    catalogLoading,
    catalogError,
    reloadCatalog: () => setCatalogVersion((v) => v + 1),
    run,
    snapshot,
    busy,
    error,
    submit,
    clear,
    refresh: () => setRetry((v) => v + 1),
  }
}
