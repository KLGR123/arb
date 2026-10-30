import fixture from '../../apps/web/src/features/polymarket/fixtures/dashboard.json'
import { dashboardSchema } from '../../apps/web/src/features/polymarket/contract'

// Synthetic data is imported by tests only, never the product model or views.
export const dashboard = dashboardSchema.parse(fixture)
