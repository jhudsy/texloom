// Compiling and the PDF pane: PDF view, download, progress, and the compile log
// underneath (resizable, toggled with "Log").
import type { Replica } from './replica'
import type { Device } from './settings'
import { h, saveFile, verticalResizer } from './dom'
import { PdfView } from './pdfview'
import { compileInBrowser, browserCompileAvailable, type Progress } from './compile'
import { fileBase, mainFile } from './projectfiles'

export type CompileOut = { ok: boolean; log: string } | null

export interface Output {
  pane: HTMLElement
  compileBtn: HTMLButtonElement
  compile(interactive?: boolean): Promise<CompileOut>
  lastLog(): string
  pdfView: PdfView
}

export function createOutput(r: Replica, writable: boolean, device: () => Device, userName: () => string): Output {
  const pdfView = new PdfView()
  const info = h('div', { class: 'pdfinfo muted' }, 'No PDF yet.')
  const progress = h('div', { class: 'progress', hidden: true }, h('div', { class: 'bar' }), h('span', {}))
  const logPre = h('pre', { class: 'log' })
  const logSplit = h('div', { class: 'hsplit' })
  const logBox = h('div', { class: 'logbox' }, logSplit, logPre)
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
  const logBtn = h('button', { title: 'Show/hide the compile log', onclick: () => setLogOpen(!!logBox.hidden) }, 'Log')
  const dlBtn = h('button', { title: 'Download the PDF', disabled: true, onclick: () => lastPdf && saveFile(`${fileBase(r)}.pdf`, lastPdf, 'application/pdf') }, '⤓ PDF')
  const compileBtn = h('button', { class: 'primary', title: 'Compile (⌘S / Ctrl+S)', onclick: () => compile() }, 'Compile')

  const pane = h('div', { class: 'pane output' },
    h('div', { class: 'pdfbar' }, info, h('span', { class: 'spacer' }), dlBtn, logBtn),
    progress, pdfView.el, logBox)
  verticalResizer(logSplit, logBox, 'texloom.logHeight', 60, 180)
  setLogOpen(localStorage.getItem(LOG_KEY) === '1')

  function show(pdf: Uint8Array | null, log: string, text: string, ok: boolean) {
    if (pdf) {
      lastPdf = pdf
      pdfError = null
      dlBtn.removeAttribute('disabled')
      pdfView.show(pdf).catch((e) => { console.error('PDF render failed', e); pdfError = `PDF error: ${e}`; info.textContent = pdfError })
    }
    logPre.textContent = log
    lastLog = log
    info.textContent = pdfError ?? text
    if (!ok) setLogOpen(true)
  }

  // collaborators' (or the folder helper's) latest published output
  const renderLatest = async () => {
    const o = r.out.get('latest')
    if (!o) return
    const dec = new TextDecoder()
    const [pdf, log] = await Promise.all([o.pdf ? r.getBlob(o.pdf) : null, o.log ? r.getBlob(o.log).then((b) => dec.decode(b)) : ''])
    show(pdf, log, `${o.ok ? '✓' : '✗ errors'} · ${o.by} · ${new Date(o.at * 1000).toLocaleTimeString()}`, !!o.ok)
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
      text.textContent = `Downloading TeX (once per browser) — ${Math.round(p.percent ?? 0)}%` + (p.total ? ` · ${MB(p.loaded)} / ${MB(p.total)} MB` : '')
    } else {
      bar.style.width = '100%'
      text.textContent = label ?? 'Compiling…'
    }
    progress.classList.toggle('busy', p.phase === 'compile')
  }

  /** Compile per this device's setting. `interactive` shows alerts (button, ⌘S) instead of returning them (assistant). */
  async function compile(interactive = true): Promise<CompileOut> {
    const fail = (msg: string): CompileOut => { if (interactive) alert(msg); return { ok: false, log: msg } }
    const d = device()
    if (d.compile === 'none') return interactive ? fail('Compiling is turned off on this device (⚙ Settings → Compiling).') : null
    const main = mainFile(r)
    if (!main) return fail('No .tex file with \\documentclass found.')
    if (compileBtn.hasAttribute('disabled')) return fail('A compile is already running; try again in a moment.')
    if (d.compile === 'local') return compileLocally(fail)
    if (!(await browserCompileAvailable())) {
      return fail('In-browser compilation is not installed on this server.\n\nChoose “Compile with my own TeX” in ⚙ Settings and run:\n  texloom run <folder> --compile')
    }
    compileBtn.textContent = 'Compiling…'; compileBtn.setAttribute('disabled', '')
    showProgress({ phase: 'download', percent: 0 })
    try {
      const t0 = performance.now()
      const res = await compileInBrowser(await r.snapshotFiles(), main,
        { engine: r.meta.get('engine') || 'pdflatex', bib: r.meta.get('bib') || 'auto' }, (p) => showProgress(p))
      const secs = ((performance.now() - t0) / 1000).toFixed(1)
      show(res.pdf, res.log, `${res.pdf ? '✓' : '✗ errors'} · in browser · ${secs}s`, !!res.pdf)
      if (writable && res.pdf) { // publish so collaborators see it without compiling
        const [pdfId, logId] = await Promise.all([r.putBlob(res.pdf), r.putBlob(new TextEncoder().encode(res.log))])
        r.out.set('latest', { pdf: pdfId, log: logId, ok: true, by: `${userName()} (browser)`, at: Date.now() / 1000, main })
      }
      return { ok: !!res.pdf, log: res.log }
    } catch (e) {
      return fail(`Compile failed: ${e}`)
    } finally {
      showProgress(null)
      compileBtn.textContent = 'Compile'; compileBtn.removeAttribute('disabled')
    }
  }

  async function compileLocally(fail: (m: string) => CompileOut): Promise<CompileOut> {
    if (!(await r.requestCompile())) return fail('Not connected, so no local helper can be reached.')
    showProgress({ phase: 'compile' }, 'Asking your texloom helper to compile…')
    let acked = false
    r.onCompileAck = (p) => { acked = true; showProgress({ phase: 'compile' }, `Compiling with ${p.by}…`) }
    const before = r.out.get('latest')?.at
    try {
      for (let i = 0; i < 240; i++) {
        await new Promise((res) => setTimeout(res, 250))
        if (r.out.get('latest')?.at !== before) break
        if (i === 20 && !acked) return fail('No texloom helper with --compile answered.\n\nOn a computer with TeX, run:\n  texloom run <folder> --compile')
      }
    } finally {
      showProgress(null)
    }
    const o = r.out.get('latest')
    if (!o || o.at === before) return fail('The local helper did not finish compiling within 60 s.')
    const log = o.log ? new TextDecoder().decode(await r.getBlob(o.log)) : ''
    return { ok: !!o.ok, log }
  }

  // optional auto-compile after a pause in typing (local edits only)
  let autoTimer: ReturnType<typeof setTimeout> | undefined
  r.files.observeDeep((_e, tr) => {
    const d = device()
    if (!d.autoCompile || d.compile === 'none' || typeof tr.origin === 'symbol') return
    clearTimeout(autoTimer)
    autoTimer = setTimeout(() => { if (!compileBtn.hasAttribute('disabled')) compile() }, 2500)
  })

  return { pane, compileBtn, compile, lastLog: () => lastLog, pdfView }
}
