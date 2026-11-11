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
      server.middlewares.use('/cmaps', (req, res) => {
        const name = decodeURIComponent((req.url ?? '').split('?')[0]).replace(/^\/+/, '')
        if (!/^[\w.-]+$/.test(name) || name.startsWith('.')) { res.statusCode = 404; res.end(); return } // one file in the folder, nothing else
        res.end(readFileSync(new URL(name, dir)))
      })
    },
  }
}

/** Write /licenses.txt: TeXLoom's own licence and where its source is, then every package bundled
 *  into the app (the dependencies in package.json and theirs) with its licence text, then what
 *  isn't an npm package: TeX Live, TeXCount and the TeX engine's patches. */
function licences(): Plugin {
  const root = new URL('./', import.meta.url)
  const read = (path: string) => { try { return readFileSync(new URL(path, root), 'utf8') } catch { return null } }
  return {
    name: 'texloom-licences',
    apply: 'build',
    generateBundle() {
      const seen = new Map<string, { version: string; license: string; home: string; text: string }>()
      const visit = (name: string) => {
        if (seen.has(name)) return
        const meta = read(`node_modules/${name}/package.json`)
        if (!meta) return
        const pkg = JSON.parse(meta)
        const dir = `node_modules/${name}/`
        let files: string[] = []
        try { files = readdirSync(new URL(dir, root)).filter((f) => /^(licen[cs]e|copying|notice)/i.test(f)) } catch {}
        const repo = typeof pkg.repository === 'string' ? pkg.repository : pkg.repository?.url
        seen.set(name, {
          version: pkg.version, license: typeof pkg.license === 'string' ? pkg.license : pkg.license?.type ?? 'see its files',
          home: pkg.homepage ?? (repo ? String(repo).replace(/^git\+/, '').replace(/\.git$/, '') : `https://www.npmjs.com/package/${name}`),
          text: files.map((f) => read(dir + f) ?? '').join('\n\n').trim(),
        })
        for (const dep of Object.keys(pkg.dependencies ?? {})) visit(dep)
      }
      for (const dep of Object.keys(JSON.parse(read('package.json')!).dependencies)) visit(dep)
      const line = '='.repeat(78)
      const parts = [
        'TeXLoom web app: licences',
        '',
        'TeXLoom is free software under the GNU Affero General Public License, version 3 or later',
        '(AGPL-3.0-or-later). Its complete source, including the changes made to the TeX engine below,',
        'is at https://github.com/jhudsy/texloom.',
        '',
        'It includes the software listed below, each under its own licence.',
        '',
        line,
        'TeX Live engines and packages, through BusyTeX (https://github.com/busytex/busytex) and texlyre-busytex',
        'Each TeX Live component is free software under its own licence (GPL, LPPL, the Knuth licence',
        'and others): https://tug.org/texlive/copying.html. Packages are fetched unchanged from CTAN',
        'mirrors, where their sources are. The engine is patched at deploy time so that Biber finds',
        'bibliographies in folders and keeps accented names: deploy/patch-busytex.py in the source above.',
        '',
        line,
        'TeXCount (web/public/vendor/texcount.pl), unchanged',
        'Copyright Einar Andreas Rødland. LaTeX Project Public License (LPPL): https://www.latex-project.org/lppl/',
        'http://app.uio.no/ifi/texcount/',
        '',
        ...[...seen].sort(([a], [b]) => a.localeCompare(b)).flatMap(([name, p]) => [
          line, `${name} ${p.version}`, `Licence: ${p.license}`, p.home, '', p.text || '(no licence file in the package)', ''])
      ]
      this.emitFile({ type: 'asset', fileName: 'licenses.txt', source: parts.join('\n') })
    },
  }
}

// In development, the Python relay runs on :8787 and Vite proxies to it.
export default defineConfig({
  plugins: [pdfjsCmaps(), serviceWorker(), licences()],
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
