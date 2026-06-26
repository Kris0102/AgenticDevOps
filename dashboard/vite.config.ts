import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Single-user localhost. Vite dev server proxies API + SSE to the FastAPI control
// plane (uvicorn :8000), so the browser sees one origin — no CORS. SSE streams pass
// through http-proxy unbuffered.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/runs': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
