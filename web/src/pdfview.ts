// Renders a PDF into a scrollable stack of canvases with PDF.js. Keeps the scroll
// position across re-renders so recompiling doesn't jump you back to page 1.
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { t } from './i18n'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
export { pdfjs }

export class PdfView {
  readonly el: HTMLDivElement
  /** Double-click or “Go to source” on a page: page number (1-based) and position in PDF points from the top-left. */
  onPick: ((page: number, x: number, y: number) => void) | null = null
  /** Where the PDF was last clicked (what “go to source” uses), in PDF points. */
  private lastClick: { page: number; x: number; y: number } | null = null
  private menu: HTMLDivElement | null = null
  private pages: { wrap: HTMLDivElement; scale: number }[] = []
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
      this.openMenu(e.clientX, e.clientY, [[t('Go to source here'), () => this.onPick?.(at.page, at.x, at.y)]])
    })
    this.el.addEventListener('scroll', () => this.closeMenu(), { passive: true })
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

  private openMenu(x: number, y: number, items: [string, () => void][]) {
    this.closeMenu()
    const m = document.createElement('div')
    m.className = 'ctxmenu'
    m.setAttribute('role', 'menu')
    for (const [label, run] of items) {
      const b = document.createElement('button')
      b.setAttribute('role', 'menuitem')
      b.textContent = label
      b.onclick = () => { this.closeMenu(); run() }
      m.append(b)
    }
    document.body.append(m)
    const w = m.offsetWidth, h = m.offsetHeight
    Object.assign(m.style, { left: `${Math.min(x, innerWidth - w - 4)}px`, top: `${Math.min(y, innerHeight - h - 4)}px` })
    ;(m.firstElementChild as HTMLElement | null)?.focus()
    const away = (e: Event) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !m.contains(e.target as Node)) this.closeMenu()
    }
    setTimeout(() => {
      document.addEventListener('pointerdown', away, true)
      document.addEventListener('keydown', away, true)
      addEventListener('blur', away)
    })
    this.menu = m
    ;(m as any).cleanup = () => {
      document.removeEventListener('pointerdown', away, true)
      document.removeEventListener('keydown', away, true)
      removeEventListener('blur', away)
    }
  }
  private closeMenu() {
    if (!this.menu) return
    ;(this.menu as any).cleanup?.()
    this.menu.remove()
    this.menu = null
  }

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
    const doc = await pdfjs.getDocument({ data: data.slice() }).promise
    if (gen !== this.gen) return
    const scrollFrac = this.el.scrollTop / Math.max(1, this.el.scrollHeight)
    if (!this.el.clientWidth) return // hidden: render when it becomes visible
    this.renderedWidth = this.el.clientWidth
    const width = Math.max(160, this.el.clientWidth - 24) // a very narrow pane scrolls sideways instead of failing
    const pages = document.createDocumentFragment()
    const info: { wrap: HTMLDivElement; scale: number }[] = []
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i)
      const base = page.getViewport({ scale: 1 })
      const vp = page.getViewport({ scale: width / base.width })
      const dpr = window.devicePixelRatio || 1
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
      info.push({ wrap, scale: vp.width / base.width })
    }
    this.pages = info
    this.el.replaceChildren(pages)
    this.el.scrollTop = scrollFrac * this.el.scrollHeight
  }
}
