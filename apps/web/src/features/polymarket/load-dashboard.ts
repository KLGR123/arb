import { createServerFn } from '@tanstack/react-start'

export const getDashboard = createServerFn({ method: 'GET' }).handler(async () => {
  const { loadDashboard } = await import('./load-dashboard.server')
  // Keep opaque upstream resolution.raw JSON out of Start's typed serializer.
  // The route validates the complete JSON envelope again before rendering it.
  return JSON.stringify(await loadDashboard())
})
