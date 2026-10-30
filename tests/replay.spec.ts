import { expect, test } from '@playwright/test'
import { mockBacktest, recording } from './helpers/backtest'

test('recorded run replays without future fills, keeps equity aligned and switches stages', async ({
  page,
}) => {
  const errors: string[] = []
  const external: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route(/^https?:\/\//, (route) => {
    if (new URL(route.request().url()).origin === new URL(test.info().project.use.baseURL!).origin)
      return route.continue()
    external.push(route.request().url())
    return route.abort()
  })
  await mockBacktest(page)
  await page.goto('/backtest')
  await page.getByLabel('已发布 Dataset').selectOption('demo-synthetic-1h-v2')
  await page.getByRole('button', { name: 'realistic', exact: true }).click()
  await page.getByRole('button', { name: '创建回测 run' }).click()
  const chart = page.getByTestId('backtest-chart')
  await expect(chart).toHaveAttribute('data-status', 'ready')
  await expect(
    page.getByText('回放 / 标注 · 非实盘', { exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId('replay-count')).toHaveText('32 / 336')
  await expect(page.getByTestId('replay-fills')).toHaveText('1')
  await expect(chart).toHaveAttribute('data-fill-labels', '1')
  await expect(page.getByTestId('replay-position')).not.toContainText(
    '当前持仓 0.000000',
  )

  await page.getByRole('button', { name: '回到起点' }).click()
  await expect(page.getByTestId('replay-count')).toHaveText('1 / 336')
  await expect(page.getByTestId('replay-fills')).toHaveText('0')
  await expect(chart).toHaveAttribute('data-fill-labels', '0')
  await expect(page.getByTestId('replay-equity')).toHaveText(
    'Equity 10000.00 USD',
  )
  await expect(page.getByTestId('replay-position')).toContainText(
    '当前持仓 0.000000',
  )
  await page.getByRole('button', { name: '下一根' }).click()
  await expect(page.getByTestId('replay-count')).toHaveText('2 / 336')
  await expect(page.getByTestId('replay-readout')).toHaveAttribute(
    'data-time',
    recording.bars.bars[1].timestamp,
  )

  await page.getByLabel('回放倍速').selectOption('16')
  await page.getByRole('button', { name: '播放回放', exact: true }).click()
  await expect(page.getByRole('button', { name: '暂停回放' })).toBeVisible()
  await expect
    .poll(async () =>
      Number(
        (await page.getByTestId('replay-count').textContent())!.split(' / ')[0],
      ),
    )
    .toBeGreaterThan(4)
  await page.getByRole('button', { name: '暂停回放' }).click()
  const paused = await page.getByTestId('replay-count').textContent()
  await page.waitForTimeout(200)
  await expect(page.getByTestId('replay-count')).toHaveText(paused!)

  // Seek immediately before/at the first entry and exit, testing rewind as well.
  for (const [count, fills, flat] of [
    [30, 0, true],
    [31, 1, false],
    [33, 1, false],
    [34, 2, true],
    [30, 0, true],
  ] as const) {
    await page.getByLabel('回放进度').fill(String(count))
    await expect(page.getByTestId('replay-count')).toHaveText(`${count} / 336`)
    await expect(page.getByTestId('replay-fills')).toHaveText(String(fills))
    if (flat)
      await expect(page.getByTestId('replay-position')).toContainText(
        '当前持仓 0.000000',
      )
    const expected =
      recording.run.stages.realistic.result.equity[count - 1].value
    await expect(page.getByTestId('replay-equity')).toHaveText(
      `Equity ${expected.toFixed(2)} USD`,
    )
  }
  await page.getByRole('button', { name: '显示完整 run' }).click()
  await expect(page.getByTestId('replay-count')).toHaveText('336 / 336')
  await expect(page.getByTestId('replay-fills')).toHaveText('10')
  await expect(chart).toHaveAttribute('data-fill-labels', '10')
  await expect(page.getByRole('button', { name: '下一根' })).toBeDisabled()
  await expect(
    page.getByRole('button', { name: '播放回放', exact: true }),
  ).toBeDisabled()
  await expect(page.getByTestId('replay-equity')).toHaveText(
    `Equity ${recording.run.stages.realistic.result.metrics.final_equity.toFixed(2)} USD`,
  )
  await page.getByLabel('运行阶段').selectOption('fast')
  await expect(chart).toHaveAttribute('data-status', 'ready')
  await expect(page.getByTestId('replay-count')).toHaveText('32 / 336')
  await page.getByRole('button', { name: '显示完整 run' }).click()
  await expect(page.getByTestId('replay-fills')).toHaveText('10')
  await expect(page.getByTestId('replay-equity')).toHaveText(
    `Equity ${recording.run.stages.fast.result.metrics.final_equity.toFixed(2)} USD`,
  )
  await page.getByLabel('止损 / 止盈分析参考线').uncheck()
  await page.getByLabel('止损 / 止盈分析参考线').check()
  await page.getByRole('button', { name: '回到起点' }).click()
  // The final step automatically ends Vela's replay and must settle at full data.
  await page.getByLabel('回放进度').fill('335')
  await page.getByRole('button', { name: '下一根' }).click()
  await expect(page.getByTestId('replay-count')).toHaveText('336 / 336')
  await expect(
    page.getByRole('button', { name: '播放回放', exact: true }),
  ).toBeDisabled()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
  if (test.info().project.name === 'desktop') {
    const box = await chart.boundingBox()
    expect(box).not.toBeNull()
    await chart.hover({
      position: { x: box!.width * 0.55, y: box!.height * 0.3 },
    })
    await expect(page.getByTestId('replay-readout')).toContainText('图表光标')
    const priceTime = await page
      .getByTestId('replay-readout')
      .getAttribute('data-time')
    await chart.hover({
      position: { x: box!.width * 0.55, y: box!.height * 0.85 },
    })
    await expect(page.getByTestId('replay-readout')).toHaveAttribute(
      'data-time',
      priceTime!,
    )
    await expect(page.getByTestId('replay-equity')).toHaveAttribute(
      'data-time',
      priceTime!,
    )
    await page.mouse.move(0, 0)
  }
  await chart.screenshot({
    path: `test-results/replay-${test.info().project.name}.png`,
  })
  await page.getByRole('link', { name: '返回研究首页' }).click()
  await page.getByRole('link', { name: '回测工作台', exact: true }).click()
  await expect(page.getByTestId('chart-empty')).toBeVisible()
  expect(errors).toEqual([])
  expect(external).toEqual([])
})
