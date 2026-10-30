import { expect, test } from '@playwright/test'
import { dashboard } from './fixtures/polymarket'
import cases from '../research/tests/fixtures/selected-assets.json'
import {
  assetSelectionSchema,
  marketFor,
  selectedAsset,
} from '../apps/web/src/features/polymarket/model'

test('dashboard consumes the same frozen selection as Python loader and scanner', () => {
  for (const item of cases) {
    // JSON serialization is part of the contract; asset IDs stay strings.
    const view = assetSelectionSchema.parse(JSON.parse(JSON.stringify(item.selection)))
    const market = {
      ...marketFor(dashboard),
      id: '200',
      event_id: '100',
      condition_id: '0xAbCd',
      protocol: view.protocol_version,
      asset_selection: view,
      tokens: [], // historical ID metadata is never a fallback input
    }
    for (const index of [0, 1]) {
      const asset = selectedAsset(market, index)
      expect(asset, item.name).toEqual(view.status === 'selected' ? view.assets[index] : null)
      if (asset) expect(typeof asset.asset_id).toBe('string')
    }
  }
})

test('malformed selected views cannot introduce Number IDs or another ledger', () => {
  const market = structuredClone(marketFor(dashboard))
  const invalid = structuredClone(market.asset_selection) as any
  invalid.assets[0].asset_id = 9007199254740992
  expect(assetSelectionSchema.safeParse(invalid).success).toBe(false)
  market.asset_selection.assets[0].ledger = 'ConditionalTokens'
  expect(selectedAsset(market, 0)).toBeNull()
})
