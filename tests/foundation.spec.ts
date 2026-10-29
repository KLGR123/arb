import { expect, test } from '@playwright/test'

test('SSR, shadcn interaction and offline Vela rendering', async ({ page, request }) => {
  // Fetch without a JS runtime: the route must really be server rendered.
  const response = await request.get('/')
  expect(response.status()).toBe(200)
  expect(await response.text()).toContain('研究，从这里开始。')

  const errors: string[] = []
  const externalRequests: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  // The offline example must work even when every external network call is denied.
  await page.route(/^https?:\/\/(?!127\.0\.0\.1:3000)/, (route) => {
    externalRequests.push(route.request().url())
    return route.abort()
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('研究，从这里开始。')
  const chart = page.getByTestId('vela-chart')
  await expect(chart).toHaveAttribute('data-status', 'ready')
  const canvases = chart.locator('canvas')
  expect(await canvases.count()).toBeGreaterThan(0)
  const initialCanvasCount = await canvases.count()
  for (const count of [96, 48]) {
    const button = page.getByRole('button', { name: `${count} 小时`, exact: true })
    await button.click()
    await expect(button).toHaveAttribute('aria-pressed', 'true')
    await expect(chart).toHaveAttribute('aria-label', `${count} 根合成小时 K 线，非真实行情`)
    await expect(chart).toHaveAttribute('data-status', 'ready')
    await expect(canvases).toHaveCount(initialCanvasCount)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: `test-results/foundation-${test.info().project.name}.png`, fullPage: true })
  expect(externalRequests).toEqual([])
  expect(errors).toEqual([])
})
