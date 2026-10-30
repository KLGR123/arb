import { expect, test } from '@playwright/test'
import { mockBacktest } from './helpers/backtest'

test.beforeEach(async ({ page }) => mockBacktest(page))

test('backtest SSR, API frozen catalog, engines and unique API runs', async ({
  page,
  request,
}) => {
  const response = await request.get('/backtest')
  expect(response.status()).toBe(200)
  expect(await response.text()).toContain('回测工作台')
  const errors: string[] = []
  const external: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  await page.route(/^https?:\/\//, (route) => {
    if (new URL(route.request().url()).origin === new URL(test.info().project.use.baseURL!).origin)
      // Let the API fixture handler registered in beforeEach handle same-origin requests.
      return route.fallback()
    external.push(route.request().url())
    return route.abort()
  })
  await page.goto('/backtest')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('回测工作台')
  await expect(page.getByTestId('chart-empty')).toBeVisible()
  await page.getByLabel('已发布 Dataset').selectOption('demo-synthetic-1h-v2')
  await expect(page.getByTestId('dataset-id')).toHaveText(
    'demo-synthetic-1h-v2',
  )
  await expect(page.getByLabel('结束日期（UTC）')).toHaveValue('2026-01-14')
  await page.getByRole('button', { name: 'realistic', exact: true }).click()
  await expect(page.getByTestId('engine-name')).toContainText('NautilusTrader')
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      request.url().endsWith('/api/research/backtests'),
  )
  await page.getByRole('button', { name: '创建回测 run' }).click()
  expect((await submitted).postDataJSON()).toEqual({
    dataset_id: 'demo-synthetic-1h-v2',
    engine: 'realistic',
    strategy: {
      id: 'sma_cross',
      parameters: { fast_window: 10, slow_window: 30 },
    },
    execution: { fee_bps: 10, slippage_bps: 5, fill_policy: 'next_bar_open' },
    start: '2026-01-01T00:00:00Z',
    end: '2026-01-14T23:59:59Z',
    initial_capital: 10000,
    currency: 'USD',
  })
  await expect(page.getByTestId('run-id')).toHaveText(/^[a-f0-9-]{36}$/)
  const firstId = await page.getByTestId('run-id').textContent()
  await expect(page.getByText('realistic · succeeded · final')).toBeVisible()
  await expect(page.getByTestId('metric-成交数')).toHaveText('10')
  await expect(page.getByTestId('backtest-chart')).toHaveAttribute(
    'data-status',
    'ready',
  )
  await page.getByRole('button', { name: 'fast', exact: true }).click()
  await expect(
    page.getByText('配置已更改。再次创建 run 以执行新配置。'),
  ).toBeVisible()
  await expect(page.getByText('realistic · succeeded · final')).toBeVisible()
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await expect(page.getByTestId('run-id')).not.toHaveText(firstId!)
  await expect(page.getByText('fast · succeeded · draft')).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: `test-results/backtest-${test.info().project.name}.png`,
    fullPage: true,
  })
  expect(errors).toEqual([])
  expect(external).toEqual([])
})

test('preset round trip survives reload and route state stays isolated', async ({
  page,
}) => {
  await page.goto('/')
  await expect(page.getByTestId('vela-chart')).toHaveAttribute(
    'data-status',
    'ready',
  )
  await page.getByRole('button', { name: '96 小时', exact: true }).click()
  await page.getByRole('link', { name: '回测工作台', exact: true }).click()
  await page.getByRole('button', { name: 'realistic', exact: true }).click()
  await page.getByLabel('已发布 Dataset').selectOption('demo-synthetic-1h-v2')
  await page.getByLabel('快线窗口（根）').fill('8')
  await page.getByLabel('慢线窗口（根）').fill('21')
  await page.getByLabel('单边费用（bps）').fill('7.53')
  await page.getByLabel('滑点（bps）').fill('2.57')
  await page.getByLabel('开始日期（UTC）').fill('2026-01-02')
  await page.getByLabel('结束日期（UTC）').fill('2026-01-12')
  await page.getByLabel('初始资金（USD）').fill('100.01')
  await page.getByRole('button', { name: '保存预设' }).click()
  await expect(page.getByText('预设已保存到当前浏览器。')).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole('button', { name: 'fast', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await page.getByRole('button', { name: '加载预设' }).click()
  await expect(page.getByTestId('run-id')).toHaveCount(0)
  await expect(
    page.getByRole('button', { name: 'realistic', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByLabel('已发布 Dataset')).toHaveValue(
    'demo-synthetic-1h-v2',
  )
  for (const [label, value] of [
    ['快线窗口（根）', '8'],
    ['慢线窗口（根）', '21'],
    ['单边费用（bps）', '7.53'],
    ['滑点（bps）', '2.57'],
    ['开始日期（UTC）', '2026-01-02'],
    ['结束日期（UTC）', '2026-01-12'],
    ['初始资金（USD）', '100.01'],
  ]) {
    await expect(page.getByLabel(label)).toHaveValue(value)
  }
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await expect(page.getByTestId('run-id')).toBeVisible()
  await expect(page.getByText('realistic · succeeded · final')).toBeVisible()
  await page.getByRole('link', { name: '返回研究首页' }).click()
  await expect(
    page.getByRole('button', { name: '48 小时', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('link', { name: '回测工作台', exact: true }).click()
  await expect(
    page.getByRole('button', { name: 'fast', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('run-id')).toHaveCount(0)
})

test('invalid parameters and untrusted presets cannot create a run', async ({
  page,
}) => {
  await page.goto('/backtest')
  const create = page.getByRole('button', { name: '创建回测 run' })
  await page.getByLabel('快线窗口（根）').fill('30')
  await expect(create).toBeDisabled()
  await expect(page.getByRole('alert')).toContainText('快线小于慢线')
  await page.getByLabel('快线窗口（根）').fill('10')
  await page.getByLabel('单边费用（bps）').fill('-1')
  await expect(create).toBeDisabled()
  await page.getByLabel('单边费用（bps）').fill('10')
  await page.getByLabel('开始日期（UTC）').fill('2026-02-01')
  await expect(create).toBeDisabled()
  await page.getByLabel('开始日期（UTC）').fill('2026-01-01')
  await page.getByLabel('初始资金（USD）').fill('0')
  await expect(create).toBeDisabled()
  await page.getByLabel('初始资金（USD）').fill('10000')
  await page.getByRole('button', { name: '加载预设' }).click()
  await expect(page.getByRole('alert')).toContainText('还没有保存')
  await page.getByRole('button', { name: '保存预设' }).click()
  const good = await page.evaluate(
    () => localStorage.getItem('arb.backtest.preset.v1')!,
  )
  for (const config of [
    { datasetId: 'unpublished-id' },
    { engine: 'live' },
    { start: '2026-99-99' },
    { initialCapital: null },
    { fastWindow: 0 },
  ]) {
    await page.evaluate(
      ({ good, config }) => {
        const value = JSON.parse(good)
        Object.assign(value.config, config)
        localStorage.setItem('arb.backtest.preset.v1', JSON.stringify(value))
      },
      { good, config },
    )
    await page.getByRole('button', { name: '加载预设' }).click()
    await expect(page.getByRole('alert')).toContainText('预设无法加载')
    await expect(page.getByLabel('已发布 Dataset')).toHaveValue(
      'demo-synthetic-1h-v1',
    )
  }
  await expect(page.getByTestId('run-id')).toHaveCount(0)
})

test('unavailable browser storage reports failure without losing the form', async ({
  page,
}) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => {
      throw new DOMException('Blocked', 'SecurityError')
    }
    Storage.prototype.getItem = () => {
      throw new DOMException('Blocked', 'SecurityError')
    }
  })
  await page.goto('/backtest')
  await page.getByRole('button', { name: '保存预设' }).click()
  await expect(page.getByRole('alert')).toHaveText(
    '浏览器存储不可用，预设未保存。',
  )
  await page.getByRole('button', { name: '加载预设' }).click()
  await expect(page.getByRole('alert')).toContainText('预设无法加载')
  await page.getByRole('button', { name: '创建回测 run' }).click()
  await expect(page.getByTestId('run-id')).toBeVisible()
})
