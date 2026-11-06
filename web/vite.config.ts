import { defineConfig, type Plugin } from 'vite'
import { readFileSync, readdirSync } from 'node:fs'
import { createHash } from 'node:crypto'

/** Emit /sw.js with the list of built files and a version derived from them. */
function serviceWorker(): Plugin {
  return {
    name: 'texloom-service-worker',
    apply: 'build',
    generateBundle(_opts, bundle) {
      const publicFiles = readdirSync(new URL('./public', import.meta.url)).map((f) => `/${f}`)
      const shell = ['/', '/index.html', ...Object.keys(bundle).filter((f) => f !== 'index.html').map((f) => `/${f}`), ...publicFiles]
      const version = createHash('sha256').update(shell.sort().join('\n')).digest('hex').slice(0, 12)
      const src = readFileSync(new URL('./sw-template.js', import.meta.url), 'utf8')
        .replace('__VERSION__', JSON.stringify(version))
        .replace('__SHELL__', JSON.stringify([...new Set(shell)]))
      this.emitFile({ type: 'asset', fileName: 'sw.js', source: src })
    },
  }
}

// In development, the Python relay runs on :8787 and Vite proxies to it.
export default defineConfig({
  plugins: [serviceWorker()],
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'ws://localhost:8787', ws: true },
      '/blob': 'http://localhost:8787',
      '/api': 'http://localhost:8787',
      '/core': 'http://localhost:8787',
    },
  },
  optimizeDeps: { exclude: ['texlyre-busytex'] },
  build: { target: 'es2022' },
})
