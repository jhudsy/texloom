// The editor pane: a collaborative CodeMirror editor for text files (with LaTeX
// support), or a preview for images and PDFs.
import * as Y from 'yjs'
import { EditorView, basicSetup } from 'codemirror'
import { EditorState, Compartment, RangeSet, StateEffect, StateField } from '@codemirror/state'
import { gutter, GutterMarker, ViewPlugin, type ViewUpdate } from '@codemirror/view'
import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { yCollab } from 'y-codemirror.next'
import { oneDark } from '@codemirror/theme-one-dark'
import type { Replica } from './replica'
import { h, saveFile } from './dom'
import { latexSupport, type AddReference, type ProjectIndex } from './latex'
import { imageDropExtension } from './pasteimage'
import { PdfView } from './pdfview'
import { lineAuthors } from './history'
import type { Problem } from './latexlog'
import { forceLinting } from '@codemirror/lint'
import { keymap } from '@codemirror/view'
import { Prec, type Extension } from '@codemirror/state'

export interface EditorPane {
  el: HTMLElement
  open(path: string | null): void
  showAuthors(on: boolean): void
  goto(line: number, col?: number): void
  view(): EditorView | null
  current(): string | null
  relint(): void
  gotoOffset(pos: number): void
  context(): { file: string; selection: string; fromLine: number; toLine: number } | null
  selection(): { file: string; from: number; to: number; text: string } | null
  reveal(from: number, to: number): void
}

export interface EditorHooks {
  onCompile: () => void
  onSync: () => void // show the current line in the PDF
  problems: (file: string) => Problem[] // last compile's problems for a file
  review: (file: string) => Extension // comment/suggestion highlights
  onReview: (kind: 'comment' | 'suggestion') => void // ⌘⌥M / ⌘⌥E
  addReference: AddReference // from \cite{ completion
}

export function createEditorPane(r: Replica, writable: boolean, index: ProjectIndex, hooks: EditorHooks): EditorPane {
  const el = h('div', { class: 'editor' })
  let view: EditorView | null = null
  let current: string | null = null
  const dark = matchMedia('(prefers-color-scheme: dark)').matches
  const authorsSlot = new Compartment()
  let authorsOn = false

  // "Who wrote this": a gutter with the author of most of each line.
  const COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#469990', '#9a6324']
  class AuthorMarker extends GutterMarker {
    constructor(readonly name: string, readonly color: string) { super() }
    eq(o: AuthorMarker) { return o.name === this.name && o.color === this.color }
    toDOM() { return h('span', { class: 'author', title: this.name, style: `border-left-color:${this.color}` }, this.name.slice(0, 10)) }
  }
  const setAuthors = StateEffect.define<RangeSet<GutterMarker>>()
  const authorsField = StateField.define<RangeSet<GutterMarker>>({
    create: () => RangeSet.empty,
    update: (v, tr) => {
      for (const e of tr.effects) if (e.is(setAuthors)) return e.value
      return v.map(tr.changes)
    },
  })
  function authorsExtension(yt: Y.Text) {
    const compute = (view: EditorView) => {
      const owners = lineAuthors(yt)
      const doc = view.state.doc
      const ranges = owners.slice(0, doc.lines).flatMap((c, i) => {
        if (c < 0) return []
        const name = r.clients.get(String(c)) ?? `#${String(c).slice(-4)}`
        return [new AuthorMarker(String(name).slice(0, 64), COLORS[c % COLORS.length]).range(doc.line(i + 1).from)]
      })
      view.dispatch({ effects: setAuthors.of(RangeSet.of(ranges)) })
    }
    const plugin = ViewPlugin.define((view) => {
      let t: ReturnType<typeof setTimeout> | undefined = setTimeout(() => compute(view), 0)
      return {
        update(u: ViewUpdate) { if (u.docChanged) { clearTimeout(t); t = setTimeout(() => compute(u.view), 400) } },
        destroy() { clearTimeout(t) },
      }
    })
    return [authorsField, plugin, gutter({ class: 'cm-authors', markers: (v) => v.state.field(authorsField), initialSpacer: () => new AuthorMarker('Someone', '#888') })]
  }

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
            latexSupport(index, { onCompile: hooks.onCompile, onSync: hooks.onSync, problems: () => (current ? hooks.problems(current) : []), addReference: writable ? hooks.addReference : undefined }),
            imageDropExtension(r, writable),
            authorsSlot.of(authorsOn ? authorsExtension(yt) : []),
            hooks.review(path),
            Prec.high(keymap.of([
              { key: 'Mod-Alt-m', run: () => { hooks.onReview('comment'); return true } },
              { key: 'Mod-Alt-e', run: () => { hooks.onReview('suggestion'); return true } },
            ])),
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

  function showAuthors(on: boolean) {
    authorsOn = on
    const yt = current ? r.files.get(current) : null
    if (view && yt) view.dispatch({ effects: authorsSlot.reconfigure(on ? authorsExtension(yt) : []) })
  }

  function goto(line: number, col = 0) {
    if (!view) return
    const doc = view.state.doc
    const l = doc.line(Math.max(1, Math.min(doc.lines, line)))
    const pos = Math.min(l.to, l.from + col)
    view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: 'center' }) })
    view.focus()
  }

  function gotoOffset(pos: number) {
    if (!view) return
    const p = Math.max(0, Math.min(view.state.doc.length, pos))
    view.dispatch({ selection: { anchor: p }, effects: EditorView.scrollIntoView(p, { y: 'center' }) })
    view.focus()
  }

  function selection() {
    if (!current || !view) return null
    const sel = view.state.selection.main
    return { file: current, from: sel.from, to: sel.to, text: view.state.doc.sliceString(sel.from, sel.to) }
  }

  function reveal(from: number, to: number) {
    if (!view) return
    const n = view.state.doc.length
    const a = Math.max(0, Math.min(from, n)), b = Math.max(a, Math.min(to, n))
    view.dispatch({ selection: { anchor: a, head: b }, effects: EditorView.scrollIntoView(a, { y: 'center' }) })
    view.focus()
  }

  return { el, open, context, selection, reveal, showAuthors, goto, gotoOffset, view: () => view, current: () => current, relint: () => { if (view) forceLinting(view) } }
}
