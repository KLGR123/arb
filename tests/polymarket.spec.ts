import { expect, test } from '@playwright/test'
import {
  brier,
  calibration,
  dashboard,
  depth,
  pnlLayers,
  totalPnl,
  type Forecast,
} from '../apps/web/src/features/polymarket/model'

test('calibration uses individual outcomes, includes p=1 and leaves empty bins empty', () => {
  const forecasts: Forecast[] = [
    {
      market_id: 'a',
      predicted_at: '2026-06-01T00:00:00Z',
      resolved_at: '2026-06-02T00:00:00Z',
      p: 0.2,
      q: 0.5,
      outcome: 1,
    },
    {
      market_id: 'b',
      predicted_at: '2026-06-01T00:00:00Z',
      resolved_at: '2026-06-02T00:00:00Z',
      p: 1,
      q: 0.5,
      outcome: 0,
    },
  ]
  expect(brier(forecasts)).toBeCloseTo(0.82)
  expect(brier(forecasts, 'q')).toBe(0.25)
  const bins = calibration(forecasts)
  expect(bins[9]).toMatchObject({ count: 1, actual: 0, predicted: 1 })
  expect(bins[0]).toMatchObject({ count: 0, actual: null, predicted: null })
  expect(bins.reduce((sum, b) => sum + b.count, 0)).toBe(2)
  expect(brier([])).toBeNull()
})

test('signed PnL, sorted cumulative depth and fixture provenance are consistent', () => {
  expect(
    totalPnl({
      timestamp: '',
      benchmark: 0,
      trading: 10,
      rebates: 3,
      rewards: 5,
      settlement: -6,
      fees: -2,
    }),
  ).toBe(10)
  const levels = [
    { price: 0.4, size: 10 },
    { price: 0.5, size: 20 },
  ]
  expect(depth(levels, 'bid')).toEqual([
    { price: 0.5, size: 20, cumulative: 20 },
    { price: 0.4, size: 10, cumulative: 30 },
  ])
  expect(depth(levels, 'ask').map((l) => l.cumulative)).toEqual([10, 30])
  expect(levels[0].price).toBe(0.4)
  expect(dashboard.source).toBe('synthetic-fixture')
  expect(brier(dashboard.forecasts)).toBeCloseTo(0.1735)
  expect(
    calibration(dashboard.forecasts).reduce((sum, b) => sum + b.count, 0),
  ).toBe(100)
  for (const f of dashboard.forecasts)
    expect(Date.parse(f.predicted_at)).toBeLessThan(Date.parse(f.resolved_at))
  for (const s of dashboard.book) {
    expect(Math.max(...s.bids.map((l) => l.price))).toBeLessThan(
      Math.min(...s.asks.map((l) => l.price)),
    )
    for (const level of [...s.bids, ...s.asks])
      expect(level.size).toBeGreaterThan(0)
  }
})

test('PnL stack keeps zero-to-negative and sign changes on separate sides', () => {
  const points = [0, -18, 5].map((settlement) => ({
    timestamp: '',
    benchmark: 0,
    trading: 100,
    rebates: 10,
    rewards: 20,
    fees: -2,
    settlement,
  }))
  const layers = pnlLayers(points)
  const negative = layers.find(
    (l) => l.key === 'settlement' && l.sign === 'negative',
  )!
  const positive = layers.find(
    (l) => l.key === 'settlement' && l.sign === 'positive',
  )!
  expect(negative.base).toEqual([0, 0, 0])
  expect(negative.top).toEqual([0, -18, 0])
  expect(positive.base).toEqual([130, 130, 130])
  expect(positive.top).toEqual([130, 130, 135])
  for (const layer of layers)
    for (let i = 0; i < points.length; i++) {
      if (layer.sign === 'negative') {
        expect(layer.top[i]).toBeLessThanOrEqual(layer.base[i])
        expect(layer.base[i]).toBeLessThanOrEqual(0)
      } else {
        expect(layer.top[i]).toBeGreaterThanOrEqual(layer.base[i])
        expect(layer.base[i]).toBeGreaterThanOrEqual(0)
      }
    }
})

test('Polymarket SSR and three offline views render, replay stays synchronized', async ({
  page,
  request,
}) => {
  const response = await request.get('/polymarket')
  expect(response.status()).toBe(200)
  expect(await response.text()).toContain('只读 / 纸面研究')
  const errors: string[] = [],
    external: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  await page.route(/^https?:\/\/(?!127\.0\.0\.1:3000)/, (route) => {
    external.push(route.request().url())
    return route.abort()
  })
  await page.goto('/')
  await page.getByRole('link', { name: 'Polymarket 看板', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText(
    'Polymarket 研究看板',
  )
  await expect(page.getByRole('img', { name: /校准曲线/ })).toBeVisible()
  await expect(
    page.getByRole('table', { name: /预测概率分桶/ }).locator('tbody tr'),
  ).toHaveCount(10)
  await page.screenshot({
    path: `test-results/polymarket-calibration-${test.info().project.name}.png`,
    fullPage: true,
  })

  await page.getByRole('tab', { name: /PnL 分析/ }).click()
  const chart = page.getByTestId('polymarket-pnl-chart')
  await expect(chart).toHaveAttribute('data-status', 'ready')
  expect(await chart.locator('canvas').count()).toBeGreaterThan(0)
  await page.screenshot({
    path: `test-results/polymarket-pnl-${test.info().project.name}.png`,
    fullPage: true,
  })
  await page.getByLabel('显示 PnL 分解堆叠').check()
  await expect(page.getByRole('img', { name: /PnL 分解堆叠图/ })).toBeVisible()
  await expect(chart).toHaveCount(0)
  await page.screenshot({
    path: `test-results/polymarket-stack-${test.info().project.name}.png`,
    fullPage: true,
  })
  await page.getByText('查看每日累计明细', { exact: true }).click()
  const final = dashboard.pnl[dashboard.pnl.length - 1]
  await expect(
    page
      .getByRole('table', { name: /每日 UTC 累计 PnL/ })
      .locator('tbody tr')
      .last(),
  ).toContainText(totalPnl(final).toFixed(2))
  await page.getByLabel('显示 PnL 分解堆叠').uncheck()
  await expect(chart).toHaveAttribute('data-status', 'ready')

  await page.getByRole('tab', { name: /盘口回放/ }).click()
  await expect(page.getByTestId('book-frame')).toContainText('1 / 12')
  const initial = await page.getByTestId('book-levels').textContent()
  await page.getByRole('button', { name: '下一帧', exact: true }).click()
  await expect(page.getByTestId('book-frame')).toContainText(
    '2 / 12 · 00:01:00 UTC',
  )
  await expect(page.getByTestId('book-levels')).not.toHaveText(initial!)
  await page.getByRole('button', { name: '定位 00:06:00 UTC' }).click()
  await expect(page.getByLabel('盘口回放进度')).toHaveValue('6')
  await expect(page.getByTestId('book-levels')).toContainText('00:06:00 UTC')
  await page.screenshot({
    path: `test-results/polymarket-book-${test.info().project.name}.png`,
    fullPage: true,
  })
  await page.getByLabel('盘口回放进度').fill('10')
  await page.getByLabel('盘口回放倍速').selectOption('4')
  await page.getByRole('button', { name: '播放回放', exact: true }).click()
  await expect(page.getByTestId('book-frame')).toContainText(
    '12 / 12 · 00:11:00 UTC · 回放结束',
  )
  await expect(
    page.getByRole('button', { name: '下一帧', exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole('button', { name: '播放回放', exact: true }),
  ).toBeDisabled()
  await page.getByRole('button', { name: '回到起点', exact: true }).click()
  await expect(page.getByTestId('book-frame')).toContainText('1 / 12')
  await page.getByRole('button', { name: '播放回放', exact: true }).click()
  await page.getByRole('button', { name: '暂停回放', exact: true }).click()
  await expect(
    page.getByRole('button', { name: '播放回放', exact: true }),
  ).toBeEnabled()

  // Leaving the view tears down its timer and resets route-local state.
  await page.getByRole('tab', { name: /概率校准/ }).click()
  await page.getByRole('tab', { name: /概率校准/ }).focus()
  await page.keyboard.press('ArrowLeft')
  await expect(page.getByRole('tab', { name: /盘口回放/ })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByTestId('book-frame')).toContainText('1 / 12')
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  expect(
    await page.getByRole('button', { name: /下单|连接钱包|充值|提现/ }).count(),
  ).toBe(0)
  expect(errors).toEqual([])
  expect(external).toEqual([])
})
