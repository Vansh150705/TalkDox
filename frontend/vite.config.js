import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api to the backend (default: local FastAPI on :8000). Override with VITE_PROXY_TARGET.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    server: { port: 5173, proxy: { '/api': env.VITE_PROXY_TARGET || 'http://localhost:8000' } },
  }
})
