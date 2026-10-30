import { defineConfig, devices } from '@playwright/test'
import { resolve } from 'node:path'

const port = Number(process.env.SMOKE_PORT ?? 3000)

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  // Cold Vela imports and SSR hydration on the shared NAS can exceed 5 seconds.
  expect: { timeout: 15_000 },
  use: { baseURL: `http://127.0.0.1:${port}`, trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
  ],
  webServer: {
    command: `npm run ${process.env.SMOKE_PREVIEW ? 'preview' : 'dev'} --workspace @arb/web -- --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    env: process.env.SMOKE_SYNTHETIC ? {
      ARB_POLYMARKET_ALLOW_SYNTHETIC: '1',
      ARB_POLYMARKET_DASHBOARD_FILE: resolve('apps/web/src/features/polymarket/fixtures/dashboard.json'),
    } : {},
    timeout: 60_000,
  },
})
