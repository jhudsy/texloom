import { defineConfig } from 'vite'

// In development, the Python relay runs on :8787 and Vite proxies to it.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'ws://localhost:8787', ws: true },
      '/blob': 'http://localhost:8787',
      '/core': 'http://localhost:8787',
    },
  },
  optimizeDeps: { exclude: ['texlyre-busytex'] },
  build: { target: 'es2022' },
})
