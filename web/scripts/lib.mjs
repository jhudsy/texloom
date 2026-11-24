// What the web app's checks (scripts/check-*.mjs) share: web/src's modules built for Node, stand-ins
// for what a browser has and Node doesn't, and the last line a check prints.
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'

export const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** web/src modules, built for Node in one vite build and imported: load('src/a.ts', 'src/b.ts')
 *  gives [a, b]. Modules they share are one copy, as in the app. Each check builds into
 *  node_modules/.cache/<its name>. Set up browser() first if a module needs it as it loads. */
export async function load(...entries) {
  const out = path.join(web, 'node_modules/.cache', path.basename(process.argv[1], '.mjs'))
  const names = entries.map((e) => path.basename(e, '.ts'))
  await build({
    configFile: false, root: web, logLevel: 'warn',
    build: {
      ssr: true, outDir: out, emptyOutDir: true, minify: false,
      rollupOptions: { input: Object.fromEntries(names.map((n, i) => [n, entries[i]])), output: { entryFileNames: '[name].mjs', chunkFileNames: 'chunks/[name]-[hash].mjs' } },
    },
  })
  return Promise.all(names.map((n) => import(pathToFileURL(path.join(out, `${n}.mjs`)).href)))
}

/** Stand-ins for the browser: localStorage (one that keeps nothing, unless one is given; never
 *  Node's own, which warns), and whatever else is given (location, say) where Node has none. */
export function browser({ localStorage = { getItem: () => null, setItem() {}, removeItem() {} }, ...more } = {}) {
  Object.defineProperty(globalThis, 'localStorage', { value: localStorage, configurable: true, writable: true })
  for (const [k, v] of Object.entries(more)) globalThis[k] ??= v
}

/** The last line: `passed`, or `name: FAILED` and the problems found (exit status 1). */
export function report(name, bad, passed) {
  if (bad.length) {
    console.error(`${name}: FAILED\n  ${bad.join('\n  ')}`)
    process.exit(1)
  }
  console.log(passed)
}

/** Checks that print a line each: `const t = tally(); t.check(what, ok, detail); …; t.report()`. */
export function tally() {
  const results = []
  return {
    check: (what, ok, detail = '') => { results.push([what, !!ok, detail]) },
    report() {
      let failed = 0
      for (const [what, ok, detail] of results) {
        console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}${ok || detail === '' || detail === undefined ? '' : `: ${detail}`}`)
        if (!ok) failed++
      }
      console.log(`${results.length - failed}/${results.length} passed`)
      process.exit(failed ? 1 : 0)
    },
  }
}
