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
      // 127.0.0.1, not localhost: on Windows "localhost" can resolve to the
      // IPv6 ::1 first, which uvicorn (IPv4-only bind) refuses -> 500s here.
      '/runs': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/settings': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/repos': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      // Lesson edit/delete are keyed by lesson_id alone, so they sit at the root
      // rather than under /repos — they need their own entry here or they 404 in dev.
      '/lessons': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
