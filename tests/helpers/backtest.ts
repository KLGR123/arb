import type { Page } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import recording from '../../apps/web/src/features/backtest/fixtures/recorded-run.json'
import registry from '../../research/src/arb_research/fixtures/registry.json'

export { recording }
export const catalog = {
  datasets: registry.map(({ file: _file, ...dataset }) => dataset),
}
export async function mockBacktest(page: Page) {
  const runs = new Map<string, typeof recording.run>()
  await page.route('**/api/research/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace(
      '/api/research',
      '',
    )
    if (path === '/datasets') return route.fulfill({ json: catalog })
    if (path === '/backtests' && route.request().method() === 'POST') {
      const request = route.request().postDataJSON()
      const run = structuredClone(recording.run)
      run.run_id = randomUUID()
      run.request = request
      run.requested_engine = request.engine
      runs.set(run.run_id, run)
      return route.fulfill({
        status: 202,
        json: {
          ...run,
          status: 'queued',
          source: 'arb.backtests',
          phase: null,
          metrics: {},
          equity: [],
          fills: [],
          stages: {
            fast: { status: 'queued', error: null, result: null },
            ...(request.engine === 'realistic'
              ? { realistic: { status: 'queued', error: null, result: null } }
              : {}),
          },
        },
      })
    }
    const [, , id, suffix] = path.split('/')
    const run = runs.get(id)
    if (!run)
      return route.fulfill({ status: 404, json: { detail: 'Unknown run id' } })
    if (suffix === 'bars')
      return route.fulfill({ json: { ...recording.bars, run_id: id } })
    const result = run.stages[run.requested_engine].result
    return route.fulfill({
      json: {
        ...run,
        status: 'succeeded',
        phase: result.phase,
        source: result.source,
        metrics: result.metrics,
        equity: result.equity,
        fills: result.fills,
        stages: {
          fast: run.stages.fast,
          ...(run.requested_engine === 'realistic'
            ? { realistic: run.stages.realistic }
            : {}),
        },
      },
    })
  })
}
