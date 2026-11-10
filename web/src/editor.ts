// The editor pane: a collaborative CodeMirror editor for text files (with LaTeX
// support), or a preview for images and PDFs.
import * as Y from 'yjs'
import { EditorView, basicSetup } from 'codemirror'
import { EditorState, Compartment, RangeSet, StateEffect, StateField } from '@codemirror/state'
import { Decoration, gutter, GutterMarker, ViewPlugin, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { yCollab } from 'y-codemirror.next'
import { oneDark } from '@codemirror/theme-one-dark'
import type { Replica } from './replica'
import { h, saveFile } from './dom'
import { locale, t, tn } from './i18n'
import { blankNonProse, countWords } from './navigate'
import { latexSupport, recheck, type AddReference, type ProjectIndex } from './latex'
import { imageDropExtension } from './pasteimage'
import { spellcheck } from './spell'
import { isTexFile } from './projectfiles'
import { editorMenu, type EditorMenuHost } from './editormenu'
import { mathPreview } from './mathpreview'
import { lineAuthors } from './history'
import type { Problem } from './latexlog'
import { keymap } from '@codemirror/view'
import { Prec, type Extension } from '@codemirror/state'
import { setSuggestingFlag, suggestSession, type SuggestSession } from './suggestmode'
import type { ReviewMap } from './review'

export interface EditorPane {
  el: HTMLElement
  open(path: string | null): void
  showAuthors(on: boolean): void
  /** Move the cursor to a line; `flash` briefly highlights it (e.g. arriving from the PDF). */
  goto(line: number, col?: number, flash?: boolean): void
  view(): EditorView | null
  current(): string | null
  relint(): void
  gotoOffset(pos: number): void
  /** What the user is looking at. Lines are the file's; while suggesting, `selection` is the text as
   *  shown (with the user's suggestions) and `fileSelection` what the file itself has there. */
  context(): { file: string; selection: string; fromLine: number; toLine: number; suggesting: boolean; fileSelection?: string } | null
  onContextChange(f: () => void): void
  selection(): { file: string; from: number; to: number; text: string } | null
  reveal(from: number, to: number): void
  refreshSpell(): void
  /** Suggest mode: typing makes suggestions instead of changing the file. */
  setSuggesting(on: boolean): void
  /** Save what's been typed while suggesting. */
  flush(): void
}

// A line briefly highlighted after jumping to it (from the PDF, say).
const setFlash = StateEffect.define<number>()
const flashField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(deco, tr) {
    for (const e of tr.effects) if (e.is(setFlash)) deco = e.value < 0 ? Decoration.none : Decoration.set([Decoration.line({ class: 'cm-flash' }).range(e.value)])
    return deco.map(tr.changes)
  },
  provide: (f) => EditorView.decorations.from(f),
})

export interface EditorHooks {
  onCompile: () => void
  onSync: () => void // show the current line in the PDF
  problems: (file: string) => Problem[] // last compile's problems for a file
  review: (file: string, map?: ReviewMap) => Extension // comment/suggestion highlights
  onReview: (kind: 'comment' | 'suggestion') => void // ⌘⌥M; right-click
  onSymbols: () => void // ⌘⌥S
  onToggleSuggest: () => void // ⌘⌥E
  userName: () => string
  menu: EditorMenuHost // right-click on selected text and LaTeX commands
  addReference: AddReference // from \cite{ completion
  spell: () => { on: boolean; lang: string }
  mathPreview: () => boolean
  checks: () => boolean
}

export function createEditorPane(r: Replica, writable: boolean, index: ProjectIndex, hooks: EditorHooks): EditorPane {
  const el = h('div', { class: 'editor' })
  let view: EditorView | null = null
  let current: string | null = null
  const dark = matchMedia('(prefers-color-scheme: dark)').matches
  const authorsSlot = new Compartment()
  const spellSlot = new Compartment()
  const mathSlot = new Compartment()
  const mathExt = () => (hooks.mathPreview() && isTexFile(current) ? (mathExtension ??= mathPreview(r.files)) : [])
  let mathExtension: Extension | undefined
  const spellExt = () => { const sp = hooks.spell(); return spellcheck(sp.on, sp.lang, isTexFile(current)) }
  let authorsOn = false
  let suggesting = false
  let session: SuggestSession | null = null // while suggesting in a text file

  // how many words are selected, in the corner of the editor while anything is
  const selectionCount = h('div', { class: 'selcount', role: 'status', hidden: true })
  let selectionTimer: ReturnType<typeof setTimeout> | undefined
  function showSelectionCount(v: EditorView) {
    clearTimeout(selectionTimer)
    selectionTimer = setTimeout(() => { // dragging over a long document shouldn't count on every step
      const text = v.state.selection.ranges.filter((x) => !x.empty).map((x) => v.state.sliceDoc(x.from, x.to)).join('\n')
      selectionCount.hidden = !text
      if (!text) return
      const n = countWords(blankNonProse(text))
      selectionCount.textContent = tn(n, '{{n}} word selected', '{{n}} words selected', { n: n.toLocaleString(locale) })
    }, 80)
  }

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

  let bound: Y.Text | undefined // the text the open editor edits
  // The open path now holds a different text (a same-name file from someone else won,
  // clashes.ts): edits to the old one would be lost, so show the new one.
  r.files.observe((ev) => {
    if (current && ev.keysChanged.has(current) && r.files.has(current) && r.files.get(current) !== bound) {
      const p = current
      current = null
      open(p)
    }
  })

  const contextWatchers: (() => void)[] = []
  const contextChanged = () => contextWatchers.forEach((f) => f())
  function open(path: string | null) { show(path); contextChanged() }

  function show(path: string | null) {
    if (path === current && (view || !path)) return
    current = path
    view?.destroy(); view = null
    session?.destroy(); session = null
    el.replaceChildren(selectionCount)
    selectionCount.hidden = true
    bound = undefined
    if (!path) return
    const yt = bound = r.files.get(path)
    if (yt) {
      // Suggesting, the editor shows its own view of the file (suggestmode.ts) instead of
      // editing the shared text, so collaborators' cursors aren't shown meanwhile.
      const s = session = suggesting && writable ? suggestSession(r, path, yt, hooks.userName, hooks.onToggleSuggest) : null
      view = new EditorView({
        parent: el,
        state: EditorState.create({
          doc: s ? s.initial : yt.toString(),
          extensions: [
            basicSetup,
            StreamLanguage.define(stex),
            EditorView.lineWrapping,
            dark ? oneDark : [],
            s ? s.extension : yCollab(yt, r.awareness, { undoManager: new Y.UndoManager(yt) }),
            latexSupport(index, { onCompile: hooks.onCompile, onSync: hooks.onSync, problems: () => sharedToViewLines(current ? hooks.problems(current) : []), addReference: writable ? hooks.addReference : undefined, checks: () => hooks.checks() && isTexFile(current) }),
            imageDropExtension(r, writable),
            spellSlot.of(spellExt()),
            editorMenu(hooks.menu),
            mathSlot.of(mathExt()),
            authorsSlot.of(authorsOn && !s ? authorsExtension(yt) : []),
            hooks.review(path, s ? { toView: s.toView, skip: s.mine } : undefined),
            flashField,
            EditorView.updateListener.of((u) => { if (u.selectionSet || u.docChanged) { showSelectionCount(u.view); contextChanged() } }),
            // ⌘⌥E / Ctrl+Alt+E: suggest mode on or off. By the physical key, since keyboards report
            // Alt with letters differently on each system (on a Mac, ⌥E is a dead key)
            Prec.highest(EditorView.domEventHandlers({
              keydown: (e) => {
                if (!(writable && e.code === 'KeyE' && e.altKey && (e.metaKey || e.ctrlKey))) return false
                if (e.getModifierState?.('AltGraph')) return false // AltGr+E types € on many keyboards
                e.preventDefault()
                hooks.onToggleSuggest()
                return true
              },
            })),
            Prec.high(keymap.of([
              { key: 'Mod-Alt-m', run: () => { hooks.onReview('comment'); return true } },
              { key: 'Mod-Alt-s', run: () => { if (writable) hooks.onSymbols(); return writable }, preventDefault: true },
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
    const box = h('div', { class: 'binary' }, h('p', {}, t('{{path}} — {{size}} KB', { path, size: (info.size / 1024).toFixed(1) })))
    el.append(box)
    r.getBlob(info.h).then((data) => {
      if (current !== path) return
      box.append(h('p', {}, h('button', { onclick: () => saveFile(name, data) }, t('Download {{name}}', { name }))))
    }).catch((e) => box.append(h('p', { class: 'err' }, String(e))))
  }

  // While suggesting, the editor's positions are in its own view of the file: everything outside
  // the editor (comments, search, the assistant, compile errors) uses the shared file's.
  const lineIn = (text: string, pos: number) => { let n = 1; for (let i = text.indexOf('\n'); i >= 0 && i < pos; i = text.indexOf('\n', i + 1)) n++; return n }
  /** Compile problems are on the file's lines: while suggesting, find them in the view. */
  function sharedToViewLines(problems: Problem[]): Problem[] {
    if (!session || !view) return problems
    const shared = session.shared(), doc = view.state.doc
    const starts = [0]
    for (let i = shared.indexOf('\n'); i >= 0; i = shared.indexOf('\n', i + 1)) starts.push(i + 1)
    return problems.map((p) => p.line == null ? p : { ...p, line: doc.lineAt(Math.min(doc.length, session!.toView(starts[Math.min(p.line, starts.length) - 1]))).number })
  }

  /** The selection in the shared file. */
  function sharedSelection(): { from: number; to: number; text: string } | null {
    if (!view) return null
    const sel = view.state.selection.main
    if (!session) return { from: sel.from, to: sel.to, text: view.state.doc.sliceString(sel.from, sel.to) }
    const from = session.toShared(sel.from, -1), to = sel.empty ? from : Math.max(from, session.toShared(sel.to, 1))
    return { from, to, text: session.shared().slice(from, to) }
  }

  function context() {
    if (!current) return null
    const sel = sharedSelection()
    if (!sel) return { file: current, selection: '', fromLine: 0, toLine: 0, suggesting: false }
    if (!session) {
      const doc = view!.state.doc
      return { file: current, selection: sel.text, fromLine: doc.lineAt(sel.from).number, toLine: doc.lineAt(sel.to).number, suggesting: false }
    }
    const shared = session.shared(), main = view!.state.selection.main
    const shown = view!.state.doc.sliceString(main.from, main.to)
    return { file: current, selection: shown, fromLine: lineIn(shared, sel.from), toLine: lineIn(shared, sel.to), suggesting: true, ...(shown !== sel.text ? { fileSelection: sel.text } : {}) }
  }

  function showAuthors(on: boolean) {
    authorsOn = on
    const yt = current ? r.files.get(current) : null
    if (view && yt) view.dispatch({ effects: authorsSlot.reconfigure(on && !session ? authorsExtension(yt) : []) })
  }

  function goto(line: number, col = 0, flash = false) {
    if (!view) return
    const doc = view.state.doc
    let pos: number
    if (session) { // a line of the shared file
      const shared = session.shared()
      let start = 0
      for (let n = 1; n < line; n++) { const i = shared.indexOf('\n', start); if (i < 0) break; start = i + 1 }
      const end = shared.indexOf('\n', start)
      pos = session.toView(Math.min(end < 0 ? shared.length : end, start + col))
    } else {
      const l = doc.line(Math.max(1, Math.min(doc.lines, line)))
      pos = Math.min(l.to, l.from + col)
    }
    const l = doc.lineAt(Math.min(pos, doc.length))
    view.dispatch({ selection: { anchor: pos }, effects: [EditorView.scrollIntoView(pos, { y: 'center' }), ...(flash ? [setFlash.of(l.from)] : [])] })
    view.focus()
    if (flash) { const v = view; setTimeout(() => { if (view === v) v.dispatch({ effects: setFlash.of(-1) }) }, 1600) }
  }

  function gotoOffset(pos: number) {
    if (!view) return
    const p = Math.max(0, Math.min(view.state.doc.length, session ? session.toView(pos) : pos))
    view.dispatch({ selection: { anchor: p }, effects: EditorView.scrollIntoView(p, { y: 'center' }) })
    view.focus()
  }

  function selection() {
    const sel = current ? sharedSelection() : null
    return sel && current ? { file: current, ...sel } : null
  }

  function reveal(from: number, to: number) {
    if (!view) return
    if (session) { from = session.toView(from); to = session.toView(to) }
    const n = view.state.doc.length
    const a = Math.max(0, Math.min(from, n)), b = Math.max(a, Math.min(to, n))
    view.dispatch({ selection: { anchor: a, head: b }, effects: EditorView.scrollIntoView(a, { y: 'center' }) })
    view.focus()
  }

  function setSuggesting(on: boolean) {
    on &&= writable
    setSuggestingFlag(on, hooks.userName)
    if (on === suggesting) return
    suggesting = on
    const path = current, sel = sharedSelection(), focused = view?.hasFocus
    current = null
    show(path) // with or without its own view of the file
    if (view && sel) { // keep the cursor where it was in the file
      const a = session ? session.toView(sel.from) : sel.from, b = session ? session.toView(sel.to) : sel.to
      const n = view.state.doc.length
      view.dispatch({ selection: { anchor: Math.min(a, n), head: Math.min(b, n) }, scrollIntoView: true })
      if (focused) view.focus()
    }
  }

  const refreshSpell = () => { if (view) view.dispatch({ effects: [spellSlot.reconfigure(spellExt()), mathSlot.reconfigure(mathExt())] }) }

  return { el, open, onContextChange: (f: () => void) => { contextWatchers.push(f) }, context, selection, reveal, refreshSpell, showAuthors, goto, gotoOffset, setSuggesting, flush: () => session?.flush(), view: () => view, current: () => current, relint: () => { view?.dispatch({ effects: recheck.of(null) }) } }
}
