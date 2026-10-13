import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In production FastAPI serves the built SPA, so the API is same-origin and the client uses relative URLs ('').
// In dev, Vite proxies the same paths to the API container, which keeps that "single origin" property: no CORS and
// no build-time API URL. SSE responses stream through the proxy unchanged.
const API = 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // One bundle (~160 KB gzipped) for an internal dashboard served from the API; route-level code splitting would
    // add loading states without a visible benefit here.
    chunkSizeWarningLimit: 700,
  },
  server: {
    proxy: {
      '/api': API,
      '/health': API,
      '/docs': API,
      '/openapi.json': API,
    },
  },
})
