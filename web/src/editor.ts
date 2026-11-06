// The editor pane: a collaborative CodeMirror editor for text files (with LaTeX
// support), or a preview for images and PDFs.
import * as Y from 'yjs'
import { EditorView, basicSetup } from 'codemirror'
import { EditorState } from '@codemirror/state'
import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { yCollab } from 'y-codemirror.next'
import { oneDark } from '@codemirror/theme-one-dark'
import type { Replica } from './replica'
import { h, saveFile } from './dom'
import { latexSupport, type ProjectIndex } from './latex'
import { PdfView } from './pdfview'

export interface EditorPane {
  el: HTMLElement
  open(path: string | null): void
  context(): { file: string; selection: string; fromLine: number; toLine: number } | null
}

export function createEditorPane(r: Replica, writable: boolean, index: ProjectIndex, onCompile: () => void): EditorPane {
  const el = h('div', { class: 'editor' })
  let view: EditorView | null = null
  let current: string | null = null
  const dark = matchMedia('(prefers-color-scheme: dark)').matches

  function open(path: string | null) {
    if (path === current && (view || !path)) return
    current = path
    view?.destroy(); view = null
    el.replaceChildren()
    if (!path) return
    const yt = r.files.get(path)
    if (yt) {
      view = new EditorView({
        parent: el,
        state: EditorState.create({
          doc: yt.toString(),
          extensions: [
            basicSetup,
            StreamLanguage.define(stex),
            EditorView.lineWrapping,
            dark ? oneDark : [],
            yCollab(yt, r.awareness, { undoManager: new Y.UndoManager(yt) }),
            latexSupport(index, { onCompile }),
            EditorState.readOnly.of(!writable),
            EditorView.editable.of(writable),
          ],
        }),
      })
      return
    }
    const info = r.bin.get(path)
    if (!info) return
    const name = path.slice(path.lastIndexOf('/') + 1)
    const box = h('div', { class: 'binary' }, h('p', {}, `${path} — ${(info.size / 1024).toFixed(1)} KB`))
    el.append(box)
    r.getBlob(info.h).then((data) => {
      if (current !== path) return
      const type = /\.pdf$/i.test(path) ? 'application/pdf' : /\.svg$/i.test(path) ? 'image/svg+xml' : /\.(png|jpe?g|gif|webp)$/i.test(path) ? `image/${path.split('.').pop()!.toLowerCase().replace('jpg', 'jpeg')}` : ''
      // SVG is shown via <img>, where scripts in it don't run; PDFs are rendered with PDF.js.
      if (type.startsWith('image/')) {
        // Revoke the blob URL once shown, so it can't be opened as a page on this site (SVG scripts).
        const url = URL.createObjectURL(new Blob([data as BlobPart], { type }))
        const img = h('img', { src: url, alt: name })
        img.addEventListener('load', () => URL.revokeObjectURL(url), { once: true })
        img.addEventListener('error', () => URL.revokeObjectURL(url), { once: true })
        box.append(img)
      }
      else if (type === 'application/pdf') { const pv = new PdfView(); box.append(pv.el); pv.show(data).catch(() => {}) }
      box.append(h('p', {}, h('button', { onclick: () => saveFile(name, data) }, `Download ${name}`)))
    }).catch((e) => box.append(h('p', { class: 'err' }, String(e))))
  }

  function context() {
    if (!current) return null
    if (!view) return { file: current, selection: '', fromLine: 0, toLine: 0 }
    const sel = view.state.selection.main
    const doc = view.state.doc
    return { file: current, selection: doc.sliceString(sel.from, sel.to), fromLine: doc.lineAt(sel.from).number, toLine: doc.lineAt(sel.to).number }
  }

  return { el, open, context }
}
