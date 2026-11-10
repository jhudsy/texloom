// Suggest mode in the editor: typing becomes suggestions instead of changing the file.
//
// While suggesting, the editor isn't bound to the shared text. It shows this tab's view: the
// shared text with its open typed suggestions applied (suggestcore.ts), drawn as a word diff.
// Each edit updates those suggestions; they're saved to the review map a moment after typing
// stops, where collaborators, the Review tab, the assistant and the MCP tools see ordinary
// suggestions. Edits from others to the shared text, and suggestions others accept, reject or
// delete, come back into the view.
import * as Y from 'yjs'
import { Annotation, Transaction, type Extension } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, WidgetType, showPanel, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import { h } from './dom'
import { t } from './i18n'
import { addItem, allItems, ChangeMark, changeTitle, editorMarkup, rangeOf } from './review'
import type { Replica } from './replica'
import { anchor, compose, edit, loadHunks, normalize, refresh, saveHunks, settle, toShared, toView, type Hunk } from './suggestcore'
import { refine, wordDiff } from './worddiff'

/** Marks changes to the review map that this tab's suggesting made (not news to it). */
const ORIGIN = { suggestMode: true }
/** Marks editor changes that come from the shared document rather than from typing. */
const fromShared = Annotation.define<boolean>()
/** Whose typed suggestions these are: this page, so that two tabs never edit the same ones.
 *  Typed suggestions from an earlier visit show like anyone else's. */
const ME = crypto.randomUUID()
const SAVE_AFTER = 800 // ms after typing stops

let on = false
const sessions = new Set<{ flush(): void }>()
/** Save what's been typed in every open suggesting editor, before something reads or settles
 *  suggestions (accepting one, the assistant). */
export function flushSuggestions() { sessions.forEach((x) => x.flush()) }
/** Whether this tab is suggesting: other ways of changing text (search and replace, adding a
 *  package for a symbol) then suggest too. */
export const isSuggesting = () => on
let suggester = () => ''
export const setSuggestingFlag = (v: boolean, by?: () => string) => { on = v; if (by) suggester = by }

/** Replace [from, to) of a file, or, while suggesting, suggest it (once: the same suggestion
 *  again, from a second click, is left out). */
export function changeOrSuggest(r: Replica, file: string, from: number, to: number, text: string) {
  const yt = r.files.get(file)
  if (!yt) return
  if (!on) { r.doc.transact(() => { if (to > from) yt.delete(from, to - from); if (text) yt.insert(from, text) }); return }
  const quote = yt.toString().slice(from, to)
  const same = allItems(r).some((i) => {
    if (i.kind !== 'suggestion' || i.resolved || i.file !== file || i.quote !== quote || i.text !== text) return false
    const at = rangeOf(r, i.id)
    return !!at && at.from === from && at.to === to
  })
  if (!same) addItem(r, 'suggestion', file, from, to, text, suggester())
}

/** Open suggestions in `file` whose new text contains `text` (so as not to suggest it twice). */
export const suggested = (r: Replica, file: string, text: string) =>
  on && allItems(r).some((i) => i.kind === 'suggestion' && !i.resolved && i.file === file && i.text.includes(text))

export interface SuggestSession {
  /** The text the editor starts with. */
  initial: string
  extension: Extension
  toView(x: number): number
  toShared(v: number, bias?: -1 | 1): number
  shared(): string
  /** Is this review item one of this view's own suggestions (drawn here, not by the review highlights)? */
  mine(id: string): boolean
  /** Save typed suggestions now. */
  flush(): void
  destroy(): void
}

class Struck extends WidgetType {
  constructor(readonly text: string, readonly id: string | null) { super() }
  eq(o: Struck) { return o.text === this.text && o.id === this.id }
  toDOM() { return h('span', { class: 'cm-suggest-del cm-suggest-gone', ...(this.id ? { 'data-review': this.id } : {}), title: t('Your suggestion removes this') }, this.text) }
  ignoreEvent() { return false }
}

export function suggestSession(r: Replica, file: string, yt: Y.Text, by: () => string, stop: () => void): SuggestSession {
  let shared = yt.toString()
  let hunks: Hunk[] = []
  let gone = new Set<string>()
  let dirty = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let cm: EditorView | null = null
  /** What each suggestion holds as stored, to tell what's been typed since. */
  const saved = new Map<string, string>()
  const remember = () => { saved.clear(); for (const h of hunks) if (h.id) saved.set(h.id, `${h.conflict ? 1 : 0}${h.text}`) }
  const unsaved = (h: Hunk) => !!h.id && saved.get(h.id) !== `${h.conflict ? 1 : 0}${h.text}`
  /** Start again from what's stored. */
  function load() {
    const res = loadHunks(r.doc, r.review, yt, file, ME)
    hunks = res.hunks
    anchor(yt, hunks)
    gone = new Set(res.gone)
    remember()
    dirty = false
    if (gone.size) later() // others' edits pushed two together: one of them goes
  }

  const isOpen = (id: string) => { const m = r.review.get(id); return m instanceof Y.Map && !m.get('resolved') }

  function flush() {
    clearTimeout(timer)
    if (!dirty) return
    dirty = false
    const kept = hunks.filter((h) => !h.id || r.review.has(h.id)) // (reconcile drops deleted ones first)
    const lost = kept.length !== hunks.length
    hunks = saveHunks(r.doc, r.review, yt, kept, gone, { file, by: by(), me: ME, origin: ORIGIN })
    anchor(yt, hunks)
    gone = new Set()
    remember()
    if (lost) show()
  }
  const later = () => { dirty = true; clearTimeout(timer); timer = setTimeout(flush, SAVE_AFTER) }

  /** Put the view in the editor, changing only what differs, so the cursor stays put. */
  function show() {
    if (!cm) return
    const next = compose(shared, hunks), now = cm.state.doc.toString()
    if (next === now) { put({}); return } // redraw: suggestions may have new ids
    let p = 0
    const n = Math.min(next.length, now.length)
    while (p < n && next.charCodeAt(p) === now.charCodeAt(p)) p++
    let q = 0
    while (q < n - p && next.charCodeAt(next.length - 1 - q) === now.charCodeAt(now.length - 1 - q)) q++
    // word by word, so that typing and undo history outside what changed keep their places
    const was = now.slice(p, now.length - q), will = next.slice(p, next.length - q)
    const changes: { from: number; to?: number; insert?: string }[] = []
    if (was.length + will.length > 400_000) changes.push({ from: p, to: now.length - q, insert: will })
    else {
      let at = p
      for (const op of wordDiff(was, will)) {
        if (op.op === 'eq') at += op.text.length
        else if (op.op === 'del') { changes.push({ from: at, to: at + op.text.length }); at += op.text.length }
        else changes.push({ from: at, insert: op.text })
      }
    }
    put({ changes, annotations: [fromShared.of(true), Transaction.addToHistory.of(false)] })
  }
  /** Dispatch to the editor; if it's in the middle of an update, just after (a few tries at most). */
  let tries = 0
  function put(spec: Parameters<EditorView['dispatch']>[0]) {
    const v = cm
    if (!v) return
    try { v.dispatch(spec); tries = 0 }
    catch (e) {
      if (++tries > 3) { tries = 0; console.error('suggest mode: could not update the editor', e); return }
      queueMicrotask(() => { if (cm === v) show() })
    }
  }

  /** The shared text changed, or suggestions were accepted, rejected or deleted. */
  function reconcile() {
    const res = refresh(r.doc, yt, hunks)
    shared = yt.toString()
    res.gone.forEach((id) => gone.add(id))
    const next: Hunk[] = []
    for (const h of res.hunks) {
      if (h.id && !isOpen(h.id)) {
        // accepted, rejected or deleted by someone: it leaves the view, except what was typed
        // since it was last saved, which becomes a new suggestion
        if (!unsaved(h)) continue
        const again = normalize(shared, h.a, h.b, h.text, null)
        if (again) next.push(again)
      } else if (shared.slice(h.a, h.b) !== h.text) next.push(h)
      else if (h.id) gone.add(h.id) // the file now says the same: nothing left to suggest
    }
    hunks = settle(shared, next).hunks
    anchor(yt, hunks)
    if (gone.size || hunks.some((h) => !h.id || unsaved(h))) later() // carried others' changes in, or new
    show()
  }
  load()
  const onTransaction = (tr: Y.Transaction) => {
    if (tr.origin === ORIGIN) return
    const text = yt as unknown as Y.AbstractType<Y.YEvent<any>>, review = r.review as unknown as Y.AbstractType<Y.YEvent<any>>
    if (tr.changed.has(text) || tr.changed.has(review) || tr.changedParentTypes.has(review)) reconcile()
  }
  r.doc.on('afterTransaction', onTransaction)
  const onHide = () => { if (document.visibilityState === 'hidden') flush() }
  document.addEventListener('visibilitychange', onHide)
  addEventListener('pagehide', flush)

  /** Turn edits in the editor into changes to the suggestions. */
  function typed(tr: Transaction) {
    try {
      let cur = tr.startState.doc.toString()
      tr.changes.iterChanges((fromA, toA, fromB, _toB, ins) => {
        const s = ins.toString(), f = fromB, to = fromB + (toA - fromA) // positions after the earlier changes
        const res = edit(shared, hunks, cur, f, to, s)
        hunks = res.hunks
        res.gone.forEach((id) => gone.add(id))
        cur = cur.slice(0, f) + s + cur.slice(to)
      })
      anchor(yt, hunks)
      later()
      if (compose(shared, hunks) === tr.newDoc.toString()) return
      console.error('suggest mode: the view and the suggestions disagree; reloading them')
    } catch (e) { console.error('suggest mode:', e) }
    // never expected: start again from what's saved (losing only what was typed since)
    load()
    queueMicrotask(show)
  }

  /** Your suggestions in the view, as Settings says: by default not marked at all (they're in the
   *  Review tab); underlined, with a thin mark where something was removed (hover for the change);
   *  or removed text struck through and new text highlighted. */
  function decorations(): DecorationSet {
    const markup = editorMarkup.mode()
    if (markup === 'none') return Decoration.none
    const out = []
    const all = markup === 'all'
    let d = 0
    for (const hk of hunks) {
      let p = hk.a + d
      d += hk.text.length - (hk.b - hk.a)
      const base = shared.slice(hk.a, hk.b), title = changeTitle(null, base, hk.text)
      const attrs = { ...(hk.id ? { 'data-review': hk.id } : {}), title }
      for (const op of refine(wordDiff(base, hk.text))) {
        if (op.op === 'eq') p += op.text.length
        else if (op.op === 'ins') { out.push(Decoration.mark({ class: all ? 'cm-suggest-new' : 'cm-suggest-simple', attributes: attrs }).range(p, p + op.text.length)); p += op.text.length }
        else out.push(Decoration.widget({ widget: all ? new Struck(op.text, hk.id) : new ChangeMark('remove', title, hk.id), side: -1 }).range(p))
      }
    }
    return Decoration.set(out, true)
  }

  const plugin = ViewPlugin.fromClass(class {
    decorations: DecorationSet
    constructor(view: EditorView) { cm = view; this.decorations = decorations() }
    update(u: ViewUpdate) {
      for (const tr of u.transactions) if (tr.docChanged && !tr.annotation(fromShared)) typed(tr)
      this.decorations = decorations()
    }
    destroy() { cm = null }
  }, { decorations: (v) => v.decorations })

  const keepFocus = (e: Event) => e.preventDefault() // keep typing where you were
  const unwatchMarkup = editorMarkup.watch(() => put({}))
  const banner = showPanel.of(() => ({
    top: true,
    dom: h('div', { class: 'cm-suggest-banner', role: 'status' },
      h('span', {}, h('b', {}, t('Suggesting')), ' · ', t('Your edits become suggestions that others accept or reject. Compiling uses the file without them.')),
      h('button', { class: 'small', onmousedown: keepFocus, onclick: stop }, t('Stop suggesting'))),
  }))

  const session: SuggestSession = {
    initial: compose(shared, hunks),
    extension: [plugin, banner],
    toView: (x) => toView(hunks, x),
    toShared: (v, bias = -1) => toShared(hunks, v, bias),
    shared: () => shared,
    mine: (id) => hunks.some((x) => x.id === id),
    flush,
    destroy() {
      flush()
      sessions.delete(session)
      unwatchMarkup()
      clearTimeout(timer)
      r.doc.off('afterTransaction', onTransaction)
      document.removeEventListener('visibilitychange', onHide)
      removeEventListener('pagehide', flush)
    },
  }
  sessions.add(session)
  return session
}
