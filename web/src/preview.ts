// The Preview tab: shows the Markdown, image or PDF file chosen in the file list.
import { h } from './dom'
import { t } from './i18n'
import { renderMarkdown } from './markdown'
import { loadKatex } from './mathpreview'
import { PdfView } from './pdfview'
import type { Replica } from './replica'

const MARKDOWN = /\.(?:md|markdown)$/i
const IMAGE_TYPES: [RegExp, string][] = [[/\.png$/i, 'image/png'], [/\.jpe?g$/i, 'image/jpeg'], [/\.gif$/i, 'image/gif'], [/\.svg$/i, 'image/svg+xml'], [/\.webp$/i, 'image/webp']]
const PDF = /\.pdf$/i
const MAX_MATH = 20_000
const typeOf = (path: string) => IMAGE_TYPES.find(([re]) => re.test(path))?.[1] ?? null

export const isPreviewable = (path: string) => MARKDOWN.test(path) || PDF.test(path) || typeOf(path) !== null

export class FilePreview {
  readonly el = h('div', { class: 'pane preview-pane', hidden: true })
  private path: string | null = null
  private shown = ''
  private gen = 0
  private stop: () => void = () => {}
  private images = new Map<string, HTMLImageElement[]>() // the last render's, reused so they don't flicker

  constructor(private r: Replica) {
    this.empty()
    const recheck = () => { if (this.path && this.fingerprint(this.path) !== this.shown) this.show(this.path) }
    r.files.observe(recheck)
    r.bin.observe(recheck)
  }

  private fingerprint(path: string) { return this.r.files.has(path) ? 'text' : this.r.bin.get(path)?.h ?? '' }

  /** A file's bytes, and a key that changes with them. SVGs, and a few PDFs, are stored as text. */
  private source(path: string): { key: string; data: () => Promise<Uint8Array> } | null {
    const text = this.r.files.get(path)?.toString()
    if (text !== undefined) return { key: text, data: async () => new TextEncoder().encode(text) }
    const info = this.r.bin.get(path)
    return info ? { key: info.h, data: () => this.r.getBlob(info.h) } : null
  }

  private empty(text = t('Choose a Markdown file, image or PDF in the file list to see it here.')) {
    this.el.replaceChildren(h('p', { class: 'muted preview-empty' }, text))
  }

  show(path: string) {
    this.stop()
    this.stop = () => {}
    this.images = new Map()
    this.path = path
    this.shown = this.fingerprint(path)
    const gen = ++this.gen
    if (!this.shown) { this.empty(); return }
    const name = h('div', { class: 'preview-name muted small' }, path)
    if (MARKDOWN.test(path) && this.r.files.has(path)) { this.markdown(path, name, gen); return }
    const src = this.source(path)
    if (!src) { this.empty(); return }
    const box = h('div', { class: PDF.test(path) ? 'preview-pdf' : 'preview-image' })
    this.el.replaceChildren(name, box)
    const text = this.r.files.get(path)
    if (text) { // edited in the editor: follow along
      let timer: ReturnType<typeof setTimeout> | undefined
      const later = () => { clearTimeout(timer); timer = setTimeout(() => this.show(path), 500) }
      text.observe(later)
      this.stop = () => { clearTimeout(timer); text.unobserve(later) }
    }
    if (PDF.test(path)) {
      src.data().then((data) => {
        if (gen !== this.gen) return
        const view = new PdfView()
        box.append(view.el)
        return view.show(data)
      }).catch((e) => { if (gen === this.gen) box.append(h('p', { class: 'err' }, String(e))) })
    }
    else box.append(this.image(src.data, path, path.slice(path.lastIndexOf('/') + 1)))
  }

  private image(data: () => Promise<Uint8Array>, path: string, alt: string): HTMLImageElement {
    const img = h('img', { alt }) as HTMLImageElement
    data().then((data) => {
      // Revoke the blob URL once shown, so it can't be opened as a page on this site (SVG scripts).
      // In an <img>, scripts inside an SVG don't run.
      const url = URL.createObjectURL(new Blob([data as BlobPart], { type: typeOf(path) ?? '' }))
      const done = () => URL.revokeObjectURL(url)
      img.addEventListener('load', done, { once: true })
      img.addEventListener('error', done, { once: true })
      img.src = url
    }).catch(() => { img.alt = t('Image not found: {{name}}', { name: alt }) })
    return img
  }

  private markdown(path: string, name: HTMLElement, gen: number) {
    const text = this.r.files.get(path)!
    const box = h('div', { class: 'md-preview' })
    this.el.replaceChildren(name, box)
    const render = async () => {
      const source = text.toString()
      const katex = source.includes('$') ? await loadKatex().catch(() => null) : null // loaded first, so the height doesn't change after the scroll is put back
      if (gen !== this.gen) return
      const previous = this.images
      this.images = new Map()
      const content = renderMarkdown(source, path, {
        image: (p, alt) => {
          const src = typeOf(p) ? this.source(p) : null
          if (!src) return null
          const img = previous.get(src.key)?.pop() ?? this.image(src.data, p, alt)
          img.alt = alt
          this.images.set(src.key, [...(this.images.get(src.key) ?? []), img])
          return img
        },
        math: katex ? (tex, display, into) => {
          if (tex.length > MAX_MATH) { into.textContent = t('Too long to preview.'); return }
          try { katex.render(tex, into, { displayMode: display, throwOnError: false, trust: false }) }
          catch { into.textContent = tex }
        } : undefined,
      })
      const top = box.scrollTop
      box.replaceChildren(content)
      box.scrollTop = top
    }
    let timer: ReturnType<typeof setTimeout> | undefined
    const later = () => { clearTimeout(timer); timer = setTimeout(render, 250) }
    text.observe(later)
    this.stop = () => { clearTimeout(timer); text.unobserve(later) }
    void render()
  }
}
