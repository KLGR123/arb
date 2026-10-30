import { expect, test } from '@playwright/test'

test.skip(
  !process.env.ARB_BACKTEST_LIVE,
  'Run with python3 scripts/smoke-backtest.py',
)
for (const engine of ['fast', 'realistic']) {
  test(`local FastAPI and worker execute frozen ${engine} job`, async ({
    page,
  }) => {
    test.setTimeout(180_000)
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route(
      (url) =>
        ['http:', 'https:'].includes(url.protocol) &&
        url.origin !== new URL(test.info().project.use.baseURL!).origin,
      (route) => route.abort(),
    )
    await page.goto('/backtest')
    await page.getByLabel('已发布 Dataset').selectOption('demo-synthetic-1h-v2')
    await page.getByRole('button', { name: engine, exact: true }).click()
    const created = page.waitForResponse(
      (response) =>
        response.url().endsWith('/api/research/backtests') &&
        response.request().method() === 'POST',
    )
    await page.getByRole('button', { name: '创建回测 run' }).click()
    expect((await created).status()).toBe(202)
    await expect(page.getByTestId('run-id')).toHaveText(/^[a-f0-9-]{36}$/)
    const id = await page.getByTestId('run-id').textContent()
    await expect(
      page.getByText(
        `${engine} · succeeded · ${engine === 'fast' ? 'draft' : 'final'}`,
      ),
    ).toBeVisible({ timeout: 150_000 })
    await expect(page.getByTestId('backtest-chart')).toHaveAttribute(
      'data-status',
      'ready',
    )
    await page.getByLabel('运行阶段').selectOption(engine)
    await page.getByRole('button', { name: '显示完整 run' }).click()
    await expect(page.getByTestId('replay-count')).toHaveText('336 / 336')
    await expect(page.getByTestId('replay-fills')).toHaveText('10')
    await expect(page.getByTestId('metric-成交数')).toHaveText('10')
    await expect(page.getByTestId('run-id')).toHaveText(id!)
    expect(errors).toEqual([])
  })
}
