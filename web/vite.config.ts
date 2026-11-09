import { defineConfig, type Plugin } from 'vite'
import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { createHash } from 'node:crypto'

/** Write /sw.js with the list of built files and a version derived from them. The list is
 *  taken from the files actually written: during generateBundle the bundle still holds
 *  chunks Vite removes afterwards (the empty JS stub of a CSS-only import), and a listed
 *  file that doesn't exist made the new service worker fail to install. */
function serviceWorker(): Plugin {
  return {
    name: 'texloom-service-worker',
    apply: 'build',
    writeBundle(opts) {
      const out = opts.dir!
      const walk = (d: string): string[] => readdirSync(d, { withFileTypes: true })
        .flatMap((e) => (e.isDirectory() ? walk(join(d, e.name)) : [join(d, e.name)]))
      // KaTeX's fonts come in three formats: browsers that run this app use woff2, so the
      // older two aren't kept for offline use
      const built = walk(out).map((f) => '/' + relative(out, f).split('\\').join('/'))
        .filter((f) => f !== '/index.html' && f !== '/sw.js' && !/\.(woff|ttf)$/.test(f))
      const shell = [...new Set(['/', '/index.html', ...built])].sort()
      const engine = readFileSync(new URL('./node_modules/texlyre-busytex/package.json', import.meta.url), 'utf8')
      const version = createHash('sha256').update(shell.join('\n') + engine).digest('hex').slice(0, 12)
      const src = readFileSync(new URL('./sw-template.js', import.meta.url), 'utf8')
        .replace('__VERSION__', JSON.stringify(version))
        .replace('__SHELL__', JSON.stringify(shell))
        .replace('__ENGINE__', JSON.stringify(JSON.parse(readFileSync(new URL('./node_modules/texlyre-busytex/package.json', import.meta.url), 'utf8')).version))
      writeFileSync(join(out, 'sw.js'), src)
    },
  }
}

/** Serve PDF.js's character maps at /cmaps/. Without them it can't draw CID fonts that name an
 *  Adobe character collection, which is what XeLaTeX writes for Chinese, Japanese and Korean. */
function pdfjsCmaps(): Plugin {
  const dir = new URL('./node_modules/pdfjs-dist/cmaps/', import.meta.url)
  return {
    name: 'texloom-pdfjs-cmaps',
    generateBundle() {
      for (const fileName of readdirSync(dir)) this.emitFile({ type: 'asset', fileName: `cmaps/${fileName}`, source: readFileSync(new URL(fileName, dir)) })
    },
    configureServer(server) {
      server.middlewares.use('/cmaps', (req, res) => { res.end(readFileSync(new URL(`.${req.url}`, dir))) })
    },
  }
}

// In development, the Python relay runs on :8787 and Vite proxies to it.
export default defineConfig({
  plugins: [pdfjsCmaps(), serviceWorker()],
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
