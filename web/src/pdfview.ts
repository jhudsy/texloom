// Renders a PDF into a scrollable stack of canvases with PDF.js. Keeps the scroll
// position across re-renders so recompiling doesn't jump you back to page 1.
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
export { pdfjs }

export class PdfView {
  readonly el: HTMLDivElement
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
      pages.append(canvas)
    }
    this.el.replaceChildren(pages)
    this.el.scrollTop = scrollFrac * this.el.scrollHeight
  }
}
