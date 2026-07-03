import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const backendUrl = process.env.VITE_BACKEND_URL || 'http://localhost:8010'
const backendWsUrl = backendUrl.replace(/^http/, 'ws')

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
      '/ws': {
        target: backendWsUrl,
        ws: true,
      },
      '/opencode': {
        target: 'http://localhost:4096',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/opencode/, ''),
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
