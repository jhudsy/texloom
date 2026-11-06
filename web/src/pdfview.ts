// Renders a PDF into a scrollable stack of canvases with PDF.js. Keeps the scroll
// position across re-renders so recompiling doesn't jump you back to page 1.
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
export { pdfjs }

export class PdfView {
  readonly el: HTMLDivElement
  /** Double-click on a page: page number (1-based) and position in PDF points from the top-left. */
  onPick: ((page: number, x: number, y: number) => void) | null = null
  private pages: { wrap: HTMLDivElement; scale: number }[] = []
  private gen = 0
  private last: Uint8Array | null = null
  private renderedWidth = 0
  constructor() {
    this.el = document.createElement('div')
    this.el.className = 'pdf'
    // Re-render at the new width after the pane is resized (canvases scale via CSS meanwhile).
    let t: ReturnType<typeof setTimeout> | undefined
    new ResizeObserver(() => {
      const w = this.el.clientWidth
      if (!this.last || !w || Math.abs(w - this.renderedWidth) < 24) return
      clearTimeout(t)
      t = setTimeout(() => { if (this.last) this.show(this.last).catch(() => {}) }, 250)
    }).observe(this.el)
    this.el.addEventListener('dblclick', (e) => {
      const wrap = (e.target as HTMLElement).closest('.page') as HTMLDivElement | null
      const i = this.pages.findIndex((p) => p.wrap === wrap)
      if (i < 0 || !this.onPick) return
      const rect = wrap!.getBoundingClientRect()
      const s = this.pages[i].scale
      this.onPick(i + 1, (e.clientX - rect.left) / s, (e.clientY - rect.top) / s)
    })
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
    const width = this.el.clientWidth - 24
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
