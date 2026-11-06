// Renders a PDF into a scrollable stack of canvases with PDF.js. Keeps the scroll
// position across re-renders so recompiling doesn't jump you back to page 1.
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

export class PdfView {
  readonly el: HTMLDivElement
  private gen = 0
  constructor() {
    this.el = document.createElement('div')
    this.el.className = 'pdf'
  }

  async show(data: Uint8Array) {
    const gen = ++this.gen
    const doc = await pdfjs.getDocument({ data: data.slice() }).promise
    if (gen !== this.gen) return
    const scrollFrac = this.el.scrollTop / Math.max(1, this.el.scrollHeight)
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
