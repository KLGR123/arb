import { expect, test } from '@playwright/test'
import { catalog, mockBacktest, recording } from './helpers/backtest'

test.beforeEach(async ({ page }) => mockBacktest(page))

test('catalog failures and non-frozen data disable submission and can retry', async ({
  page,
}) => {
  let broken = true
  await page.route('**/api/research/datasets', (route) =>
    route.fulfill({
      json: broken
        ? { datasets: [{ ...catalog.datasets[0], frozen: false }] }
        : catalog,
    }),
  )
  await page.goto('/backtest')
  await expect(page.getByRole('alert').first()).toContainText('响应格式无效', {
    timeout: 15_000,
  })
  await expect(
    page.getByRole('button', { name: '创建回测 run' }),
  ).toBeDisabled()
  broken = false
  await page.getByRole('button', { name: '重试目录' }).click()
  await expect(page.getByRole('button', { name: '创建回测 run' })).toBeEnabled()
})

test('POST 422, unreachable API and malformed responses never create a stub', async ({
  page,
}) => {
  let mode = '422'
  await page.route('**/api/research/backtests', (route) => {
    if (mode === 'offline') return route.abort()
    return route.fulfill(
      mode === '422'
        ? {
            status: 422,
            json: {
              detail:
                'Interval must contain more complete bars than slow_window',
            },
          }
        : { status: 202, json: { run_id: 'bad' } },
    )
  })
  await page.goto('/backtest')
  for (const [next, message] of [
    ['422', 'API 校验失败（422）'],
    ['offline', '创建请求未确认'],
    ['invalid', '响应格式无效'],
  ]) {
    mode = next
    await page.getByRole('button', { name: '创建回测 run' }).click()
    await expect(page.getByRole('alert')).toContainText(message)
    await expect(page.getByTestId('run-id')).toHaveCount(0)
    await expect(page.getByTestId('backtest-chart')).toHaveCount(0)
  }
})

test('queued to draft to partial preserves one run; failed and empty states are explicit', async ({
  page,
}) => {
  let mode = 'queued'
  let polls = 0
  await page.route(/\/api\/research\/backtests\/[a-f0-9-]+$/, (route) => {
    polls++
    const id = route.request().url().split('/').pop()!
    const fast = recording.run.stages.fast
    const empty = { status: 'queued', error: null, result: null }
    const hasResult = mode === 'draft' || mode === 'partial' || mode === 'empty'
    const result =
      mode === 'empty'
        ? {
            ...fast.result,
            equity: [],
            fills: [],
            metrics: { ...fast.result.metrics, fill_count: 0 },
          }
        : fast.result
    return route.fulfill({
      json: {
        ...recording.run,
        run_id: id,
        status: mode === 'empty' ? 'succeeded' : mode,
        phase: hasResult ? 'draft' : null,
        metrics: hasResult ? result.metrics : {},
        source: hasResult ? result.source : 'arb.backtests',
        equity: hasResult ? result.equity : [],
        fills: hasResult ? result.fills : [],
        stages: {
          fast: hasResult
            ? { ...fast, result }
            : mode === 'failed'
              ? { status: 'failed', error: 'engine_failed', result: null }
              : empty,
          realistic:
            mode === 'partial' || mode === 'failed'
              ? { status: 'failed', error: 'engine_failed', result: null }
              : empty,
        },
      },
    })
  })
  await page.goto('/backtest')
  await page.getByRole('button', { name: 'realistic', exact: true }).click()
  await page.getByRole('button', { name: '创建回测 run' }).click()
  const id = await page.getByTestId('run-id').textContent()
  await expect(page.getByText('realistic · queued · 等待结果')).toBeVisible()
  mode = 'draft'
  await expect(page.getByText('realistic · draft · draft')).toBeVisible()
  await expect(page.getByTestId('metric-成交数')).toHaveText('10')
  mode = 'partial'
  await expect(page.getByRole('alert')).toContainText('部分阶段失败')
  await expect(page.getByTestId('run-id')).toHaveText(id!)
  const terminalPolls = polls
  await page.waitForTimeout(1200)
  expect(polls).toBe(terminalPolls)
  mode = 'failed'
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await expect(page.getByRole('alert')).toContainText('运行失败')
  await expect(page.getByTestId('backtest-chart')).toHaveCount(0)
  mode = 'empty'
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await expect(page.getByText('运行已结束，无净值数据')).toBeVisible()
  await expect(page.getByText('本阶段无成交')).toBeVisible()
})

test('query failure keeps run id; retry only reads and leaving the route stops polling', async ({
  page,
}) => {
  let broken = true
  let reads = 0
  let posts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST') posts++
  })
  await page.route(/\/api\/research\/backtests\/[a-f0-9-]+$/, (route) => {
    reads++
    return route.fulfill(
      broken
        ? { status: 503, json: {} }
        : {
            json: {
              ...recording.run,
              run_id: route.request().url().split('/').pop(),
              status: 'running',
            },
          },
    )
  })
  await page.goto('/backtest')
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await expect(page.getByRole('alert')).toContainText('503')
  const id = await page.getByTestId('run-id').textContent()
  broken = false
  await page.getByRole('button', { name: '重试查询' }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByTestId('run-id')).toHaveText(id!)
  expect(posts).toBe(1)
  await page.getByRole('link', { name: '返回研究首页' }).click()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    '研究，从这里开始。',
  )
  const stopped = reads
  await page.waitForTimeout(1200)
  expect(reads).toBe(stopped)
})
