import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The Python backend owns /api. In development Vite proxies to it so the app
// runs from a single origin; in production the backend serves ./dist directly.
const API_TARGET = process.env.LEMONDX_API ?? 'http://127.0.0.1:8099'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
