// Renders a PDF into a scrollable stack of pages with PDF.js. Every page has its place and size,
// but only those on or near the screen are drawn (a 200-page thesis at full sharpness wouldn't fit
// in memory), and the document stays open between redraws. Keeps the scroll position across
// recompiles, so they don't jump you back to page 1.
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { t } from './i18n'
import { h } from './dom'
import { icon } from './icons'
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
const MAX_PAGE_PIXELS = 6e6 // one page's canvas, however far zoomed in
const NEAR = 1, FAR = 3 // screens above and below: pages this near are drawn; this far, their canvases are freed
const GAP = 12 // px between pages, and around them (the .pdf padding and gap)
// Printing draws its own copy of each page. The site's security rules allow no frames, which
// handing the PDF to the browser's own viewer for printing would need. 200 dpi prints text well
// and keeps a long document's pages within memory.
const PRINT_DPI = 200, MAX_PRINT_PIXELS = 8e6

/** A canvas with no pixels (a new one has 300 × 150), standing in for a page not drawn. */
const emptyCanvas = () => { const c = document.createElement('canvas'); c.width = c.height = 0; return c }

interface Page { wrap: HTMLDivElement; canvas: HTMLCanvasElement; points: number; ratio: number; drawn: number }

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
  private pages: Page[] = []
  private gen = 0
  private last: Uint8Array | null = null
  private doc: pdfjs.PDFDocumentProxy | null = null
  private task: pdfjs.PDFDocumentLoadingTask | null = null
  private width = 0 // the pages' width now, in CSS pixels
  private drawing = false
  private renderedWidth = 0
  constructor() {
    this.el = document.createElement('div')
    this.el.className = 'pdf'
    // Re-render at the new width after the pane is resized (canvases scale via CSS meanwhile).
    let timer: ReturnType<typeof setTimeout> | undefined
    new ResizeObserver(() => {
      const w = this.el.clientWidth
      if (!this.pages.length || !w || Math.abs(w - this.renderedWidth) < 24) return
      this.renderedWidth = w
      this.layout()
      clearTimeout(timer)
      timer = setTimeout(() => this.update(), 250)
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
      this.pageFrame ||= requestAnimationFrame(() => { this.pageFrame = 0; this.showPage(); this.update() })
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
    this.printBtn = h('button', { class: 'iconbtn', title: t('Print the PDF'), 'aria-label': t('Print'), onclick: () => { this.print().catch((e) => { this.printDone(); alert(t('Couldn’t prepare the PDF for printing: {{error}}', { error: e })) }) } }, icon('print'))
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
    const label = [...this.printBtn.childNodes]
    this.printDone = () => { this.printBtn.disabled = false; this.printBtn.replaceChildren(...label) }
    const task = openPdf(this.last) // its own copy: the view's pages stay as they are
    const sheets = h('div', { class: 'print-sheets' })
    const urls: string[] = []
    let size = ''
    try {
      const doc = await task.promise
      for (let i = 1; i <= doc.numPages; i++) {
        this.printBtn.replaceChildren(icon('print'), ' ', t('Preparing… ({{done}} of {{total}})', { done: i, total: doc.numPages }))
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
    // loaded, not all decoded at once: the browser decodes them as it prints
    await Promise.all([...sheets.querySelectorAll('img')].map((img) => img.complete ? null : new Promise((res) => { img.onload = img.onerror = res })))
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
    this.layout()
    this.el.scrollLeft = across * this.el.scrollWidth - x
    this.el.scrollTop = down * this.el.scrollHeight - y
    clearTimeout(this.zoomTimer)
    this.zoomTimer = setTimeout(() => this.update(), 300) // sharp again, once the zooming stops
  }

  /** Size every page for the current width (drawn ones stretch until they're drawn again). */
  private layout() {
    this.width = this.pageWidth()
    for (const p of this.pages) {
      p.wrap.style.width = `${Math.floor(this.width)}px`
      p.wrap.style.height = `${Math.floor(this.width * p.ratio)}px`
      p.canvas.style.width = `${Math.floor(this.width)}px`
    }
  }

  private scaleOf(p: Page) { return this.width / p.points }

  /** Draw the pages near the screen at the current size, nearest first, and free those far away. */
  private update() {
    if (!this.doc || !this.pages.length || this.drawing) return
    const view = this.el.getBoundingClientRect(), h = Math.max(1, view.height)
    let next: Page | null = null, best = Infinity
    for (const p of this.pages) {
      const r = p.wrap.getBoundingClientRect()
      const away = r.bottom < view.top ? view.top - r.bottom : r.top > view.bottom ? r.top - view.bottom : 0
      if (away > FAR * h) { if (p.drawn) this.free(p) }
      else if (away <= NEAR * h && p.drawn !== this.width && away < best) { best = away; next = p }
    }
    if (!next) return
    this.drawing = true
    const gen = this.gen
    this.draw(next).catch(() => {}).finally(() => {
      this.drawing = false
      if (gen === this.gen) this.update() // the next one
    })
  }

  private free(p: Page) {
    const blank = emptyCanvas()
    blank.style.width = p.canvas.style.width
    p.canvas.replaceWith(blank)
    p.canvas.width = p.canvas.height = 0
    p.canvas = blank
    p.drawn = 0
    p.wrap.classList.add('blank')
  }

  /** Draw one page at the current width, off screen, then put it in place. */
  private async draw(p: Page, doc = this.doc) {
    if (!doc) return
    const width = this.width
    const page = await doc.getPage(this.pages.indexOf(p) + 1 || 1)
    const vp = page.getViewport({ scale: width / p.points })
    const dpr = Math.min(window.devicePixelRatio || 1, Math.sqrt(MAX_PAGE_PIXELS / (vp.width * vp.height)))
    const canvas = document.createElement('canvas')
    canvas.width = Math.floor(vp.width * dpr)
    canvas.height = Math.floor(vp.height * dpr)
    canvas.style.width = `${Math.floor(width)}px`
    await page.render({ canvas, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined }).promise
    if (!this.pages.includes(p)) { canvas.width = canvas.height = 0; return } // a newer document came meanwhile
    p.canvas.replaceWith(canvas)
    p.canvas.width = p.canvas.height = 0
    p.canvas = canvas
    p.drawn = width
    p.wrap.classList.remove('blank')
  }

  /** The page and PDF-point position under a mouse event, if it's on a page. */
  private pointAt(e: MouseEvent): { page: number; x: number; y: number } | null {
    const wrap = (e.target as HTMLElement).closest?.('.page') as HTMLDivElement | null
    const i = this.pages.findIndex((p) => p.wrap === wrap)
    if (i < 0) return null
    const rect = wrap!.getBoundingClientRect()
    const s = this.scaleOf(this.pages[i])
    return { page: i + 1, x: (e.clientX - rect.left) / s, y: (e.clientY - rect.top) / s }
  }

  /** The point “go to source” should use: the last click if it's still in view, else the middle of the view. */
  focusPoint(): { page: number; x: number; y: number } | null {
    if (!this.pages.length) return null
    const view = this.el.getBoundingClientRect()
    const visible = (page: number, y: number) => {
      const p = this.pages[page - 1]
      if (!p) return false
      const top = p.wrap.getBoundingClientRect().top + y * this.scaleOf(p)
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
    const s = this.scaleOf(p)
    const y = Math.max(0, Math.min(r.height, mid - r.top)) / s
    return { page: best + 1, x: r.width / 2 / s, y }
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
    const s = this.scaleOf(p)
    Object.assign(mark.style, { left: `${(x - 2) * s}px`, top: `${(y - 2) * s}px`, width: `${(w + 4) * s}px`, height: `${(h + 4) * s}px` })
    p.wrap.append(mark)
    mark.scrollIntoView({ block: 'center', behavior: 'smooth' })
    setTimeout(() => mark.remove(), 2500)
  }

  async show(data: Uint8Array) {
    this.last = data
    const gen = ++this.gen
    const task = openPdf(data)
    let doc: pdfjs.PDFDocumentProxy
    try { doc = await task.promise } catch (e) { void task.destroy(); throw e }
    if (gen !== this.gen) { void task.destroy(); return }
    // every page's size, which is cheap; drawing is what costs
    const sizes: { points: number; ratio: number }[] = []
    for (let i = 1; i <= doc.numPages; i++) {
      const base = (await doc.getPage(i)).getViewport({ scale: 1 })
      sizes.push({ points: base.width, ratio: base.height / base.width })
    }
    if (gen !== this.gen) { void task.destroy(); return }
    const scrollFrac = this.el.scrollTop / Math.max(1, this.el.scrollHeight)
    const scrollAcross = this.el.scrollLeft / Math.max(1, this.el.scrollWidth)
    this.renderedWidth = this.el.clientWidth
    const width = this.pageWidth()
    const pages: Page[] = sizes.map(({ points, ratio }) => {
      const canvas = emptyCanvas()
      const wrap = h('div', { class: 'page blank' }, canvas) as HTMLDivElement
      return { wrap, canvas, points, ratio, drawn: 0 }
    })
    // draw what will be on screen before showing the new document, so nothing flashes blank
    const old = { doc: this.doc, task: this.task, pages: this.pages }
    this.doc = doc; this.task = task; this.pages = pages; this.width = width
    let y = GAP
    const tops = pages.map((p) => { const top = y; y += Math.floor(width * p.ratio) + GAP; return top })
    const top = scrollFrac * (y + GAP), height = this.el.clientHeight || 800
    this.drawing = true // scrolling meanwhile mustn't draw pages that aren't on screen yet
    try {
      for (const [i, p] of pages.entries()) {
        if (tops[i] + width * p.ratio >= top - height / 2 && tops[i] <= top + height * 1.5) await this.draw(p, doc).catch(() => {})
        if (gen !== this.gen) return
      }
    } finally { this.drawing = false }
    this.el.replaceChildren(...pages.map((p) => p.wrap))
    this.layout()
    this.el.scrollTop = scrollFrac * this.el.scrollHeight
    this.el.scrollLeft = scrollAcross * this.el.scrollWidth
    for (const p of old.pages) p.canvas.width = p.canvas.height = 0
    if (old.task) void old.task.destroy() // otherwise each document keeps a worker for the life of the tab
    this.controls.hidden = false
    this.pageTotal.textContent = `/ ${pages.length}`
    this.showPage()
    this.update()
  }
}
