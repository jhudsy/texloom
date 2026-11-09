// In-browser LaTeX compilation via texlyre-busytex (TeX Live 2026 in WebAssembly).
// Engine assets are static files served from /core/busytex/ (the server's
// tex-assets/ directory). The browser caches them in IndexedDB per site, so they
// are downloaded once and shared by every project.
import type { BusyTexRunner } from 'texlyre-busytex'
import { stripComments } from './texscan'

const BASE = '/core/busytex'
let runner: Promise<BusyTexRunner> | null = null
let available: Promise<boolean> | null = null

export type Progress = { phase: 'download' | 'compile'; percent?: number; loaded?: number; total?: number }

/** Does the server (or the offline cache) have this TeX asset? */
export async function hasAsset(path: string): Promise<boolean> {
  try { if ('caches' in self && (await caches.match(path))) return true } catch {}
  return fetch(path, { method: 'HEAD' }).then((r) => r.ok).catch(() => false)
}

export function browserCompileAvailable(): Promise<boolean> {
  return (available ??= hasAsset(`${BASE}/busytex.wasm`))
}

export async function engineCached(): Promise<boolean> {
  try {
    const { isPackageCached } = await import('texlyre-busytex')
    return await isPackageCached(`${BASE}/texlive-basic.js`)
  } catch { return false }
}

export async function clearEngineCache() {
  const { clearAllPackageCache } = await import('texlyre-busytex')
  await clearAllPackageCache()
  runner?.then((r) => r.terminate()).catch(() => {})
  runner = null; runnerLevel = null
}

let progressCb: ((p: Progress) => void) | undefined

// TeX Live files on demand: when the server offers it (/api/info → texlive), the engine asks
// it for any file it lacks (GET <endpoint>/<kpathsea format>/<name>), so the basic bundle is
// enough to start and every TeX Live package works; the big recommended/extra bundles
// aren't downloaded at all. Remembered for offline use (fetched files stay cached).
const ENDPOINT_KEY = 'texloom.texliveEndpoint'
let endpoint: Promise<string | null> | null = null
export function texliveEndpoint(): Promise<string | null> {
  return (endpoint ??= fetch('/api/info', { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then((j) => {
      const e = typeof j?.texlive?.endpoint === 'string' && j.texlive.endpoint.startsWith('/') ? new URL(j.texlive.endpoint, location.origin).href : null
      try { if (e) localStorage.setItem(ENDPOINT_KEY, e); else localStorage.removeItem(ENDPOINT_KEY) } catch {}
      return e
    })
    .catch(() => { try { return localStorage.getItem(ENDPOINT_KEY) } catch { return null } }))
}

// The TeX bundles are nested: basic ⊂ recommended ⊂ extra, and each carries its own file
// index (ls-R). Loaded together, whichever finishes last hides the other's extra files
// (e.g. nicefrac.sty "not found" although extra was loaded). So a runner gets basic plus at
// most ONE of recommended/extra: recommended when it has every package and class the
// project asks for, otherwise extra (a superset). Busytex itself would enable both
// whenever any package is unresolved, which is nearly every real document.
type Level = 'recommended' | 'extra'
let runnerLevel: Level | null = null

async function bundles(): Promise<{ basic: boolean; recommended: boolean; extra: boolean }> {
  const [basic, recommended, extra] = await Promise.all(['basic', 'recommended', 'extra'].map((p) => hasAsset(`${BASE}/texlive-${p}.js`)))
  return { basic, recommended, extra }
}

let recommendedNames: Promise<Set<string> | null> | null = null
/** File names (e.g. booktabs.sty, beamer.cls) in the recommended bundle, from its file list. */
function recommendedFiles(): Promise<Set<string> | null> {
  return (recommendedNames ??= fetch(`${BASE}/texlive-recommended.txt`)
    .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
    .then((t) => new Set(t.split('\n').map((l) => l.slice(l.lastIndexOf('/') + 1).trim()).filter((n) => /\.(sty|cls|bst|bbx|cbx|lbx|def|cfg|fd|clo|tex)$/.test(n))))
    .catch(() => { recommendedNames = null; return null }))
}

/** Files the project asks TeX for by name (packages, classes, bibliography styles, Beamer themes). */
export function requestedFiles(files: Map<string, string | Uint8Array>): string[] {
  const out = new Set<string>()
  const add = (list: string, ext: string) => {
    for (const n of list.split(',')) { const t = n.trim(); if (/^[\w.@+-]+$/.test(t)) out.add(t.endsWith(ext) ? t : t + ext) }
  }
  for (const [path, v] of files) {
    if (typeof v !== 'string' || !/\.(tex|sty|cls|ltx)$/i.test(path)) continue
    const src = stripComments(v)
    for (const m of src.matchAll(/\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\]\s*)?\{([^}]*)\}/g)) add(m[1], '.sty')
    for (const m of src.matchAll(/\\(?:documentclass|LoadClass)\s*(?:\[[^\]]*\]\s*)?\{([^}]*)\}/g)) add(m[1], '.cls')
    for (const m of src.matchAll(/\\bibliographystyle\s*\{([^}]*)\}/g)) add(m[1], '.bst')
    for (const m of src.matchAll(/\\use(|color|font|inner|outer)theme\s*(?:\[[^\]]*\]\s*)?\{([^}]*)\}/g)) add(`beamer${m[1]}theme${m[2].trim()}`, '.sty')
  }
  const local = new Set([...files.keys()].map((p) => p.slice(p.lastIndexOf('/') + 1)))
  return [...out].filter((n) => !local.has(n))
}

/** The engine when the project hasn't chosen one. ctex, xeCJK and fontspec can't work on pdfLaTeX,
 *  where they stop with an error that doesn't say what to do. */
export function defaultEngine(files: Map<string, string | Uint8Array>): 'xelatex' | 'pdflatex' {
  const needsUnicode = /^(?:ctex(?:art|rep|book|beamer)?|xeCJK|fontspec)\.(?:cls|sty)$/
  return requestedFiles(files).some((n) => needsUnicode.test(n)) ? 'xelatex' : 'pdflatex'
}

/** The one optional bundle this project needs. */
async function levelFor(files: Map<string, string | Uint8Array>): Promise<Level | null> {
  if (await texliveEndpoint()) return null // basic, plus single files from the server
  const have = await bundles()
  if (!have.extra) return have.recommended ? 'recommended' : null
  if (!have.recommended || runnerLevel === 'extra') return 'extra' // extra covers everything recommended has
  const names = await recommendedFiles()
  return names && requestedFiles(files).every((n) => names.has(n)) ? 'recommended' : 'extra'
}

async function getRunner(level: Level | null): Promise<BusyTexRunner> {
  if (runner && runnerLevel === 'recommended' && level === 'extra') { // need the bigger bundle: start over
    runner.then((r) => r.terminate()).catch(() => {})
    runner = null
  }
  return (runner ??= (async () => {
    const { BusyTexRunner } = await import('texlyre-busytex')
    const have = await bundles()
    runnerLevel = level
    const r = new BusyTexRunner({
      busytexBasePath: BASE,
      preloadDataPackages: have.basic ? [`${BASE}/texlive-basic.js`] : [], // loaded up front
      catalogDataPackages: level ? [`${BASE}/texlive-${level}.js`] : [], // fetched only if a document needs it
      onDownloadProgress: (p) => progressCb?.({ phase: 'download', ...p }),
    })
    await r.initialize(true)
    return r
  })().catch((e) => { runner = null; runnerLevel = null; throw e }))
}

const DRIVERS = {
  pdflatex: 'pdftex_bibtex8',
  xelatex: 'xetex_bibtex8_dvipdfmx',
  lualatex: 'luahbtex_bibtex8',
} as const

/** After a failed compile, LaTeX's own .log (and BibTeX's .blg) from the engine's files.
 *  The engine keeps only the last step's log: when biber or BibTeX fails (a missing .bib,
 *  say), that is its output, and LaTeX's log, which names the problem, would be lost. */
async function withTexLog(r: BusyTexRunner, main: string, log: string): Promise<string> {
  try {
    const base = main.replace(/\.tex$/i, '')
    const files = await r.readProjectFiles()
    const text = (ext: string) => {
      const f = files.find((x) => x.path === base + ext)
      return f ? (typeof f.content === 'string' ? f.content : new TextDecoder().decode(f.content)) : ''
    }
    const tex = text('.log'), blg = text('.blg')
    const extra = [tex && !log.includes(tex.slice(0, 200)) ? `TEX LOG (${base}.log):\n${tex}` : '', blg && !log.includes(blg.slice(0, 200)) ? `BIBTEX LOG (${base}.blg):\n${blg}` : '']
    return [log, ...extra].filter(Boolean).join('\n\n======\n\n')
  } catch {
    return log
  }
}

export class CompileStopped extends Error {}
type Job = { stopped: boolean; inEngine: boolean; stop: (e: Error) => void }
let job: Job | null = null

/** Stop the compile in progress. While it's still downloading, the download carries on for next time. */
export function stopBrowserCompile() {
  if (!job) return
  job.stopped = true
  if (job.inEngine) { // TeX can only be stopped by ending its worker; the next compile starts a new one
    runner?.then((r) => r.terminate()).catch(() => {})
    runner = null
    runnerLevel = null
  }
  job.stop(new CompileStopped())
}

type CompileResult = { pdf: Uint8Array | null; log: string; synctex: Uint8Array | null }

export function compileInBrowser(
  files: Map<string, string | Uint8Array>,
  main: string,
  opts: { engine?: string; bib?: string } = {},
  onProgress?: (p: Progress) => void,
): Promise<CompileResult> {
  const mine: Job = { stopped: false, inEngine: false, stop: () => {} }
  job = mine
  const stopped = new Promise<never>((_, reject) => { mine.stop = reject })
  return Promise.race([compiling(mine, files, main, opts, onProgress), stopped]).finally(() => { if (job === mine) job = null })
}

async function compiling(
  mine: Job,
  files: Map<string, string | Uint8Array>,
  main: string,
  opts: { engine?: string; bib?: string },
  onProgress?: (p: Progress) => void,
): Promise<CompileResult> {
  progressCb = onProgress
  try {
    const r = await getRunner(await levelFor(files))
    if (mine.stopped) throw new CompileStopped()
    mine.inEngine = true
    onProgress?.({ phase: 'compile' })
    const all = [...files.values()].filter((v): v is string => typeof v === 'string').join('\n')
    let biber = false, bibtex = false
    const bib = opts.bib ?? 'auto'
    if (bib === 'auto') {
      const usesBiblatex = /\\usepackage(\[[^\]]*\])?\{biblatex\}/.test(all)
      biber = usesBiblatex && !/backend\s*=\s*bibtex/.test(all)
      bibtex = !biber && /\\bibliography\{/.test(all)
    } else {
      biber = bib === 'biber'
      bibtex = bib === 'bibtex'
    }
    const makeindex = /\\makeindex\b/.test(all)
    const res = await r.compile(
      [...files].map(([path, content]) => ({ path, content })),
      main, bibtex, biber, makeindex, true, 'silent',
      DRIVERS[(opts.engine ?? 'pdflatex') as keyof typeof DRIVERS] ?? DRIVERS.pdflatex,
      null, (await texliveEndpoint()) ?? undefined,
    )
    let log = res.log ?? ''
    if (!res.success) log = await withTexLog(r, main, log)
    return { pdf: res.success && res.pdf ? res.pdf : null, log, synctex: res.synctex ?? null }
  } finally {
    if (progressCb === onProgress) progressCb = undefined // a stopped compile may end after a new one began
  }
}
