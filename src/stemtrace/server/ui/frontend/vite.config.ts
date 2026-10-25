import path from 'node:path'
import { tanstackRouter } from '@tanstack/router-plugin/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [tanstackRouter(), react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000/stemtrace',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000/stemtrace',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  experimental: {
    // The server rewrites /assets/ URLs only in index.html (server/ui/static.py), so URLs
    // inside CSS (the bundled fonts) must be relative to the stylesheet to work under a
    // mount prefix such as /stemtrace or --root-path.
    renderBuiltUrl: (_filename, { hostType }) =>
      hostType === 'css' ? { relative: true } : undefined,
  },
})
