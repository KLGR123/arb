import { open } from 'node:fs/promises'
import recorded from './fixtures/recorded-sync.json'
import { dashboardSchema, type Dashboard } from './contract'

export type DashboardResult =
  | { status: 'ready'; data: Dashboard; loaded_at: string }
  | { status: 'empty' | 'invalid' | 'unavailable'; message: string }

// Server configuration only. There is no browser-supplied path or upstream URL.
export async function loadDashboard(
  file = process.env.ARB_POLYMARKET_DASHBOARD_FILE,
  allowSynthetic = process.env.ARB_POLYMARKET_ALLOW_SYNTHETIC === '1',
): Promise<DashboardResult> {
  let value: unknown = recorded
  if (file) {
    try {
      const handle = await open(file, 'r')
      try {
        const stat = await handle.stat()
        if (!stat.isFile() || stat.size > 8 * 1024 * 1024)
          return { status: 'invalid', message: '本地 sync 包不是普通文件或超过 8 MiB。' }
        value = JSON.parse(await handle.readFile('utf8'))
      } finally {
        await handle.close()
      }
    } catch (error) {
      if (error instanceof SyntaxError)
        return { status: 'invalid', message: '本地 sync 包 JSON 无效，请重新导出。' }
      const code = (error as NodeJS.ErrnoException).code
      return code === 'ENOENT'
        ? { status: 'empty', message: '尚无本地 sync 包，请先同步并导出。' }
        : { status: 'unavailable', message: '本地 sync 包不可读，请检查服务端配置。' }
    }
  }
  const parsed = dashboardSchema.safeParse(value)
  if (!parsed.success)
    return { status: 'invalid', message: '本地 sync 包不符合展示契约，请重新导出。' }
  if (parsed.data.source === 'synthetic-fixture' && !allowSynthetic)
    return { status: 'invalid', message: '默认路径拒绝合成 fixture；仅离线回归可显式启用。' }
  return { status: 'ready', data: parsed.data, loaded_at: new Date().toISOString() }
}
