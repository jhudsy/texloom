// Comments and suggested changes, anchored to text with Yjs relative positions so they
// stay attached while people edit around them. A suggestion changes nothing until
// someone accepts it.
import * as Y from 'yjs'
import { StateEffect, StateField, type Extension } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, WidgetType, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import type { Replica } from './replica'
import { h } from './dom'
import { t, locale } from './i18n'

export type Kind = 'comment' | 'suggestion'
export interface Item {
  id: string; kind: Kind; file: string; quote: string; text: string; by: string; at: number
  resolved: boolean; outcome?: 'accepted' | 'rejected'; replies: { by: string; at: number; text: string }[]
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
    at: Number(m.get('at')) || 0, resolved: !!m.get('resolved'), outcome: m.get('outcome'),
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
  if (current !== it.quote && !confirm(t('The text has changed since this was suggested.\n\nNow: “{{now}}”\nSuggested replacement: “{{suggested}}”\n\nApply anyway?', { now: current.slice(0, 200), suggested: it.text.slice(0, 200) }))) return 'cancelled'
  r.doc.transact(() => {
    yt.delete(rg.from, rg.to - rg.from)
    if (it.text) yt.insert(rg.from, it.text)
    const m = r.review.get(id)!
    m.set('resolved', true); m.set('outcome', 'accepted')
  })
  return null
}

// ---- editor decorations ----------------------------------------------------------
class InsertWidget extends WidgetType {
  constructor(readonly text: string, readonly id: string) { super() }
  eq(o: InsertWidget) { return o.text === this.text && o.id === this.id }
  toDOM() { return h('span', { class: 'cm-suggest-ins', 'data-review': this.id, title: t('Suggested replacement') }, this.text || '⌫') }
}

const setDecos = StateEffect.define<DecorationSet>()
const decoField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update: (v, tr) => { for (const e of tr.effects) if (e.is(setDecos)) return e.value; return v.map(tr.changes) },
  provide: (f) => EditorView.decorations.from(f),
})

/** Highlights for open comments and suggestions in `file`; clicking one calls onPick(id). */
export function reviewExtension(r: Replica, file: string, onPick: (id: string) => void): Extension {
  const compute = (view: EditorView) => {
    const len = view.state.doc.length
    const ranges = []
    for (const i of allItems(r)) {
      if (i.file !== file || i.resolved) continue
      const rg = rangeOf(r, i.id)
      if (!rg) continue
      const from = Math.min(rg.from, len), to = Math.min(rg.to, len)
      if (i.kind === 'comment') {
        if (to > from) ranges.push(Decoration.mark({ class: 'cm-comment', attributes: { 'data-review': i.id, title: `${i.by}: ${i.text.slice(0, 200)}` } }).range(from, to))
      } else {
        if (to > from) ranges.push(Decoration.mark({ class: 'cm-suggest-del', attributes: { 'data-review': i.id, title: t('{{name}} suggests a change', { name: i.by }) } }).range(from, to))
        ranges.push(Decoration.widget({ widget: new InsertWidget(i.text, i.id), side: 1 }).range(to))
      }
    }
    view.dispatch({ effects: setDecos.of(Decoration.set(ranges, true)) })
  }
  const plugin = ViewPlugin.define((view) => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const schedule = () => { clearTimeout(timer); timer = setTimeout(() => compute(view), 120) }
    r.review.observeDeep(schedule)
    schedule()
    return {
      update(u: ViewUpdate) { if (u.docChanged) schedule() },
      destroy() { clearTimeout(timer); r.review.unobserveDeep(schedule) },
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
export interface ReviewHost {
  replica: Replica
  writable: boolean
  userName: () => string
  selection: () => { file: string; from: number; to: number; text: string } | null
  reveal: (file: string, from: number, to: number) => void
}

export interface ReviewPanel { el: HTMLElement; add(kind: Kind): void; focus(id: string): void }

export function createReviewPanel(host: ReviewHost): ReviewPanel {
  const r = host.replica
  let showResolved = false
  let focused: string | null = null
  const list = h('div', { class: 'review-list' })
  const resolvedChk = h('input', { type: 'checkbox', 'aria-label': t('Show resolved'), onchange: (e: any) => { showResolved = e.target.checked; render() } })
  const el = h('div', { class: 'review' },
    h('div', { class: 'ai-bar' },
      host.writable ? h('button', { title: t('Comment on the selected text (⌘⌥M / Ctrl+Alt+M)'), onclick: () => add('comment') }, t('💬 Comment')) : null,
      host.writable ? h('button', { title: t('Suggest a replacement for the selected text (⌘⌥E / Ctrl+Alt+E)'), onclick: () => add('suggestion') }, t('✎ Suggest')) : null,
      h('span', { class: 'spacer' }),
      h('label', { class: 'check small' }, resolvedChk, t('Show resolved'))),
    list)

  function add(kind: Kind) {
    const sel = host.selection()
    if (!sel) return alert(t('Open a text file and select some text first.'))
    if (kind === 'comment' && sel.from === sel.to) return alert(t('Select the text you want to comment on.'))
    const text = prompt(kind === 'comment' ? t('Comment:') : t('Replace “{{text}}” with:', { text: sel.text.slice(0, 80) + (sel.text.length > 80 ? '…' : '') }), kind === 'suggestion' ? sel.text : '')
    if (text === null || (kind === 'comment' && !text.trim())) return
    const id = addItem(r, kind, sel.file, sel.from, sel.to, text, host.userName())
    if (id) focus(id)
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
    const replyIn = h('input', { placeholder: t('Reply…'), 'aria-label': t('Reply') })
    replyIn.addEventListener('keydown', (e) => { if (e.key === 'Enter' && replyIn.value.trim()) { reply(r, it.id, replyIn.value, host.userName()); replyIn.value = '' } })
    return h('div', { class: `rcard ${it.kind} ${it.resolved ? 'resolved' : ''} ${it.id === focused ? 'focused' : ''}`, 'data-id': it.id },
      h('div', { class: 'rhead', onclick: go, title: rg ? t('Show in the editor') : t('The text it referred to was deleted') },
        h('span', { class: 'rkind' }, it.kind === 'comment' ? '💬' : '✎'),
        h('span', { class: 'rloc' }, it.file), h('span', { class: 'muted small' }, ` · ${it.by} · ${when(it.at)}`)),
      it.kind === 'comment'
        ? h('div', {}, h('div', { class: 'rquote' }, it.quote.slice(0, 300)), h('div', { class: 'rtext' }, it.text))
        : h('div', { class: 'rsuggest' }, h('del', {}, it.quote.slice(0, 500) || t('(insert here)')), ' → ', h('ins', {}, it.text.slice(0, 500) || t('(delete)'))),
      it.resolved ? h('div', { class: 'muted small' }, it.outcome ? (it.outcome === 'accepted' ? t('Suggestion accepted') : t('Suggestion rejected')) : t('Resolved')) : null,
      ...it.replies.map((rp) => h('div', { class: 'rreply' }, h('b', {}, String(rp.by).slice(0, 64)), ` ${rp.text}`, h('span', { class: 'muted small' }, ` · ${when(rp.at)}`))),
      host.writable ? h('div', { class: 'ractions' },
        it.resolved ? h('button', { onclick: () => setResolved(r, it.id, false) }, t('Reopen')) : replyIn,
        !it.resolved && it.kind === 'suggestion' ? h('button', { class: 'primary', onclick: () => { const m = accept(r, it.id); if (m && m !== 'cancelled') alert(m) } }, t('Accept')) : null,
        !it.resolved && it.kind === 'suggestion' ? h('button', { onclick: () => setResolved(r, it.id, true, 'rejected') }, t('Reject')) : null,
        !it.resolved && it.kind === 'comment' ? h('button', { onclick: () => setResolved(r, it.id, true) }, t('Resolve')) : null,
        h('button', { class: 'icon', title: t('Delete'), 'aria-label': t('Delete'), onclick: () => { if (confirm(t('Delete this for everyone?'))) r.review.delete(it.id) } }, '✕')) : null)
  }

  function render() {
    const items = allItems(r).filter((i) => showResolved || !i.resolved).sort((a, b) => a.file.localeCompare(b.file) || (rangeOf(r, a.id)?.from ?? 0) - (rangeOf(r, b.id)?.from ?? 0))
    list.replaceChildren(...(items.length ? items.map(card)
      : [h('p', { class: 'muted small', style: 'padding: 8px 10px' }, host.writable
        ? t('No open comments or suggestions. Select text in the editor, then 💬 Comment or ✎ Suggest.')
        : t('No open comments or suggestions.'))]))
  }

  r.review.observeDeep(() => render())
  render()
  return { el, add, focus }
}
