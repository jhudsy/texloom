// History: automatic and named versions (Yjs snapshots), viewing a past version,
// comparing with now, restoring files, and who wrote which line.
import * as Y from 'yjs'
import type { Replica } from './replica'
import { h, askText } from './dom'
import { diffLines, diffStats, type Op } from './diff'
import { putText } from './projectfiles'
import { t, tn, locale } from './i18n'
import { docAt, nameVersion, observeNames, saveVersion, versionLabel, type Version } from './versions'

const AUTO_IDLE = 2 * 60_000 // auto-save after this long without local edits

/** Save an automatic version after a pause in local editing, and when leaving the page. */
export function autoSave(r: Replica, by: () => string) {
  if (!r.keys.writable) return
  let dirty = false, last = 0
  r.localChange.push(() => { dirty = true; last = Date.now() })
  const flush = () => { if (dirty) { dirty = false; saveVersion(r, by(), undefined, true) } }
  setInterval(() => { if (dirty && Date.now() - last > AUTO_IDLE) flush() }, 30_000)
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush() })
}

type Snap = { files: Map<string, string>; bin: Map<string, { h: string; size: number }> }
function contents(d: Y.Doc): Snap {
  const files = new Map<string, string>(), bin = new Map<string, { h: string; size: number }>()
  d.getMap<Y.Text>('files').forEach((t, p) => files.set(p, t.toString()))
  d.getMap<{ h: string; size: number }>('bin').forEach((b, p) => bin.set(p, b))
  return { files, bin }
}

// ---------------------------------------------------------------------------
// Who wrote this: the author of most characters on each line.
// ---------------------------------------------------------------------------
export function lineAuthors(yt: Y.Text): number[] {
  const owners: number[] = [] // per character
  let item = (yt as any)._start as Y.Item | null
  while (item) {
    if (!item.deleted && item.content instanceof Y.ContentString) {
      const n = item.length
      for (let i = 0; i < n; i++) owners.push(item.id.client)
    }
    item = item.right
  }
  const text = yt.toString()
  const out: number[] = []
  let start = 0
  for (const line of text.split('\n')) {
    const counts = new Map<number, number>()
    for (let i = start; i < start + line.length; i++) counts.set(owners[i], (counts.get(owners[i]) ?? 0) + 1)
    let best = -1, bestN = -1 // -1: empty line, nobody to credit
    for (const [c, n] of counts) if (n > bestN) { best = c; bestN = n }
    out.push(best)
    start += line.length + 1
  }
  return out
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------
export interface HistoryHost {
  replica: Replica
  writable: boolean
  userName: () => string
  showAuthors: (on: boolean) => void
  openFile: (path: string) => void
}

const when = (at: number) => new Date(at).toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' })

// what happened to a file since a version (the key is also its CSS class)
const STATUS: Record<string, string> = {
  changed: t('changed'), deleted: t('deleted since'), added: t('added since'), 'changed-file': t('changed (file)'), 'changed-type': t('changed type'),
}

export function createHistoryPanel(host: HistoryHost): HTMLElement {
  const r = host.replica
  const list = h('ul', { class: 'versions' })
  const detail = h('div', { class: 'version-detail' })
  const authorsChk = h('input', { type: 'checkbox', 'aria-label': t('Show who wrote each line'), onchange: (e: any) => host.showAuthors(e.target.checked) })
  let selected: Version | null = null
  let filter: 'all' | 'named' = 'all'

  const filterSel = h('select', { 'aria-label': t('Which versions'), onchange: (e: any) => { filter = e.target.value; render() } },
    h('option', { value: 'all' }, t('All versions')), h('option', { value: 'named' }, t('Named only')))

  const panel = h('div', { class: 'history' },
    h('div', { class: 'ai-bar' },
      host.writable ? h('button', { class: 'primary', onclick: async () => {
        const label = (await askText(t('Name this version (e.g. “Submitted to journal”):'), '', 120))?.trim()
        if (label) saveVersion(r, host.userName(), label.slice(0, 120))
      } }, t('Save version…')) : null,
      filterSel,
      h('span', { class: 'spacer' }),
      h('label', { class: 'check small' }, authorsChk, t('Who wrote this'))),
    h('div', { class: 'history-body' }, list, detail))

  function render() {
    const all = r.versions.toArray().filter((v) => v && (v.snap || v.link)) as Version[]
    const shown = all.filter((v) => filter === 'all' || versionLabel(r, v)).sort((a, b) => b.at - a.at)
    if (!shown.length) {
      list.replaceChildren(h('li', { class: 'muted small' },
        all.length ? t('No named versions yet.') : t('No versions yet. Versions are saved automatically a couple of minutes after you stop editing; use “Save version…” to name one.')))
      return
    }
    list.replaceChildren(...shown.map((v) => { const label = versionLabel(r, v); return h('li', {
      class: `${v === selected ? 'active' : ''} ${label ? 'named' : ''}`,
      onclick: () => { selected = v; render(); showVersion(v) },
    },
      h('div', { class: 'vlabel' }, label ? `★ ${label}` : t('Auto-saved')),
      h('div', { class: 'muted small' }, `${when(v.at)} · ${v.by}`)) }))
  }

  function showVersion(v: Version) {
    let past: Snap
    try { past = contents(docAt(r, v)) } catch (e) {
      detail.replaceChildren(h('p', { class: 'err' }, t('This version can’t be reconstructed ({{error}}). It predates full history.', { error: e })))
      return
    }
    const now = contents(r.doc)
    const paths = [...new Set([...past.files.keys(), ...past.bin.keys(), ...now.files.keys(), ...now.bin.keys()])].sort()
    const rows: HTMLElement[] = []
    for (const p of paths) {
      const a = past.files.get(p), b = now.files.get(p)
      const ab = past.bin.get(p), bb = now.bin.get(p)
      let status = '', stats = ''
      if (a !== undefined && b !== undefined) {
        if (a === b) continue
        const d = diffStats(diffLines(a, b)); status = 'changed'; stats = `+${d.added} −${d.removed}`
      } else if ((a !== undefined || ab) && b === undefined && !bb) status = 'deleted'
      else if (a === undefined && !ab && (b !== undefined || bb)) status = 'added'
      else if (ab && bb) { if (ab.h === bb.h) continue; status = 'changed-file' }
      else status = 'changed-type'
      rows.push(h('li', { onclick: () => showFile(v, p, past, now) },
        h('span', { class: 'path' }, p), h('span', { class: `vstat ${status.split('-')[0]}` }, `${STATUS[status]} ${stats}`)))
    }
    detail.replaceChildren(
      h('div', { class: 'vhead' },
        h('div', {}, h('b', {}, versionLabel(r, v) ?? t('Auto-saved')), h('div', { class: 'muted small' }, `${when(v.at)} · ${v.by}`)),
        host.writable ? h('button', { title: t('Give this version a name (named versions are kept for good)'), onclick: () => rename(v) }, versionLabel(r, v) ? t('Rename…') : t('Name…')) : null,
        host.writable ? h('button', { onclick: () => restoreAll(past) }, t('Restore all files…')) : null),
      rows.length ? h('p', { class: 'muted small' }, t('Changed since this version (click a file to compare):')) : h('p', { class: 'muted small' }, t('Nothing has changed since this version.')),
      h('ul', { class: 'vfiles' }, ...rows))
  }

  async function rename(v: Version) {
    const was = versionLabel(r, v)
    const label = await askText(was ? t('Rename this version (leave empty to remove the name):') : t('Name this version (e.g. “Submitted to journal”):'), was ?? '', 120)
    if (label === null || label.trim() === (was ?? '')) return
    nameVersion(r, v, label)
    if (selected === v) showVersion(v)
  }

  function showFile(v: Version, path: string, past: Snap, now: Snap) {
    const a = past.files.get(path), b = now.files.get(path)
    const back = h('button', { onclick: () => showVersion(v) }, t('← Files'))
    const restore = host.writable && (a !== undefined || past.bin.has(path))
      ? h('button', { class: 'primary', onclick: () => restoreFile(path, past) }, t('Restore this file')) : null
    const body = a !== undefined || b !== undefined
      ? fullDiff(diffLines(a ?? '', b ?? ''))
      : h('p', { class: 'muted' }, t('Binary file: compare by opening it.'))
    detail.replaceChildren(
      h('div', { class: 'vhead' }, back, h('span', { class: 'path' }, path), restore),
      h('p', { class: 'muted small' }, t('Red: in the version from {{when}} · Green: now', { when: when(v.at) })),
      body)
  }

  function restoreFile(path: string, past: Snap) {
    if (!confirm(t('Restore {{path}} to this version? The current text is replaced (and stays in history).', { path }))) return
    r.doc.transact(() => {
      const old = past.files.get(path)
      if (old !== undefined) putText(r, path, old)
      else { r.files.delete(path); r.bin.set(path, past.bin.get(path)!) }
    })
    host.openFile(path)
  }

  function restoreAll(past: Snap) {
    if (!confirm(t('Restore every file to this version? Files added since are removed. Everything stays in history.'))) return
    r.doc.transact(() => {
      for (const p of [...r.files.keys()]) if (!past.files.has(p)) r.files.delete(p)
      for (const p of [...r.bin.keys()]) if (!past.bin.has(p)) r.bin.delete(p)
      past.files.forEach((text, p) => putText(r, p, text))
      past.bin.forEach((b, p) => { if (r.bin.get(p)?.h !== b.h) r.bin.set(p, b) })
    })
    saveVersion(r, host.userName(), t('Restored version from {{when}}', { when: when(selected?.at ?? Date.now()) }))
  }

  r.versions.observe(render)
  observeNames(r, () => { render(); if (selected && detail.querySelector('.vfiles')) showVersion(selected) })
  render()
  return panel
}

/** Diff with long unchanged runs collapsed. */
function fullDiff(ops: Op[]): HTMLElement {
  const rows: HTMLElement[] = []
  const C = 3
  const line = (cls: string, n: number | string, t: string) =>
    rows.push(h('div', { class: `dl ${cls}` }, h('span', { class: 'ln' }, String(n)), t || ' '))
  for (let i = 0; i < ops.length; i++) {
    const o = ops[i]
    if (o.type !== 'eq') { line(o.type === 'del' ? 'del' : 'add', (o.type === 'del' ? o.a : o.b) + 1, o.text); continue }
    let j = i
    while (j < ops.length && ops[j].type === 'eq') j++
    const run = j - i
    if (run > 2 * C + 1) {
      for (let k = i; k < i + (i === 0 ? 0 : C); k++) line('ctx', ops[k].b + 1, ops[k].text)
      rows.push(h('div', { class: 'dl skip' }, tn(run - (i === 0 ? C : 2 * C) + (j === ops.length ? C : 0), '… {{n}} unchanged line', '… {{n}} unchanged lines')))
      for (let k = j - (j === ops.length ? 0 : C); k < j; k++) line('ctx', ops[k].b + 1, ops[k].text)
    } else for (let k = i; k < j; k++) line('ctx', ops[k].b + 1, ops[k].text)
    i = j - 1
  }
  return h('div', { class: 'diff full' }, ...rows)
}
