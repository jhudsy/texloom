// In-browser LaTeX compilation via texlyre-busytex (TeX Live 2026 in WebAssembly).
// Engine assets are static files served from /core/busytex/ (the server's
// tex-assets/ directory). The browser caches them in IndexedDB per site, so they
// are downloaded once and shared by every project.
import type { BusyTexRunner } from 'texlyre-busytex'

const BASE = '/core/busytex'
let runner: Promise<BusyTexRunner> | null = null
let available: Promise<boolean> | null = null

export type Progress = { phase: 'download' | 'compile'; percent?: number; loaded?: number; total?: number }

export function browserCompileAvailable(): Promise<boolean> {
  return (available ??= fetch(`${BASE}/busytex.wasm`, { method: 'HEAD' }).then((r) => r.ok).catch(() => false))
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
  runner = null
}

let progressCb: ((p: Progress) => void) | undefined

async function getRunner(): Promise<BusyTexRunner> {
  return (runner ??= (async () => {
    const { BusyTexRunner } = await import('texlyre-busytex')
    const pkgs = ['texlive-basic', 'texlive-recommended', 'texlive-extra']
    const present = await Promise.all(pkgs.map((p) => fetch(`${BASE}/${p}.js`, { method: 'HEAD' }).then((r) => r.ok).catch(() => false)))
    const have = pkgs.filter((_, i) => present[i]).map((p) => `${BASE}/${p}.js`)
    const r = new BusyTexRunner({
      busytexBasePath: BASE,
      preloadDataPackages: have.slice(0, 1), // basic: loaded up front
      catalogDataPackages: have.slice(1), // recommended/extra: fetched only if a document needs them
      onDownloadProgress: (p) => progressCb?.({ phase: 'download', ...p }),
    })
    await r.initialize(true)
    return r
  })().catch((e) => { runner = null; throw e }))
}

const DRIVERS = {
  pdflatex: 'pdftex_bibtex8',
  xelatex: 'xetex_bibtex8_dvipdfmx',
  lualatex: 'luahbtex_bibtex8',
} as const

export async function compileInBrowser(
  files: Map<string, string | Uint8Array>,
  main: string,
  opts: { engine?: string; bib?: string } = {},
  onProgress?: (p: Progress) => void,
): Promise<{ pdf: Uint8Array | null; log: string }> {
  progressCb = onProgress
  try {
    const r = await getRunner()
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
    )
    return { pdf: res.success && res.pdf ? res.pdf : null, log: res.log }
  } finally {
    progressCb = undefined
  }
}
