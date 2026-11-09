// Compiling and the PDF pane: PDF view, download, progress, and the compile log
// underneath (resizable, toggled with "Log").
import type { Replica } from './replica'
import type { Device } from './settings'
import { h, saveFile, verticalResizer } from './dom'
import { PdfView } from './pdfview'
import { pdfWindowLink } from './pdfwindow'
import { compileInBrowser, defaultEngine, browserCompileAvailable, type Progress, CompileStopped, stopBrowserCompile } from './compile'
import { allFiles, fileBase, mainFile } from './projectfiles'
import { parseLog, type Problem } from './latexlog'
import { SyncTex } from './synctex'
import { t, tn, locale } from './i18n'

export type CompileOut = { ok: boolean; log: string } | null
type Fail = (msg: (tr: typeof t) => string) => CompileOut
/** t() without translating: the English text with its placeholders filled. */
const english: typeof t = (en, vars) => (vars ? en.replace(/\{\{(\w+)\}\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : en)

export interface Output {
  pane: HTMLElement
  compileBtn: HTMLButtonElement
  compile(interactive?: boolean): Promise<CompileOut>
  lastLog(): string
  pdfView: PdfView
  problems(): Problem[]
  onProblems: (() => void)[]
  showInPdf(file: string, line: number): boolean
  /** Jump to the source of the last-clicked PDF point (or the middle of the view); false without SyncTeX. */
  showInSource(): boolean
  /** Set by the host: called by the ⇥ button to show the editor's current line. */
  syncRequest: (() => void) | null
}

export function createOutput(r: Replica, writable: boolean, device: () => Device, userName: () => string,
  goto: (file: string, line: number, flash?: boolean) => void): Output {
  const pdfView = new PdfView()
  const pdfWindow = pdfWindowLink(r.keys.project, () => String(r.meta.get('name') ?? ''), (at) => pdfView.onPick?.(at.page, at.x, at.y))
  const info = h('div', { class: 'pdfinfo muted' }, t('No PDF yet.'))
  const progress = h('div', { class: 'progress', hidden: true }, h('div', { class: 'bar' }), h('span', {}))
  const logPre = h('pre', { class: 'log', hidden: true })
  const problemList = h('ul', { class: 'problems' })
  const logSplit = h('div', { class: 'hsplit' })
  const probTab = h('button', { class: 'ltab on', onclick: () => showLogView(false) }, t('Problems'))
  const rawTab = h('button', { class: 'ltab', onclick: () => showLogView(true) }, t('Raw log'))
  const scratchBtn = h('button', { class: 'scratch', title: t('Delete LaTeX’s working files (.aux, .bbl, …) and compile again. In-browser compiles always start from scratch; this matters when compiling with your own TeX.'),
    onclick: () => compile(true, true) }, '↻ ', t('Compile from scratch'))
  const logBox = h('div', { class: 'logbox' }, logSplit, h('div', { class: 'ltabs' }, probTab, rawTab, h('span', { class: 'spacer' }), scratchBtn), problemList, logPre)
  const showLogView = (raw: boolean) => {
    logPre.hidden = !raw; problemList.hidden = raw
    probTab.classList.toggle('on', !raw); rawTab.classList.toggle('on', raw)
  }
  let problems: Problem[] = []
  const onProblems: (() => void)[] = []
  let synctex: SyncTex | null = null
  let synctexBytes: Uint8Array | null = null
  let lastPdf: Uint8Array | null = null
  let pdfError: string | null = null
  let lastLog = ''

  const LOG_KEY = 'texloom.logOpen'
  const setLogOpen = (open: boolean) => {
    logBox.hidden = !open
    logBtn.classList.toggle('on', open)
    logBtn.setAttribute('aria-pressed', String(open))
    try { localStorage.setItem(LOG_KEY, open ? '1' : '') } catch {}
  }
  const logBtn = h('button', { title: t('Show/hide the compile log'), onclick: () => setLogOpen(!!logBox.hidden) }, t('Log'))
  const dlBtn = h('button', { title: t('Download the PDF'), disabled: true, onclick: () => lastPdf && saveFile(`${fileBase(r)}.pdf`, lastPdf, 'application/pdf') }, '⤓ PDF')
  const compileBtn = h('button', { class: 'primary', title: t('Compile (⌘S / Ctrl+S)'), onclick: (e: Event) => {
    if (stopRunning) { e.stopImmediatePropagation(); stopRunning() } else compile() // stopping shouldn't change what's on show
  } }, t('Compile')) as HTMLButtonElement
  let stopRunning: (() => void) | null = null
  /** While something is compiling, the Compile button stops it. */
  const setRunning = (stop: (() => void) | null, label = t('Stop'), title = t('Stop this compile')) => {
    stopRunning = stop
    compileBtn.textContent = stop ? label : t('Compile')
    compileBtn.title = stop ? title : t('Compile (⌘S / Ctrl+S)')
    compileBtn.classList.toggle('primary', !stop)
    compileBtn.classList.toggle('danger', !!stop)
  }
  const windowBtn = h('button', { class: 'to-window wide-only', title: t('Open the PDF in a separate window, for example on another screen. It updates when you compile.'), onclick: () => {
    if (!pdfWindow.open()) info.textContent = t('Your browser blocked the new window: allow pop-ups for this site and try again.')
  } }, `↗ ${t('New window')}`)
  const summary = h('button', { class: 'psummary', title: t('Show problems'), hidden: true, onclick: () => { setLogOpen(true); showLogView(false) } })

  const pane = h('div', { class: 'pane output' },
    h('div', { class: 'pdfbar' }, info, summary, h('span', { class: 'spacer' }), pdfView.controls, windowBtn, dlBtn, logBtn),
    progress, pdfView.el, logBox)
  verticalResizer(logSplit, logBox, 'texloom.logHeight', 60, 180)
  setLogOpen(localStorage.getItem(LOG_KEY) === '1')

  // double-click or right-click → “Go to source” in the PDF: jump to the source line
  pdfView.menuExtras = () => [[t('Download the PDF'), () => dlBtn.click()], [t('Compile'), () => { compile() }]]
  pdfView.onPick = async (page, x, y) => {
    const st = await syncTex()
    const hit = st?.toSource(page, x, y)
    if (hit) goto(hit.file, hit.line, true)
    else info.textContent = st ? t('No source line for that spot.') : t('Compile first: the PDF has no SyncTeX data yet.')
  }
  async function syncTex(): Promise<SyncTex | null> {
    if (!synctex && synctexBytes) synctex = await SyncTex.load(synctexBytes, allFiles(r)).catch(() => null)
    return synctex
  }

  function renderProblems() {
    const n = { error: 0, warning: 0, box: 0 }
    for (const p of problems) n[p.severity]++
    const parts = [n.error && tn(n.error, '✗ {{n}} error', '✗ {{n}} errors'), n.warning && tn(n.warning, '⚠ {{n}} warning', '⚠ {{n}} warnings'), n.box && tn(n.box, '{{n}} bad box', '{{n}} bad boxes')].filter(Boolean)
    summary.hidden = !parts.length
    summary.textContent = parts.join(' · ')
    summary.classList.toggle('has-errors', n.error > 0)
    problemList.replaceChildren(...(problems.length ? problems.map((p) => h('li', {
      class: `${p.severity} ${p.file ? 'clickable' : ''}`,
      title: p.file ? t('Go to this line') : '',
      onclick: () => { if (p.file) goto(p.file, p.line ?? 1) },
    }, h('span', { class: 'sev' }, p.severity === 'error' ? '✗' : p.severity === 'warning' ? '⚠' : '▭'),
      h('span', { class: 'msg' }, p.message),
      p.file ? h('span', { class: 'loc' }, `${p.file}${p.line ? `:${p.line}` : ''}`) : null))
      : [h('li', { class: 'muted' }, lastLog ? t('No errors or warnings.') : t('No compile log yet.'))]))
    for (const fn of onProblems) fn()
  }

  function show(pdf: Uint8Array | null, log: string, text: string, ok: boolean, stx?: Uint8Array | null) {
    if (stx !== undefined) { synctexBytes = stx; synctex = null }
    if (pdf) {
      lastPdf = pdf
      pdfError = null
      dlBtn.removeAttribute('disabled')
      pdfView.show(pdf).catch((e) => { console.error('PDF render failed', e); pdfError = t('PDF error: {{error}}', { error: e }); info.textContent = pdfError })
      pdfWindow.pdf(pdf)
    }
    logPre.textContent = log
    lastLog = log
    info.textContent = pdfError ?? text
    problems = parseLog(log, allFiles(r))
    const unexplained = !ok && !problems.some((p) => p.severity === 'error')
    if (unexplained) problems.unshift({ severity: 'error', file: null, line: null, message: t('The compile failed without a LaTeX error message: the raw log shows what happened.') })
    renderProblems()
    if (!ok) { setLogOpen(true); showLogView(unexplained) }
  }

  // collaborators' (or the folder helper's) latest published output
  const renderLatest = async () => {
    const o = r.out.get('latest')
    if (!o) return
    const dec = new TextDecoder()
    const [pdf, log, stx] = await Promise.all([
      o.pdf ? r.getBlob(o.pdf) : null,
      o.log ? r.getBlob(o.log).then((b) => dec.decode(b)) : '',
      o.synctex ? r.getBlob(o.synctex).catch(() => null) : null,
    ])
    show(pdf, log, `${o.ok ? '✓' : t('✗ errors')} · ${o.by} · ${new Date(o.at * 1000).toLocaleTimeString(locale)}`, !!o.ok, stx)
  }
  r.out.observe(() => renderLatest().catch(console.error))
  renderLatest().catch(console.error)

  const MB = (b?: number) => ((b ?? 0) / 1048576).toFixed(0)
  function showProgress(p: Progress | null, label?: string) {
    progress.hidden = !p
    if (!p) return
    const bar = progress.firstChild as HTMLElement, text = progress.lastChild as HTMLElement
    if (p.phase === 'download') {
      bar.style.width = `${p.percent ?? 0}%`
      text.textContent = t('Downloading TeX (once per browser) — {{percent}}%', { percent: Math.round(p.percent ?? 0) }) + (p.total ? ` · ${t('{{loaded}} / {{total}} MB', { loaded: MB(p.loaded), total: MB(p.total) })}` : '')
    } else {
      bar.style.width = '100%'
      text.textContent = label ?? t('Compiling…')
    }
    progress.classList.toggle('busy', p.phase === 'compile')
  }

  /** Compile per this device's setting. `interactive` shows alerts (button, ⌘S) instead of returning them (assistant).
   *  A failure message is a function of the translator: alerts are in the interface language, while the
   *  returned log (read by the assistant) stays in English. */
  async function compile(interactive = true, fromScratch = false): Promise<CompileOut> {
    const fail: Fail = (msg) => {
      if (interactive) alert(msg(t))
      return { ok: false, log: msg(english) }
    }
    const d = device()
    if (d.compile === 'none') return interactive ? fail((t) => t('Compiling is turned off on this device (⚙ Settings → Compiling).')) : null
    const main = mainFile(r)
    if (!main) return fail((t) => t('No .tex file with \\documentclass found.'))
    if (stopRunning) return fail((t) => t('A compile is already running; try again in a moment.'))
    if (d.compile === 'local') return compileLocally(fail, fromScratch)
    if (!(await browserCompileAvailable())) {
      return fail((t) => t('In-browser compilation is not installed on this server.\n\nChoose “Compile with my own TeX” in ⚙ Settings and run:\n  texloom run <folder> --compile'))
    }
    setRunning(stopBrowserCompile)
    showProgress({ phase: 'download', percent: 0 })
    try {
      const t0 = performance.now()
      const files = await r.snapshotFiles()
      const res = await compileInBrowser(files, main,
        { engine: r.meta.get('engine') || defaultEngine(files), bib: r.meta.get('bib') || 'auto' }, (p) => showProgress(p))
      const secs = ((performance.now() - t0) / 1000).toFixed(1)
      show(res.pdf, res.log, `${res.pdf ? '✓' : t('✗ errors')} · ${t('in browser')} · ${t('{{secs}}s', { secs })}`, !!res.pdf, res.synctex)
      if (writable && res.pdf) { // publish so collaborators see it without compiling
        const [pdfId, logId, stxId] = await Promise.all([r.putBlob(res.pdf, 'output'), r.putBlob(new TextEncoder().encode(res.log), 'output'),
          res.synctex ? r.putBlob(res.synctex, 'output') : null])
        r.out.set('latest', { pdf: pdfId, log: logId, ...(stxId ? { synctex: stxId } : {}), ok: true, by: t('{{name}} (browser)', { name: userName() }), at: Date.now() / 1000, main })
      }
      return { ok: !!res.pdf, log: res.log }
    } catch (e) {
      if (e instanceof CompileStopped) {
        info.textContent = t('Compile stopped.')
        return { ok: false, log: 'The compile was stopped.' }
      }
      return fail((t) => t('Compile failed: {{error}}', { error: e }))
    } finally {
      showProgress(null)
      setRunning(null)
    }
  }

  async function compileLocally(fail: Fail, fromScratch = false): Promise<CompileOut> {
    if (!(await r.requestCompile(fromScratch))) return fail((t) => t('Not connected, so no local helper can be reached.'))
    showProgress({ phase: 'compile' }, t('Asking your texloom helper to compile…'))
    let acked = false
    r.onCompileAck = (p) => { acked = true; showProgress({ phase: 'compile' }, t('Compiling with {{name}}…', { name: p.by })) }
    const before = r.out.get('latest')?.at
    let stopped = false
    setRunning(() => { stopped = true }, t('Stop waiting'), t('Stop waiting for your texloom helper. It finishes any compile it has started.'))
    try {
      for (let i = 0; i < 240; i++) {
        await new Promise((res) => setTimeout(res, 250))
        if (stopped) return { ok: false, log: 'Stopped waiting for the texloom helper.' }
        if (r.out.get('latest')?.at !== before) break
        if (i === 20 && !acked) return fail((t) => t('No texloom helper with --compile answered.\n\nOn a computer with TeX, run:\n  texloom run <folder> --compile'))
      }
    } finally {
      showProgress(null)
      setRunning(null)
    }
    const o = r.out.get('latest')
    if (!o || o.at === before) return fail((t) => t('The local helper did not finish compiling within 60 s.'))
    const log = o.log ? new TextDecoder().decode(await r.getBlob(o.log)) : ''
    return { ok: !!o.ok, log }
  }

  // optional auto-compile after a pause in typing (local edits only)
  let autoTimer: ReturnType<typeof setTimeout> | undefined
  r.files.observeDeep((_e, tr) => {
    const d = device()
    if (!d.autoCompile || d.compile === 'none' || typeof tr.origin === 'symbol') return
    clearTimeout(autoTimer)
    autoTimer = setTimeout(() => { if (!stopRunning) compile() }, 2500)
  })

  function showInPdf(file: string, line: number): boolean {
    if (!synctexBytes) return false
    syncTex().then((st) => {
      const at = st?.toPdf(file, line)
      if (at) { pdfView.highlight(at.page, at.x, at.y, at.w, at.h); pdfWindow.mark(at.page, at.x, at.y, at.w, at.h) }
      else info.textContent = t('That line isn’t in the PDF (yet): compile first?')
    })
    return true
  }
  function showInSource(): boolean {
    const at = pdfView.focusPoint()
    if (!synctexBytes || !at) return false
    pdfView.onPick?.(at.page, at.x, at.y)
    return true
  }
  const out: Output = {
    pane, compileBtn, compile, lastLog: () => lastLog, pdfView, problems: () => problems, onProblems, showInPdf, showInSource, syncRequest: null,
  }
  const backBtn = h('button', { class: 'narrow-only', title: t('Go to the source of the spot you last clicked in the PDF, or the middle of the view (⌘⌥← / Ctrl+Alt+←). Or double-click / right-click the PDF.'), 'aria-label': t('Go to source'), onclick: () => { if (!showInSource()) info.textContent = t('Compile first: the PDF has no SyncTeX data yet.') } }, '⇤')
  const syncBtn = h('button', { class: 'narrow-only', title: t('Show the editor’s line in the PDF (⌘⌥→ / Ctrl+Alt+→)'), 'aria-label': t('Show in PDF'), onclick: () => out.syncRequest?.() }, '⇥')
  dlBtn.before(backBtn, syncBtn) // small windows only: the arrows on the divider aren't shown there
  renderProblems()
  return out
}
