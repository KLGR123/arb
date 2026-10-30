import { test, expect } from '@playwright/test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { loadDashboard } from '../apps/web/src/features/polymarket/load-dashboard.server'
import { dashboardSchema, qualityMessages } from '../apps/web/src/features/polymarket/contract'
import { DataStatus } from '../apps/web/src/features/polymarket/data-status'
import recorded from '../apps/web/src/features/polymarket/fixtures/recorded-sync.json'
import { dashboard } from './fixtures/polymarket'

test('default is a reproducible real ARB-7 recording with no fabricated analytics', async () => {
  const result = await loadDashboard('')
  expect(result.status).toBe('ready')
  if (result.status !== 'ready') throw new Error('Expected bundled recording')
  const data = result.data
  expect(data.source).toBe('recorded-sync')
  expect(data.prices.data).toHaveLength(528)
  expect(data.compliance?.geoblock.blocked).toBe(true)
  expect(data.forecasts).toEqual([])
  expect(data.pnl).toEqual([])
  expect(data.book).toEqual([])
  expect(data.provenance?.input_sha256).toMatch(/^[a-f0-9]{64}$/)
  expect(dashboardSchema.safeParse({ ...data, pnl: dashboard.pnl }).success).toBe(false)
  expect(qualityMessages(data, '2026-11-01T00:00:00Z').join(' ')).toContain('stale')
})

test('local loader rereads atomic packages and fails visibly without fixture fallback', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'arb-dashboard-'))
  const file = join(dir, 'dashboard.json')
  try {
    expect((await loadDashboard(file)).status).toBe('empty')
    await writeFile(file, '{')
    expect((await loadDashboard(file)).status).toBe('invalid')
    await writeFile(file, JSON.stringify(dashboard))
    expect((await loadDashboard(file, false)).status).toBe('invalid')
    expect((await loadDashboard(file, true)).status).toBe('ready')
    await writeFile(file, JSON.stringify({ ...recorded, source: 'local-sync' }))
    const real = await loadDashboard(file, false)
    expect(real.status).toBe('ready')
    if (real.status === 'ready') expect(real.data.source).toBe('local-sync')
    await writeFile(file, JSON.stringify({ ...recorded, as_of: null }))
    expect((await loadDashboard(file)).status).toBe('invalid')
    await writeFile(file, ' '.repeat(8 * 1024 * 1024 + 1))
    expect((await loadDashboard(file)).status).toBe('invalid')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('real empty, partial, stale, gap and geoblock metadata drive rendered notices', () => {
  const data = dashboardSchema.parse(recorded)
  data.source = 'local-sync'
  const asset = data.event.markets.find((m) => m.id === data.market_id)!.tokens[0]
  data.prices.data = [100, 300].map((timestamp) => ({
    event_id: data.event.id, market_id: data.market_id, token_id: asset.id,
    timestamp, price: 0.5, resolution_seconds: 100,
  }))
  data.prices.next_offset = 2
  data.quality.sync_incomplete = true
  data.resolutions = { data: [], missing_market_ids: [data.market_id] }
  let html = renderToStaticMarkup(createElement(DataStatus, { data, referenceTime: '2026-11-01T00:00:00Z' }))
  for (const notice of ['gap', 'stale', 'partial', '分页未读完', '结算数据缺失', '地区限制', '交易保持拒绝'])
    expect(html).toContain(notice)
  data.prices.data = []
  data.compliance = null
  data.quality.collected_at = null
  html = renderToStaticMarkup(createElement(DataStatus, { data }))
  for (const notice of ['价格：—', '采集时间未知', '地区检查未提供或不可用'])
    expect(html).toContain(notice)
  data.quality.sync_incomplete = null
  html = renderToStaticMarkup(createElement(DataStatus, { data }))
  expect(html).toContain('同步完成状态未知')
  data.quality.collected_at = '2099-01-01T00:00:00Z'
  expect(qualityMessages(data).join(' ')).toContain('核对时钟')
})
