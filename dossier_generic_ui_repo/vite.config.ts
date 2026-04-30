import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite dev server proxies API requests to the running uvicorn backends
// so the SPA can use a single origin during development. In production
// the SPA can be served behind the same reverse proxy as the API or
// configured to talk directly via env-driven base URLs — but for the
// phase 1 demo, the proxy is the simplest path that works regardless
// of the engine's CORS configuration.
//
// Engine: http://127.0.0.1:8000 — main dossier API
// Files:  http://127.0.0.1:8001 — separate file service for uploads
//
// We deliberately do NOT proxy `/files/*` on the engine path because
// the engine has its own /files routes (signed-URL minting); the
// frontend talks to BOTH on different paths. The convention used:
//   /api/*       → engine
//   /file-svc/*  → file service
// The api client (src/composables/useApi.ts) handles the prefix.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/file-svc': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/file-svc/, ''),
      },
    },
  },
})
