// Comments and suggested changes, anchored to text with Yjs relative positions so they
// stay attached while people edit around them. A suggestion changes nothing until
// someone accepts it.
import * as Y from 'yjs'
import { StateEffect, StateField, type Extension } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, WidgetType, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import type { Replica } from './replica'
import { h } from './dom'
import { t, locale } from './i18n'
import { applyChange } from './suggestcore'
import { flushSuggestions } from './suggestmode'
import { refine, wordDiff, type DiffOp } from './worddiff'

export type Kind = 'comment' | 'suggestion'
export interface Item {
  id: string; kind: Kind; file: string; quote: string; text: string; by: string; at: number
  resolved: boolean; outcome?: 'accepted' | 'rejected'; replies: { by: string; at: number; text: string }[]
  conflict?: boolean // someone changed the text it replaces while it was being typed (suggest mode)
}

// ---- data ---------------------------------------------------------------------
export function addItem(r: Replica, kind: Kind, file: string, from: number, to: number, text: string, by: string): string | null {
  const yt = r.files.get(file)
  if (!yt || to < from) return null
  const id = crypto.randomUUID()
  const m = new Y.Map<any>()
  r.doc.transact(() => {
    m.set('kind', kind); m.set('file', file); m.set('text', text.slice(0, 20000)); m.set('by', by); m.set('at', Date.now())
    m.set('quote', yt.toString().slice(from, to).slice(0, 20000)); m.set('resolved', false)
    m.set('start', Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(yt, from)))
    m.set('end', Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(yt, to, -1)))
    m.set('replies', new Y.Array())
    r.review.set(id, m)
  })
  return id
}

export function itemOf(r: Replica, id: string): Item | null {
  const m = r.review.get(id)
  if (!(m instanceof Y.Map)) return null
  const replies = m.get('replies')
  return {
    id, kind: m.get('kind') === 'suggestion' ? 'suggestion' : 'comment', file: String(m.get('file') ?? ''),
    quote: String(m.get('quote') ?? ''), text: String(m.get('text') ?? ''), by: String(m.get('by') ?? '?').slice(0, 64),
    at: Number(m.get('at')) || 0, resolved: !!m.get('resolved'), outcome: m.get('outcome'), conflict: !!m.get('conflict'),
    replies: replies instanceof Y.Array ? replies.toArray().filter((x: any) => x && typeof x.text === 'string') : [],
  }
}

export function allItems(r: Replica): Item[] {
  return [...r.review.keys()].map((id) => itemOf(r, id)).filter((x): x is Item => !!x)
}

/** Current [from, to) of an item in its file, or null if its text is gone. */
export function rangeOf(r: Replica, id: string): { from: number; to: number } | null {
  const m = r.review.get(id)
  const yt = m && r.files.get(m.get('file'))
  if (!m || !yt) return null
  try {
    const a = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(m.get('start')), r.doc)
    const b = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(m.get('end')), r.doc)
    if (!a || !b || a.type !== yt || b.type !== yt) return null
    return { from: Math.min(a.index, b.index), to: Math.max(a.index, b.index) }
  } catch { return null }
}

export function reply(r: Replica, id: string, text: string, by: string) {
  const m = r.review.get(id)
  const arr = m?.get('replies')
  if (arr instanceof Y.Array && text.trim()) arr.push([{ by, at: Date.now(), text: text.trim().slice(0, 20000) }])
}

export function setResolved(r: Replica, id: string, resolved: boolean, outcome?: 'accepted' | 'rejected') {
  const m = r.review.get(id)
  if (!m) return
  r.doc.transact(() => { m.set('resolved', resolved); if (outcome) m.set('outcome', outcome); else m.delete('outcome') })
}

/** Apply a suggestion. Returns a message if it couldn't be applied cleanly. */
export function accept(r: Replica, id: string): string | null {
  const it = itemOf(r, id), rg = rangeOf(r, id)
  const yt = it && r.files.get(it.file)
  if (!it || !yt || !rg) return t('The text this suggestion refers to no longer exists.')
  const current = yt.toString().slice(rg.from, rg.to)
  if ((current !== it.quote || it.conflict) && !confirm(t('The text has changed since this was suggested.\n\nNow: “{{now}}”\nSuggested replacement: “{{suggested}}”\n\nApply anyway?', { now: current.slice(0, 200), suggested: it.text.slice(0, 200) }))) return 'cancelled'
  r.doc.transact(() => {
    if (current === it.quote) applyChange(yt, rg.from, it.quote, it.text) // only the words that differ
    else { yt.delete(rg.from, rg.to - rg.from); if (it.text) yt.insert(rg.from, it.text) }
    const m = r.review.get(id)!
    m.set('resolved', true); m.set('outcome', 'accepted')
  })
  return null
}

// ---- editor decorations ----------------------------------------------------------
/** How suggestions show in the editor (Settings): 'none', the default, leaves the text as it is
 *  and the changes in the Review tab; 'simple' underlines them, with the details on hover; 'all'
 *  shows removed text struck through and new text inline. Each browser's own choice. */
export type Markup = 'none' | 'simple' | 'all'
export const editorMarkup = (() => {
  const KEY = 'texloom.markup'
  let mode: Markup = 'none'
  try { const v = localStorage.getItem(KEY); if (v === 'simple' || v === 'all') mode = v } catch {}
  const watchers = new Set<() => void>()
  return {
    mode: () => mode,
    set(v: Markup) {
      mode = v
      try { if (v === 'none') localStorage.removeItem(KEY); else localStorage.setItem(KEY, v) } catch {}
      watchers.forEach((f) => f())
    },
    watch(f: () => void) { watchers.add(f); return () => { watchers.delete(f) } },
  }
})()

/** A suggestion's change in a sentence, for hovering: “old” → “new”. `by` is null for your own. */
export function changeTitle(by: string | null, quote: string, text: string): string {
  const o = { name: by ?? '', text: (quote || text).slice(0, 300), old: quote.slice(0, 300), new: text.slice(0, 300) }
  if (by === null) return !quote ? t('You suggest adding “{{text}}”', o) : !text ? t('You suggest removing “{{text}}”', o) : t('You suggest “{{old}}” → “{{new}}”', o)
  return !quote ? t('{{name}} suggests adding “{{text}}”', o) : !text ? t('{{name}} suggests removing “{{text}}”', o) : t('{{name}} suggests “{{old}}” → “{{new}}”', o)
}

/** In the simple view: a thin mark where text would be added or removed. */
export class ChangeMark extends WidgetType {
  constructor(readonly kind: 'add' | 'remove', readonly title: string, readonly id: string | null) { super() }
  eq(o: ChangeMark) { return o.kind === this.kind && o.title === this.title && o.id === this.id }
  toDOM() { return h('span', { class: `cm-change-mark ${this.kind}`, title: this.title, ...(this.id ? { 'data-review': this.id } : {}) }) }
  ignoreEvent() { return false }
}

class InsertWidget extends WidgetType {
  constructor(readonly text: string, readonly id: string, readonly by: string) { super() }
  eq(o: InsertWidget) { return o.text === this.text && o.id === this.id && o.by === this.by }
  toDOM() { return h('span', { class: 'cm-suggest-ins', 'data-review': this.id, title: t('{{name}} suggests adding this', { name: this.by }) }, this.text || '⌫') }
}

const setDecos = StateEffect.define<DecorationSet>()
const decoField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update: (v, tr) => { for (const e of tr.effects) if (e.is(setDecos)) return e.value; return v.map(tr.changes) },
  provide: (f) => EditorView.decorations.from(f),
})

/** Highlights for open comments and suggestions in `file`; clicking one calls onPick(id). */
/** The text a comment or suggestion is being written about. */
const pending = new WeakMap<Replica, { file: string; start: Y.RelativePosition; end: Y.RelativePosition }>()
const pendingWatchers = new Set<() => void>()
function setPending(r: Replica, p: { file: string; start: Y.RelativePosition; end: Y.RelativePosition } | null) {
  if (p) pending.set(r, p); else pending.delete(r)
  pendingWatchers.forEach((f) => f())
}

/** While suggesting, the editor shows its own view of the file: where shared positions are in
 *  it, and which suggestions it draws itself. */
export interface ReviewMap { toView(x: number): number; skip(id: string): boolean }

export function reviewExtension(r: Replica, file: string, onPick: (id: string) => void, map?: ReviewMap): Extension {
  const compute = (view: EditorView) => {
    const len = view.state.doc.length
    const at = (x: number) => Math.min(map ? map.toView(x) : x, len)
    const shared = r.files.get(file)?.toString() ?? ''
    const ranges = []
    for (const i of allItems(r)) {
      if (i.file !== file || i.resolved || map?.skip(i.id)) continue
      const rg = rangeOf(r, i.id)
      if (!rg) continue
      const from = at(rg.from), to = at(rg.to)
      if (i.kind === 'comment') {
        if (to > from) ranges.push(Decoration.mark({ class: 'cm-comment', attributes: { 'data-review': i.id, title: `${i.by}: ${i.text.slice(0, 200)}` } }).range(from, to))
        continue
      }
      if (editorMarkup.mode() === 'none') continue // only in the Review tab
      if (editorMarkup.mode() === 'simple') { // the text as it is, underlined where a change is suggested
        const title = changeTitle(i.by, i.quote, i.text)
        if (to > from) ranges.push(Decoration.mark({ class: 'cm-suggest-simple', attributes: { 'data-review': i.id, title } }).range(from, to))
        else ranges.push(Decoration.widget({ widget: new ChangeMark('add', title, i.id), side: 1 }).range(to))
        continue
      }
      const del = (a: number, b: number) => { if (b > a) ranges.push(Decoration.mark({ class: 'cm-suggest-del', attributes: { 'data-review': i.id, title: t('{{name}} suggests removing this', { name: i.by }) } }).range(a, b)) }
      const ins = (x: number, text: string) => ranges.push(Decoration.widget({ widget: new InsertWidget(text, i.id, i.by), side: 1 }).range(x))
      if (shared.slice(rg.from, rg.to) === i.quote) { // as a diff: only the words (or letters) that change
        let p = rg.from
        for (const op of refine(wordDiff(i.quote, i.text))) {
          if (op.op === 'eq') p += op.text.length
          else if (op.op === 'del') { del(at(p), at(p + op.text.length)); p += op.text.length }
          else ins(at(p), op.text)
        }
        if (i.quote === i.text) ins(to, '') // nothing would change: still something to click
      } else { // the text has changed since: the whole of it, and the whole replacement
        del(from, to)
        ins(to, i.text)
      }
    }
    const p = pending.get(r)
    if (p?.file === file) {
      const a = Y.createAbsolutePositionFromRelativePosition(p.start, r.doc), b = Y.createAbsolutePositionFromRelativePosition(p.end, r.doc)
      if (a && b && at(b.index) > at(a.index)) ranges.push(Decoration.mark({ class: 'cm-review-pending' }).range(at(a.index), at(b.index)))
    }
    view.dispatch({ effects: setDecos.of(Decoration.set(ranges, true)) })
  }
  const plugin = ViewPlugin.define((view) => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const schedule = () => { clearTimeout(timer); timer = setTimeout(() => compute(view), 120) }
    r.review.observeDeep(schedule)
    pendingWatchers.add(schedule)
    const unwatch = editorMarkup.watch(schedule)
    schedule()
    return {
      update(u: ViewUpdate) { if (u.docChanged) schedule() },
      destroy() { clearTimeout(timer); r.review.unobserveDeep(schedule); pendingWatchers.delete(schedule); unwatch() },
    }
  })
  return [
    decoField, plugin,
    EditorView.domEventHandlers({
      click: (e) => {
        const id = (e.target as HTMLElement).closest('[data-review]')?.getAttribute('data-review')
        if (id) onPick(id)
        return false
      },
    }),
  ]
}

// ---- panel ------------------------------------------------------------------------
const CONTEXT = 60 // characters of unchanged text kept on each side of a change, in a card

/** A suggestion as a word diff: removed words struck through, new ones underlined, with long
 *  unchanged stretches shortened. */
function suggestionDiff(quote: string, text: string): HTMLElement {
  const ops: DiffOp[] = wordDiff(quote, text)
  const out = h('div', { class: 'rsuggest' })
  if (!ops.some((o) => o.op !== 'eq')) { out.append(h('span', { class: 'muted' }, t('(no change)'))); return out }
  ops.forEach((o, i) => {
    if (o.op === 'del') out.append(h('del', { title: t('Removed') }, o.text))
    else if (o.op === 'ins') out.append(h('ins', { title: t('Added') }, o.text))
    else {
      const first = i === 0, last = i === ops.length - 1
      const keep = (first || last ? 1 : 2) * CONTEXT
      if (o.text.length <= keep + 1) out.append(o.text)
      else if (first) out.append('…', o.text.slice(-CONTEXT))
      else if (last) out.append(o.text.slice(0, CONTEXT), '…')
      else out.append(o.text.slice(0, CONTEXT), ' … ', o.text.slice(-CONTEXT))
    }
  })
  return out
}

export interface ReviewHost {
  replica: Replica
  writable: boolean
  /** Suggest mode: typing in the editor makes suggestions. */
  suggesting: { on(): boolean; set(on: boolean): void; watch(f: () => void): void }
  userName: () => string
  selection: () => { file: string; from: number; to: number; text: string } | null
  reveal: (file: string, from: number, to: number) => void
}

export interface ReviewPanel { el: HTMLElement; addComment(): void; focus(id: string): void }

export function createReviewPanel(host: ReviewHost): ReviewPanel {
  const r = host.replica
  let showResolved = false
  let focused: string | null = null
  const list = h('div', { class: 'review-list' })
  const composer = h('div', { class: 'rcompose', hidden: true })
  let draft: { area: HTMLTextAreaElement } | null = null
  const replies = new Map<string, string>() // half-written replies: the list is redrawn whenever anyone comments
  const resolvedChk = h('input', { type: 'checkbox', 'aria-label': t('Show resolved'), onchange: (e: any) => { showResolved = e.target.checked; render() } })
  const modeBtn = h('button', { class: 'suggest-mode', title: t('Suggest mode: what you type in the editor becomes suggestions for others to accept or reject (⌘⌥E / Ctrl+Alt+E)'),
    onclick: () => host.suggesting.set(!host.suggesting.on()) })
  const showMode = () => {
    const on = host.suggesting.on()
    modeBtn.textContent = on ? t('✎ Suggest mode: on') : t('✎ Suggest mode')
    modeBtn.classList.toggle('on', on)
    modeBtn.setAttribute('aria-pressed', String(on))
  }
  host.suggesting.watch(showMode)
  showMode()
  const el = h('div', { class: 'review' },
    h('div', { class: 'ai-bar' },
      host.writable ? h('button', { title: t('Comment on the selected text (⌘⌥M / Ctrl+Alt+M)'), onclick: () => addComment() }, t('💬 Comment')) : null,
      host.writable ? modeBtn : null,
      h('span', { class: 'spacer' }),
      h('label', { class: 'check small' }, resolvedChk, t('Show resolved'))),
    composer,
    list)

  function addComment() {
    const sel = host.selection()
    if (!sel) return alert(t('Open a text file and select some text first.'))
    if (sel.from === sel.to) return alert(t('Select the text you want to comment on.'))
    write(sel)
  }

  /** A comment is written here rather than in a dialog, so that the editor stays visible and usable.
   *  The text it's about stays highlighted, and can move or go as people keep editing. (Changes are
   *  suggested by typing, in suggest mode.) */
  function write(sel: { file: string; from: number; to: number; text: string }) {
    if (draft && draft.area.value.trim()) { draft.area.focus(); return } // don't lose what's been written
    const yt = r.files.get(sel.file)
    if (!yt) return
    const start = Y.createRelativePositionFromTypeIndex(yt, sel.from), end = Y.createRelativePositionFromTypeIndex(yt, sel.to, -1)
    const area = h('textarea', { class: 'review-write', rows: 4, placeholder: t('Write your comment…'), 'aria-label': t('Add a comment') }) as HTMLTextAreaElement
    const close = () => { draft = null; composer.hidden = true; composer.replaceChildren(); setPending(r, null) }
    const save = () => {
      const text = area.value
      if (!text.trim()) { area.focus(); return }
      const a = Y.createAbsolutePositionFromRelativePosition(start, r.doc), b = Y.createAbsolutePositionFromRelativePosition(end, r.doc)
      close()
      if (!a || !b || a.type !== yt || b.type !== yt || b.index <= a.index) { alert(t('The text you selected has since been removed.')); return }
      const id = addItem(r, 'comment', sel.file, a.index, b.index, text, host.userName())
      if (id) focus(id)
    }
    area.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save() }
      else if (e.key === 'Escape' && !area.value.trim()) close()
    })
    const show = () => {
      const a = Y.createAbsolutePositionFromRelativePosition(start, r.doc), b = Y.createAbsolutePositionFromRelativePosition(end, r.doc)
      if (a && b && a.type === yt) host.reveal(sel.file, a.index, b.index)
    }
    const quote = sel.text.length > 400 ? `${sel.text.slice(0, 400)}…` : sel.text
    composer.replaceChildren(
      h('b', {}, t('Add a comment')),
      h('blockquote', { class: 'review-quote', title: t('Show in the editor'), onclick: show }, quote),
      area,
      h('div', { class: 'row end' },
        h('span', { class: 'muted small spacer' }, t('⌘Enter or Ctrl+Enter to save')),
        h('button', { onclick: close }, t('Cancel')),
        h('button', { class: 'primary', onclick: save }, t('Add comment'))))
    composer.hidden = false
    draft = { area }
    setPending(r, { file: sel.file, start, end })
    area.focus()
  }

  function focus(id: string) {
    focused = id
    render()
    list.querySelector(`[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' })
  }

  const when = (at: number) => new Date(at).toLocaleString(locale, { dateStyle: 'short', timeStyle: 'short' })

  function card(it: Item): HTMLElement {
    const rg = rangeOf(r, it.id)
    const go = () => { if (rg) host.reveal(it.file, rg.from, rg.to) }
    const grow = () => { replyIn.style.height = 'auto'; replyIn.style.height = `${replyIn.scrollHeight + 2}px` }
    const replyIn = h('textarea', { class: 'rreply-in', rows: 1, placeholder: t('Reply…'), 'aria-label': t('Reply'), title: t('Enter to send, Shift+Enter for a new line'), 'data-reply': it.id,
      oninput: () => { replies.set(it.id, replyIn.value); grow() } }) as HTMLTextAreaElement
    replyIn.value = replies.get(it.id) ?? ''
    if (replyIn.value) requestAnimationFrame(grow)
    replyIn.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return
      e.preventDefault()
      if (!replyIn.value.trim()) return
      replies.delete(it.id)
      reply(r, it.id, replyIn.value, host.userName())
      replyIn.value = ''
    })
    return h('div', { class: `rcard ${it.kind} ${it.resolved ? 'resolved' : ''} ${it.id === focused ? 'focused' : ''}`, 'data-id': it.id },
      h('div', { class: 'rhead', onclick: go, title: rg ? t('Show in the editor') : t('The text it referred to was deleted') },
        h('span', { class: 'rkind' }, it.kind === 'comment' ? t('💬 Comment') : t('✎ Suggestion')),
        h('span', { class: 'rloc' }, it.file), h('span', { class: 'muted small' }, ` · ${it.by} · ${when(it.at)}`)),
      it.kind === 'comment'
        ? h('div', {}, h('div', { class: 'rquote' }, it.quote.slice(0, 300)), h('div', { class: 'rtext' }, it.text))
        : suggestionDiff(it.quote, it.text),
      it.resolved ? h('div', { class: 'muted small' }, it.outcome ? (it.outcome === 'accepted' ? t('Suggestion accepted') : t('Suggestion rejected')) : t('Resolved')) : null,
      ...it.replies.map((rp) => h('div', { class: 'rreply' }, h('b', {}, String(rp.by).slice(0, 64)), ` ${rp.text}`, h('span', { class: 'muted small' }, ` · ${when(rp.at)}`))),
      host.writable ? h('div', { class: 'ractions' },
        it.resolved ? h('button', { onclick: () => setResolved(r, it.id, false) }, t('Reopen')) : replyIn,
        !it.resolved && it.kind === 'suggestion' ? h('button', { class: 'primary', onclick: () => { flushSuggestions(); const m = accept(r, it.id); if (m && m !== 'cancelled') alert(m) } }, t('Accept')) : null,
        !it.resolved && it.kind === 'suggestion' ? h('button', { onclick: () => { flushSuggestions(); setResolved(r, it.id, true, 'rejected') } }, t('Reject')) : null,
        !it.resolved && it.kind === 'comment' ? h('button', { onclick: () => setResolved(r, it.id, true) }, t('Resolve')) : null,
        h('button', { class: 'icon', title: t('Delete'), 'aria-label': t('Delete'), onclick: () => { if (confirm(t('Delete this for everyone?'))) { flushSuggestions(); r.review.delete(it.id) } } }, '✕')) : null)
  }

  function render() {
    const items = allItems(r).filter((i) => showResolved || !i.resolved).sort((a, b) => a.file.localeCompare(b.file) || (rangeOf(r, a.id)?.from ?? 0) - (rangeOf(r, b.id)?.from ?? 0))
    const typing = document.activeElement instanceof HTMLTextAreaElement && list.contains(document.activeElement) ? document.activeElement : null
    const caret = typing?.selectionStart ?? 0, typingIn = typing?.getAttribute('data-reply')
    list.replaceChildren(...(items.length ? items.map(card)
      : [h('p', { class: 'muted small', style: 'padding: 8px 10px' }, host.writable
        ? t('No open comments or suggestions. Select text in the editor and choose 💬 Comment, or turn on ✎ Suggest mode and type.')
        : t('No open comments or suggestions.'))]))
    if (typingIn) { // carry on where they were
      const again = list.querySelector<HTMLTextAreaElement>(`[data-reply="${CSS.escape(typingIn)}"]`)
      again?.focus()
      again?.setSelectionRange(caret, caret)
    }
  }

  r.review.observeDeep(() => render())
  render()
  return { el, addComment, focus }
}
