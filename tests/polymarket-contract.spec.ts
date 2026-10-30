import { dashboard } from './fixtures/polymarket'
import { expect, test } from '@playwright/test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import {
  BookView,
  CalibrationView,
  PnlView,
} from '../apps/web/src/features/polymarket/views'
import { DataStatus } from '../apps/web/src/features/polymarket/data-status'
import {
  dashboardSchema,
  historySchema,
  latestPrice,
  marketFor,
  qualityMessages,
  resolutionSchema,
  selectedAsset,
} from '../apps/web/src/features/polymarket/model'

test('empty views render safely and an empty book side retains replay controls', () => {
  const data = structuredClone(dashboard)
  data.forecasts = []
  data.pnl = []
  data.book = []
  expect(dashboardSchema.safeParse(data).success).toBe(true)
  expect(
    renderToStaticMarkup(createElement(CalibrationView, { data })),
  ).toContain('暂无预测评价样本')
  expect(renderToStaticMarkup(createElement(PnlView, { data }))).toContain(
    '暂无 PnL',
  )
  expect(renderToStaticMarkup(createElement(BookView, { data }))).toContain(
    '暂无盘口',
  )
  data.book = structuredClone(dashboard.book)
  data.book[0].bids = []
  const html = renderToStaticMarkup(createElement(BookView, { data }))
  expect(html).toContain('盘口单侧或双侧为空')
  expect(html).toContain('下一帧')
  data.event.markets[0].protocol = 'unknown'
  expect(renderToStaticMarkup(createElement(BookView, { data }))).toContain('盘口已隔离')
  data.event.markets[0].protocol = 'v2'
  data.prices.data = []
  data.compliance = {
    trading_enabled: false,
    trading_allowed: false,
    allowlist_configured: false,
    mode: 'read_only',
    geoblock: {
      blocked: true,
      country: 'US',
      region: 'CA',
      checked_at: data.as_of,
      error: null,
      cached: true,
    },
  }
  const status = renderToStaticMarkup(createElement(DataStatus, { data }))
  expect(status).toContain('价格：—')
  expect(status).toContain('地区限制')
  expect(status).toContain('交易保持拒绝')
})

test('runtime validation rejects old envelopes, lossy IDs, invalid numbers and times', () => {
  expect(dashboard.schema_version).toBe(2)
  for (const mutate of [
    (d: any) => {
      d.schema_version = 1
    },
    (d: any) => {
      d.event.markets[0].tokens[0].id = 9007199254740992
    },
    (d: any) => {
      d.prices.data[0].price = null
    },
    (d: any) => {
      d.prices.data[0].price = Infinity
    },
    (d: any) => {
      d.prices.data[0].price = 1.01
    },
    (d: any) => {
      d.prices.data[0].timestamp = '1780272000'
    },
    (d: any) => {
      d.forecasts[0].predicted_at = d.forecasts[0].resolved_at
    },
    (d: any) => {
      d.book[0].bids.push(d.book[0].bids[0])
    },
    (d: any) => {
      d.event.markets[0].event_id = 'other'
    },
    (d: any) => {
      d.prices.data[0].token_id = '1'
    },
    (d: any) => {
      d.pnl.reverse()
    },
    (d: any) => {
      d.market = { token_id: 'old-field' }
    },
  ]) {
    const value = structuredClone(dashboard)
    mutate(value)
    expect(dashboardSchema.safeParse(value).success).toBe(false)
  }
  expect(historySchema.parse({ data: [], next_offset: null }).data).toEqual([])
})

test('asset selection uses explicit protocol and quarantines unknown or ambiguous identity', () => {
  const market = structuredClone(marketFor(dashboard))
  const position = market.tokens[0]
  const ctf = { ...position, id: '42', protocol: 'v1' as const }
  market.tokens.unshift(ctf)
  expect(selectedAsset(market, 0)?.id).toBe('900719925474099312345678901')
  market.protocol = 'v1'
  expect(selectedAsset(market, 0)?.id).toBe('42')
  market.protocol = 'unknown'
  expect(selectedAsset(market, 0)).toBeNull()
  market.protocol = 'v2'
  market.tokens = [ctf]
  expect(selectedAsset(market, 0)).toBeNull()
  market.tokens = [position, { ...position, id: '43' }]
  expect(selectedAsset(market, 0)).toBeNull()
})

test('null payout, missing price, delayed buckets, stale data and geoblock remain distinct', () => {
  const data = structuredClone(dashboard)
  expect(qualityMessages(data).join(' ')).toContain('采集时间未知')
  data.prices.data = []
  data.prices.next_offset = 1000
  data.quality.collected_at = '2026-06-01T00:00:00Z'
  data.compliance = {
    trading_enabled: true,
    trading_allowed: false,
    allowlist_configured: false,
    mode: 'read_only',
    geoblock: {
      blocked: true,
      country: 'US',
      region: 'CA',
      checked_at: data.as_of,
      error: null,
      cached: false,
    },
  }
  expect(dashboardSchema.safeParse(data).success).toBe(true)
  expect(latestPrice(data)).toBeNull()
  const messages = qualityMessages(data).join(' ')
  for (const text of [
    '价格缺失',
    'stale',
    '分页未读完',
    '结算数据缺失',
    '地区限制',
  ])
    expect(messages).toContain(text)
  data.compliance.geoblock.error = 'unavailable'
  expect(qualityMessages(data).join(' ')).toContain('不可用')
  expect(qualityMessages(dashboard).join(' ')).toContain('价格更新延迟')
  expect(
    dashboardSchema.safeParse({
      ...data,
      compliance: { ...data.compliance, trading_allowed: true },
    }).success,
  ).toBe(false)
  const resolution = resolutionSchema.parse({
    market_id: data.market_id,
    condition_id: marketFor(data).condition_id,
    status: 'resolved',
    payouts: null,
    split_50_50: false,
    was_disputed: false,
    extended_review: false,
    proposed_price: null,
    reproposed_price: null,
    price: '1000000000000000000',
    resolved_at: null,
    last_updated_at: data.as_of,
    raw: {},
  })
  expect(resolution.payouts).toBeNull()
  expect(resolution.price).toBe('1000000000000000000')
  expect(
    resolutionSchema.parse({
      ...resolution,
      payouts: [0.5, 0.5],
      split_50_50: true,
    }).payouts,
  ).toEqual([0.5, 0.5])
})

test('SSR and browser show protocol identity, missing settlement and read-only quality notices', async ({
  page,
  request,
}) => {
  const response = await request.get('/polymarket')
  expect(response.status()).toBe(200)
  if (!process.env.SMOKE_SYNTHETIC) {
    expect(await response.text()).toContain('token_id')
    await page.goto('/polymarket')
    const status = page.getByRole('region', { name: '市场与数据质量' })
    await expect(status).toContainText('Protocol v1')
    await expect(status).toContainText('结算状态：resolved')
    await expect(status).toContainText('地区限制')
    await expect(status).toContainText('交易保持拒绝')
    return
  }
  expect(await response.text()).toContain('position_id')
  await page.goto('/polymarket')
  const status = page.getByRole('region', { name: '市场与数据质量' })
  await expect(status).toContainText('Protocol v2')
  await expect(status).toContainText('900719925474099312345678901')
  await expect(status).toContainText('结算状态：未知')
  await expect(status).toContainText('采集时间未知')
  await expect(status).toContainText('交易保持拒绝')
  await expect(status).toContainText('价格更新延迟')
})
