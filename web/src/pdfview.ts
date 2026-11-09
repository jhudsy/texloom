// Renders a PDF into a scrollable stack of canvases with PDF.js. Keeps the scroll
// position across re-renders so recompiling doesn't jump you back to page 1.
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { t } from './i18n'
import { h } from './dom'
import { openContextMenu, type MenuItem } from './contextmenu'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
export { pdfjs }
const openPdf = (data: Uint8Array) => pdfjs.getDocument({ data: data.slice(), cMapUrl: new URL('/cmaps/', location.href).href, cMapPacked: true })

const ZOOM_STEPS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3] // 1 fits the page to the pane
const MIN_ZOOM = ZOOM_STEPS[0], MAX_ZOOM = ZOOM_STEPS[ZOOM_STEPS.length - 1]
const ZOOM_KEY = 'texloom.pdfZoom'
const savedZoom = () => {
  try {
    const z = Number(localStorage.getItem(ZOOM_KEY))
    return z >= MIN_ZOOM && z <= MAX_ZOOM ? z : 1
  } catch { return 1 }
}
// Zoomed in, a whole document at full sharpness wouldn't fit in a browser's memory for canvases.
const MAX_PAGE_PIXELS = 6e6, MIN_PAGE_PIXELS = 2e6, MAX_DOCUMENT_PIXELS = 2e8
// Printing draws its own copy of each page. The site's security rules allow no frames, which
// handing the PDF to the browser's own viewer for printing would need.
const PRINT_DPI = 300, MAX_PRINT_PIXELS = 16e6

export class PdfView {
  readonly el: HTMLDivElement
  /** Double-click or “Go to source” on a page: page number (1-based) and position in PDF points from the top-left. */
  onPick: ((page: number, x: number, y: number) => void) | null = null
  /** More right-click items, after “Go to source here”. */
  menuExtras: (() => MenuItem[]) | null = null
  /** Where the PDF was last clicked (what “go to source” uses), in PDF points. */
  private lastClick: { page: number; x: number; y: number } | null = null
  private menu: (() => void) | null = null
  /** Which page, zoom out, the current zoom (which goes back to normal size), zoom in, print. Shown once there is a PDF. */
  readonly controls: HTMLElement
  private pageBox: HTMLInputElement
  private pageTotal: HTMLElement
  private printBtn: HTMLButtonElement
  private pageFrame = 0
  private zoomOut: HTMLButtonElement
  private zoomReset: HTMLButtonElement
  private zoomIn: HTMLButtonElement
  private zoom = savedZoom()
  private zoomTimer: ReturnType<typeof setTimeout> | undefined
  private pages: { wrap: HTMLDivElement; scale: number; canvas: HTMLCanvasElement; points: number }[] = []
  private gen = 0
  private last: Uint8Array | null = null
  private renderedWidth = 0
  constructor() {
    this.el = document.createElement('div')
    this.el.className = 'pdf'
    // Re-render at the new width after the pane is resized (canvases scale via CSS meanwhile).
    let timer: ReturnType<typeof setTimeout> | undefined
    new ResizeObserver(() => {
      const w = this.el.clientWidth
      if (!this.last || !w || Math.abs(w - this.renderedWidth) < 24) return
      clearTimeout(timer)
      timer = setTimeout(() => { if (this.last) this.show(this.last).catch(() => {}) }, 250)
    }).observe(this.el)
    this.el.addEventListener('pointerdown', (e) => { if (e.button === 0) this.lastClick = this.pointAt(e) ?? this.lastClick })
    this.el.addEventListener('dblclick', (e) => {
      const at = this.pointAt(e)
      if (at) this.onPick?.(at.page, at.x, at.y)
    })
    // right-click: a small menu (the canvas has nothing useful in the browser's own)
    this.el.addEventListener('contextmenu', (e) => {
      const at = this.pointAt(e)
      if (!at || !this.onPick) return
      e.preventDefault()
      this.lastClick = at
      this.openMenu(e.clientX, e.clientY, [[t('Go to source here'), () => this.onPick?.(at.page, at.x, at.y)], ...(this.menuExtras?.() ?? [])])
    })
    this.el.addEventListener('scroll', () => {
      this.closeMenu()
      this.pageFrame ||= requestAnimationFrame(() => { this.pageFrame = 0; this.showPage() })
    }, { passive: true })
    // Ctrl or ⌘ + scroll; a trackpad pinch arrives as Ctrl + scroll
    this.el.addEventListener('wheel', (e) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      const box = this.el.getBoundingClientRect()
      this.setZoom(this.zoom * Math.exp(-e.deltaY * 0.01), e.clientX - box.left, e.clientY - box.top)
    }, { passive: false })
    this.zoomOut = h('button', { title: t('Zoom out'), 'aria-label': t('Zoom out'), onclick: () => this.step(-1) }, '−')
    this.zoomReset = h('button', { class: 'pct', title: t('Back to normal size'), onclick: () => this.setZoom(1) })
    this.zoomIn = h('button', { title: t('Zoom in'), 'aria-label': t('Zoom in'), onclick: () => this.step(1) }, '+')
    this.pageBox = h('input', {
      class: 'pagebox', inputmode: 'numeric', autocomplete: 'off', title: t('Go to page'), 'aria-label': t('Go to page'),
      onfocus: () => this.pageBox.select(),
      onblur: () => this.showPage(),
      onkeydown: (e: KeyboardEvent) => {
        if (e.key === 'Enter') { e.preventDefault(); this.goToPage(Number(this.pageBox.value)); this.pageBox.blur() }
        else if (e.key === 'Escape') this.pageBox.blur()
      },
    })
    this.pageTotal = h('span', { class: 'muted' })
    this.printBtn = h('button', { title: t('Print the PDF'), onclick: () => { this.print().catch((e) => { this.printDone(); alert(t('Couldn’t prepare the PDF for printing: {{error}}', { error: e })) }) } }, t('Print'))
    this.controls = h('span', { class: 'pdfcontrols', hidden: true },
      h('span', { class: 'pdfpage' }, this.pageBox, this.pageTotal),
      h('span', { class: 'pdfzoom', role: 'group', 'aria-label': t('PDF zoom') }, this.zoomOut, this.zoomReset, this.zoomIn),
      this.printBtn)
    this.showZoom()
  }

  /** The page in the middle of the view. */
  private currentPage(): number {
    const mid = this.el.getBoundingClientRect().top + this.el.clientHeight / 2
    const i = this.pages.findIndex((p) => p.wrap.getBoundingClientRect().bottom >= mid)
    return i < 0 ? this.pages.length : i + 1
  }

  private showPage() {
    if (this.pages.length && document.activeElement !== this.pageBox) this.pageBox.value = String(this.currentPage())
  }

  private goToPage(n: number) {
    if (!Number.isInteger(n) || !this.pages.length) return
    const p = this.pages[Math.min(this.pages.length, Math.max(1, n)) - 1]
    this.el.scrollTop += p.wrap.getBoundingClientRect().top - this.el.getBoundingClientRect().top - 12
  }

  /** Draw every page for the printer and open the browser's print dialog. */
  private async print() {
    if (!this.last || this.printBtn.disabled) return
    this.printBtn.disabled = true
    const label = this.printBtn.textContent
    this.printDone = () => { this.printBtn.disabled = false; this.printBtn.textContent = label }
    const task = openPdf(this.last)
    const sheets = h('div', { class: 'print-sheets' })
    const urls: string[] = []
    let size = ''
    try {
      const doc = await task.promise
      for (let i = 1; i <= doc.numPages; i++) {
        this.printBtn.textContent = t('Preparing… ({{done}} of {{total}})', { done: i, total: doc.numPages })
        const page = await doc.getPage(i)
        const base = page.getViewport({ scale: 1 })
        if (i === 1) size = `${base.width}pt ${base.height}pt`
        const vp = page.getViewport({ scale: Math.min(PRINT_DPI / 72, Math.sqrt(MAX_PRINT_PIXELS / (base.width * base.height))) })
        const canvas = document.createElement('canvas')
        canvas.width = Math.floor(vp.width)
        canvas.height = Math.floor(vp.height)
        await page.render({ canvas, viewport: vp, intent: 'print' }).promise
        const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, 'image/png'))
        canvas.width = canvas.height = 0 // a long document won't fit in memory as canvases
        if (!blob) throw new Error('canvas')
        const url = URL.createObjectURL(blob)
        urls.push(url)
        sheets.append(h('img', { src: url, alt: '' }))
      }
    } finally { void task.destroy() }
    const style = h('style', {}, `@media print { @page { size: ${size}; margin: 0; } }`)
    document.head.append(style)
    document.body.append(sheets)
    await Promise.all([...sheets.querySelectorAll('img')].map((img) => img.decode()))
    document.body.classList.add('printing')
    window.addEventListener('afterprint', () => {
      document.body.classList.remove('printing')
      sheets.remove()
      style.remove()
      urls.forEach((url) => URL.revokeObjectURL(url))
      this.printDone()
    }, { once: true })
    window.print()
  }
  private printDone: () => void = () => {}

  private showZoom() {
    this.zoomReset.textContent = `${Math.round(this.zoom * 100)}%`
    this.zoomOut.disabled = this.zoom <= MIN_ZOOM
    this.zoomIn.disabled = this.zoom >= MAX_ZOOM
    this.el.classList.toggle('zoomed', this.zoom > 1) // wider than the pane: scrolls sideways
  }

  private step(direction: 1 | -1) {
    const next = direction > 0 ? ZOOM_STEPS.find((z) => z > this.zoom + 0.001) : [...ZOOM_STEPS].reverse().find((z) => z < this.zoom - 0.001)
    if (next) this.setZoom(next)
  }

  /** Width of a page at this zoom, in CSS pixels. A very narrow pane scrolls sideways instead of failing. */
  private pageWidth() { return Math.max(160, this.el.clientWidth - 24) * this.zoom }

  /** Resize what's on show at once, keeping (x, y) in the pane where it is, and draw it sharply a moment later. */
  private setZoom(zoom: number, x = this.el.clientWidth / 2, y = this.el.clientHeight / 2) {
    this.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom))
    try { localStorage.setItem(ZOOM_KEY, String(this.zoom)) } catch {}
    const across = (this.el.scrollLeft + x) / Math.max(1, this.el.scrollWidth)
    const down = (this.el.scrollTop + y) / Math.max(1, this.el.scrollHeight)
    this.showZoom()
    if (!this.pages.length) return
    const width = this.pageWidth()
    for (const p of this.pages) {
      p.canvas.style.width = `${Math.floor(width)}px`
      p.scale = width / p.points
    }
    this.el.scrollLeft = across * this.el.scrollWidth - x
    this.el.scrollTop = down * this.el.scrollHeight - y
    clearTimeout(this.zoomTimer)
    this.zoomTimer = setTimeout(() => { if (this.last) this.show(this.last).catch(() => {}) }, 300)
  }

  /** The page and PDF-point position under a mouse event, if it's on a page. */
  private pointAt(e: MouseEvent): { page: number; x: number; y: number } | null {
    const wrap = (e.target as HTMLElement).closest?.('.page') as HTMLDivElement | null
    const i = this.pages.findIndex((p) => p.wrap === wrap)
    if (i < 0) return null
    const rect = wrap!.getBoundingClientRect()
    const s = this.pages[i].scale
    return { page: i + 1, x: (e.clientX - rect.left) / s, y: (e.clientY - rect.top) / s }
  }

  /** The point “go to source” should use: the last click if it's still in view, else the middle of the view. */
  focusPoint(): { page: number; x: number; y: number } | null {
    if (!this.pages.length) return null
    const view = this.el.getBoundingClientRect()
    const visible = (page: number, y: number) => {
      const p = this.pages[page - 1]
      if (!p) return false
      const top = p.wrap.getBoundingClientRect().top + y * p.scale
      return top >= view.top && top <= view.bottom
    }
    if (this.lastClick && visible(this.lastClick.page, this.lastClick.y)) return this.lastClick
    const mid = view.top + view.height / 2
    let best = 0, dist = Infinity
    this.pages.forEach((p, i) => {
      const r = p.wrap.getBoundingClientRect()
      const d = mid < r.top ? r.top - mid : mid > r.bottom ? mid - r.bottom : 0
      if (d < dist) { dist = d; best = i }
    })
    const p = this.pages[best], r = p.wrap.getBoundingClientRect()
    const y = Math.max(0, Math.min(r.height, mid - r.top)) / p.scale
    return { page: best + 1, x: r.width / 2 / p.scale, y }
  }

  private openMenu(x: number, y: number, items: MenuItem[]) {
    this.closeMenu()
    this.menu = openContextMenu(x, y, items, () => { this.menu = null })
  }
  private closeMenu() { this.menu?.() }

  /** Briefly highlight a box (PDF points from the page's top-left) and scroll it into view. */
  highlight(page: number, x: number, y: number, w: number, h: number) {
    const p = this.pages[page - 1]
    if (!p) return
    const mark = document.createElement('div')
    mark.className = 'pdf-mark'
    Object.assign(mark.style, { left: `${(x - 2) * p.scale}px`, top: `${(y - 2) * p.scale}px`, width: `${(w + 4) * p.scale}px`, height: `${(h + 4) * p.scale}px` })
    p.wrap.append(mark)
    mark.scrollIntoView({ block: 'center', behavior: 'smooth' })
    setTimeout(() => mark.remove(), 2500)
  }

  async show(data: Uint8Array) {
    this.last = data
    const gen = ++this.gen
    const task = openPdf(data)
    const doc = await task.promise
    try {
      if (gen !== this.gen) return
    const scrollFrac = this.el.scrollTop / Math.max(1, this.el.scrollHeight)
    const scrollAcross = this.el.scrollLeft / Math.max(1, this.el.scrollWidth)
    if (!this.el.clientWidth) return // hidden: render when it becomes visible
    this.renderedWidth = this.el.clientWidth
    const width = this.pageWidth()
    const maxPixels = Math.min(MAX_PAGE_PIXELS, Math.max(MIN_PAGE_PIXELS, MAX_DOCUMENT_PIXELS / doc.numPages))
    const pages = document.createDocumentFragment()
    const info: typeof this.pages = []
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i)
      const base = page.getViewport({ scale: 1 })
      const vp = page.getViewport({ scale: width / base.width })
      const dpr = Math.min(window.devicePixelRatio || 1, Math.sqrt(maxPixels / (vp.width * vp.height)))
      const canvas = document.createElement('canvas')
      canvas.width = Math.floor(vp.width * dpr)
      canvas.height = Math.floor(vp.height * dpr)
      canvas.style.width = `${Math.floor(vp.width)}px`
      canvas.style.height = `${Math.floor(vp.height)}px`
      await page.render({ canvas, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined }).promise
      if (gen !== this.gen) return
      const wrap = document.createElement('div')
      wrap.className = 'page'
      wrap.append(canvas)
      pages.append(wrap)
      info.push({ wrap, scale: vp.width / base.width, canvas, points: base.width })
    }
    this.pages = info
    this.el.replaceChildren(pages)
    this.el.scrollTop = scrollFrac * this.el.scrollHeight
    this.el.scrollLeft = scrollAcross * this.el.scrollWidth
    this.controls.hidden = false
    this.pageTotal.textContent = `/ ${info.length}`
    this.showPage()
    } finally { void task.destroy() } // otherwise each document keeps a worker for the life of the tab
  }
}
