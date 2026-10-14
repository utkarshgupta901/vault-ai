import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In production FastAPI serves the built SPA, so the API is same-origin and the client uses relative URLs ('').
// In dev, Vite proxies the same paths to the API container, which keeps that "single origin" property: no CORS and
// no build-time API URL. SSE responses stream through the proxy unchanged.
// VAULT_API points the proxy at an API on another port (`make web-dev` passes its API=); it is read here, in Node, and
// never reaches the browser bundle.
const API = process.env.VAULT_API || 'http://localhost:8000'

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
