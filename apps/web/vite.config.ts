import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import { tanstackStart } from '@tanstack/react-start/plugin/vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'ARB_BACKTEST_')
  const proxy = {
    '/api/research': {
      target: env.ARB_BACKTEST_API_URL || 'http://127.0.0.1:8000',
      rewrite: (path: string) => path.replace(/^\/api\/research/, ''),
      timeout: 15_000,
      proxyTimeout: 15_000,
    },
  }
  return {
    server: { proxy },
    preview: { proxy },
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    plugins: [tailwindcss(), tanstackStart(), react()],
  }
})
