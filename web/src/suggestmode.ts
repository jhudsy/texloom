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
import { yRemoteSelectionsTheme } from 'y-codemirror.next'
import { h } from './dom'
import { t } from './i18n'
import { addItem, allItems, ChangeMark, changeTitle, editorMarkup, onFlushTyped, rangeOf } from './review'
import type { Replica } from './replica'
import { anchor, compose, edit, loadHunks, normalize, refresh, saveHunks, settle, toShared, toView, type Hunk } from './suggestcore'
import { commonEnds, refine, wordDiff } from './worddiff'

/** Marks changes to the review map that this tab's suggesting made (not news to it). */
const ORIGIN = { suggestMode: true }
/** Marks editor changes that come from the shared document rather than from typing. */
const fromShared = Annotation.define<boolean>()
/** Whose typed suggestions these are. This browser's, for this project (a different id in each
 *  project, so they can't be linked), so that they can be edited again on a later visit; but
 *  while one tab suggests in a file it holds a lock on that file, and another tab of the same
 *  browser uses its own page's id there, so two tabs never edit the same ones. */
const PAGE = crypto.randomUUID()
function browserId(project: string): string {
  try {
    const key = `texloom.suggester.${project}`
    const id = localStorage.getItem(key) || crypto.randomUUID()
    localStorage.setItem(key, id)
    return id
  } catch { return PAGE }
}

/** The suggestions this browser made in a project. The `typed` id in a suggestion is public, so
 *  anyone could write one with it: only those recorded here are taken back as one's own. */
function madeHere(project: string) {
  const key = `texloom.madehere.${project}`
  let ids: Set<string>
  try { ids = new Set(JSON.parse(localStorage.getItem(key) || '[]')) } catch { ids = new Set() }
  const save = () => { try { localStorage.setItem(key, JSON.stringify([...ids].slice(-5000))) } catch {} }
  return {
    has: (id: string) => ids.has(id),
    add(more: Iterable<string>) { const n = ids.size; for (const id of more) ids.add(id); if (ids.size !== n) save() },
    /** Forget ones that no longer exist. */
    keep(exists: (id: string) => boolean) { const n = ids.size; for (const id of ids) if (!exists(id)) ids.delete(id); if (ids.size !== n) save() },
  }
}
const LOCK_FRESH = 15_000 // ms: a lock not renewed for this long was left by a tab that's gone

/** Take this browser's suggestions in `file` for this tab, if no other tab has them. Returns the
 *  id to use, and how to let go. */
function claim(project: string, file: string): { me: string; release(): void } {
  const key = `texloom.suggestlock.${project}.${file}`
  const mine = () => JSON.stringify({ tab: PAGE, at: Date.now() })
  try {
    const held = JSON.parse(localStorage.getItem(key) || 'null') as { tab: string; at: number } | null
    if (held && held.tab !== PAGE && Date.now() - held.at < LOCK_FRESH) return { me: PAGE, release() {} }
    const BROWSER = browserId(project)
    localStorage.setItem(key, mine())
    const renew = setInterval(() => { try { localStorage.setItem(key, mine()) } catch {} }, LOCK_FRESH / 3)
    return { me: BROWSER, release() { clearInterval(renew); try { if (JSON.parse(localStorage.getItem(key) || 'null')?.tab === PAGE) localStorage.removeItem(key) } catch {} } }
  } catch { return { me: PAGE, release() {} } }
}
const SAVE_AFTER = 800 // ms after typing stops

const sessions = new Set<{ flush(): void }>()
/** Save what's been typed in every open suggesting editor, before something reads or settles
 *  suggestions (accepting one, the assistant). */
export function flushSuggestions() { sessions.forEach((x) => x.flush()) }
onFlushTyped(flushSuggestions)
/** Suggest mode for the project open in this page: on or off, remembered per project in this
 *  browser. The editor and the Review tab watch it; other ways of changing text (search and
 *  replace, adding a package for a symbol, a reference) suggest too while it's on. */
export const suggestMode = (() => {
  let on = false, key = '', allowed = false
  const watchers = new Set<() => void>()
  return {
    /** For the project being opened. `by` names whoever suggests. */
    start(project: string, writable: boolean, by: () => string) {
      key = `texloom.suggesting.${project}`
      allowed = writable
      suggester = by
      try { on = writable && localStorage.getItem(key) === '1' } catch { on = false }
    },
    on: () => on,
    set(v: boolean) {
      v &&= allowed
      if (v === on) return
      on = v
      try { if (on) localStorage.setItem(key, '1'); else localStorage.removeItem(key) } catch {}
      watchers.forEach((f) => f())
    },
    watch(f: () => void) { watchers.add(f); return () => { watchers.delete(f) } },
  }
})()
export const isSuggesting = () => suggestMode.on()
let suggester = () => ''

/** Replace [from, to) of a file, or, while suggesting, suggest it (once: the same suggestion
 *  again, from a second click, is left out). */
export function changeOrSuggest(r: Replica, file: string, from: number, to: number, text: string) {
  const yt = r.files.get(file)
  if (!yt) return
  if (!suggestMode.on()) { r.doc.transact(() => { if (to > from) yt.delete(from, to - from); if (text) yt.insert(from, text) }); return }
  const quote = yt.toString().slice(from, to)
  const same = allItems(r).some((i) => {
    if (i.kind !== 'suggestion' || i.resolved || i.file !== file || i.quote !== quote || i.text !== text) return false
    const at = rangeOf(r, i.id)
    return !!at && at.from === from && at.to === to
  })
  if (!same) addItem(r, 'suggestion', file, from, to, text, suggester())
}

/** Many replacements at once (search and replace all): each file read once and the existing
 *  suggestions listed once, not per match. An edit whose text no longer says `was` is skipped. */
export function changeOrSuggestAll(r: Replica, edits: { file: string; from: number; to: number; was: string; text: string }[]) {
  const key = (file: string, from: number, to: number, quote: string, text: string) => JSON.stringify([file, from, to, quote, text])
  const suggesting = suggestMode.on()
  const existing = new Set<string>()
  if (suggesting) {
    for (const i of allItems(r)) {
      if (i.kind !== 'suggestion' || i.resolved) continue
      const at = rangeOf(r, i.id)
      if (at) existing.add(key(i.file, at.from, at.to, i.quote, i.text))
    }
  }
  const byFile = new Map<string, typeof edits>()
  for (const e of edits) byFile.set(e.file, [...(byFile.get(e.file) ?? []), e])
  r.doc.transact(() => {
    for (const [file, list] of byFile) {
      const yt = r.files.get(file)
      if (!yt) continue
      const text = yt.toString()
      for (const e of [...list].sort((a, b) => b.from - a.from)) { // from the end, so earlier places stay put
        if (text.slice(e.from, e.to) !== e.was) continue
        if (!suggesting) { if (e.to > e.from) yt.delete(e.from, e.to - e.from); if (e.text) yt.insert(e.from, e.text); continue }
        if (!existing.has(key(file, e.from, e.to, e.was, e.text))) addItem(r, 'suggestion', file, e.from, e.to, e.text, suggester())
      }
    }
  })
}

/** Open suggestions in `file` whose new text contains `text` (so as not to suggest it twice). */
export const suggested = (r: Replica, file: string, text: string) =>
  suggestMode.on() && allItems(r).some((i) => i.kind === 'suggestion' && !i.resolved && i.file === file && i.text.includes(text))

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

/** A collaborator's cursor, drawn as the usual editor binding draws it. */
class Caret extends WidgetType {
  constructor(readonly color: string, readonly name: string) { super() }
  eq(o: Caret) { return o.color === this.color && o.name === this.name }
  toDOM() {
    return h('span', { class: 'cm-ySelectionCaret', style: `background-color: ${this.color}; border-color: ${this.color}` },
      '\u2060', h('div', { class: 'cm-ySelectionCaretDot' }), '\u2060', h('div', { class: 'cm-ySelectionInfo' }, this.name), '\u2060')
  }
  ignoreEvent() { return true }
}

class Struck extends WidgetType {
  constructor(readonly text: string, readonly id: string | null) { super() }
  eq(o: Struck) { return o.text === this.text && o.id === this.id }
  toDOM() { return h('span', { class: 'cm-suggest-del cm-suggest-gone', ...(this.id ? { 'data-review': this.id } : {}), title: t('Your suggestion removes this') }, this.text) }
  ignoreEvent() { return false }
}

export function suggestSession(r: Replica, file: string, yt: Y.Text, by: () => string, stop: () => void): SuggestSession {
  const lock = claim(r.keys.project, file)
  const ME = lock.me
  const mine = madeHere(r.keys.project)
  mine.keep((id) => r.review.has(id))
  let shared = yt.toString()
  let hunks: Hunk[] = []
  let gone = new Set<string>()
  let dirty = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let cm: EditorView | null = null
  /** What each suggestion holds as stored, to tell what's been typed since. */
  const saved = new Map<string, string>()
  const sig = (h: Hunk) => `${h.conflict ? 1 : 0}${h.text}`
  const remember = () => { saved.clear(); for (const h of hunks) if (h.id) saved.set(h.id, sig(h)) }
  const unsaved = (h: Hunk) => !!h.id && saved.get(h.id) !== sig(h)
  /** Start again from what's stored. */
  function load() {
    const res = loadHunks(r.doc, r.review, yt, file, ME, mine.has)
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
    if (r.files.get(file) !== yt) return // the file was moved, replaced or deleted: its anchors would be wrong
    const kept = hunks.filter((h) => !h.id || r.review.has(h.id)) // (reconcile drops deleted ones first)
    const lost = kept.length !== hunks.length
    hunks = saveHunks(r.doc, r.review, yt, kept, gone, { file, by: by(), me: ME, origin: ORIGIN })
    mine.add(hunks.flatMap((h) => (h.id ? [h.id] : [])))
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
    const [p, q] = commonEnds(next, now)
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
  let redraw = true // the decorations need drawing again even if the text hasn't changed
  function put(spec: Parameters<EditorView['dispatch']>[0]) {
    const v = cm
    if (!v) return
    redraw = true
    try { v.dispatch(spec); tries = 0 }
    catch (e) {
      if (++tries > 3) { tries = 0; console.error('suggest mode: could not update the editor', e); return }
      queueMicrotask(() => { if (cm === v) show() })
    }
  }

  /** The shared text changed, or suggestions were accepted, rejected or deleted. */
  function reconcile() {
    shared = yt.toString()
    const res = refresh(r.doc, yt, hunks, shared)
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
    anchor(yt, hunks, shared)
    if (gone.size || hunks.some((h) => !h.id || unsaved(h))) later() // carried others' changes in, or new
    show()
  }
  load()
  const onTransaction = (tr: Y.Transaction) => {
    if (tr.origin === ORIGIN) return
    const text = yt as unknown as Y.AbstractType<Y.YEvent<any>>, review = r.review as unknown as Y.AbstractType<Y.YEvent<any>>
    if (tr.changed.has(text)) reconcile()
    // otherwise only a review change matters that settled one of these suggestions (accepted,
    // rejected or deleted); suggestions elsewhere, and replies, don't
    else if ((tr.changed.has(review) || tr.changedParentTypes.has(review)) && hunks.some((h) => h.id && !isOpen(h.id))) reconcile()
  }
  r.doc.on('afterTransaction', onTransaction)
  const onHide = () => { if (document.visibilityState === 'hidden') flush() }
  document.addEventListener('visibilitychange', onHide)
  const onLeave = () => { flush(); lock.release() } // a reload must find the file free again
  addEventListener('pagehide', onLeave)

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
      anchor(yt, hunks, shared)
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
      if (u.docChanged || redraw) { this.decorations = decorations(); redraw = false } // not for scrolling or the cursor
    }
    destroy() { cm = null }
  }, { decorations: (v) => v.decorations })

  /** Collaborators' cursors and selections, found in this view of the file; and this person's, put
   *  where others look for it (on the shared text, as the usual editor binding does). */
  const cursors = ViewPlugin.fromClass(class {
    decorations: DecorationSet = Decoration.none
    private sent = ''
    private moved = false // someone else's cursor moved since the last drawing
    private listener = ({ added, updated, removed }: { added: number[]; updated: number[]; removed: number[] }) => {
      if ([...added, ...updated, ...removed].some((id) => id !== r.awareness.clientID)) { this.moved = true; put({}) }
    }
    constructor(view: EditorView) { r.awareness.on('change', this.listener); this.decorations = this.draw(view) }
    update(u: ViewUpdate) {
      if (u.selectionSet || u.focusChanged || u.docChanged) this.publish(u.view)
      if (u.docChanged || this.moved) { this.decorations = this.draw(u.view); this.moved = false }
    }
    destroy() { r.awareness.off('change', this.listener) }
    private publish(view: EditorView) {
      const sel = view.hasFocus ? view.state.selection.main : null
      const at = (v: number) => Y.createRelativePositionFromTypeIndex(yt, toShared(hunks, v, -1))
      const cursor = sel ? { anchor: at(sel.anchor), head: at(sel.head) } : null
      const key = JSON.stringify(cursor)
      if (key === this.sent) return
      this.sent = key
      queueMicrotask(() => r.awareness.setLocalStateField('cursor', cursor)) // not in the middle of an editor update
    }
    private draw(view: EditorView): DecorationSet {
      const out = [], len = view.state.doc.length
      for (const [id, st] of r.awareness.getStates()) {
        if (id === r.awareness.clientID || !st?.cursor) continue
        const where = (j: unknown) => {
          try {
            const a = Y.createAbsolutePositionFromRelativePosition(Y.createRelativePositionFromJSON(j), r.doc)
            return a && a.type === yt ? Math.min(len, toView(hunks, a.index)) : null
          } catch { return null }
        }
        const a = where(st.cursor.anchor), b = where(st.cursor.head)
        if (a === null || b === null) continue
        const user = st.user ?? {}
        const color = typeof user.color === 'string' ? user.color : '#888', light = typeof user.colorLight === 'string' ? user.colorLight : `${color}33`
        if (a !== b) out.push(Decoration.mark({ class: 'cm-ySelection', attributes: { style: `background-color: ${light}` } }).range(Math.min(a, b), Math.max(a, b)))
        out.push(Decoration.widget({ widget: new Caret(color, String(user.name ?? '').slice(0, 64)), side: b > a ? -1 : 1 }).range(b))
      }
      return Decoration.set(out, true)
    }
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
    extension: [plugin, cursors, yRemoteSelectionsTheme, banner],
    toView: (x) => toView(hunks, x),
    toShared: (v, bias = -1) => toShared(hunks, v, bias),
    shared: () => shared,
    mine: (id) => hunks.some((x) => x.id === id),
    flush,
    destroy() {
      flush()
      lock.release()
      sessions.delete(session)
      unwatchMarkup()
      r.doc.off('afterTransaction', onTransaction)
      document.removeEventListener('visibilitychange', onHide)
      removeEventListener('pagehide', onLeave)
    },
  }
  sessions.add(session)
  return session
}
