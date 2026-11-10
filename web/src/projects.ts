// The list of projects opened in this browser (start page). Kept in localStorage; the server
// can't read projects, so it can't list them for you (listsync.ts can keep an encrypted copy).
import { chats } from './assistant/chats'
import { forgetLocalCopy } from './replica'
import { b64d } from './crypto'
import { rememberFolder } from './foldersync'
import { t, locale } from './i18n'
import type { ListDoc, ListEntry } from './listcore'
import { tags } from './tags'

export interface Recent {
  project: string // unique id (from the link)
  fragment: string // link fragment with the keys
  name: string // shared project name
  at: number // last opened (ms)
  created?: number // when this project joined the list (ms): made here, or first opened
  modified?: number // when its files last changed, as far as this device saw (ms)
  writable: boolean
  nick?: string // private label, only in this browser
  docTitle?: string // \title{…} of the main file
  people?: string[] // collaborators seen in this project, most recent first
  createdBy?: string
  files?: number
  storage?: 'server' | 'relay'
  atRisk?: boolean // no server copy as far as this browser knows
}

const KEY = 'texloom.recent'
const read = (): Recent[] => { try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] } }
/** Where a tab keeps the link it was opened with (see addressbar.ts). */
export const tabKeysItem = (project: string) => `texloom.keys.${project}`

const GONE = 'texloom.recentGone' // project -> when it was removed, so other devices remove it too
let changed: (() => void) | null = null
const MAX_KEPT = 1000 // the most recently opened; a synced list keeps the rest on the server
const write = (list: Recent[], quietly = false) => {
  try { localStorage.setItem(KEY, JSON.stringify([...list].sort((a, b) => b.at - a.at).slice(0, MAX_KEPT))) } catch {}
  if (!quietly) changed?.()
}
const readGone = (): Record<string, number> => { try { return JSON.parse(localStorage.getItem(GONE) || '{}') } catch { return {} } }
const writeGone = (gone: Record<string, number>) => { try { localStorage.setItem(GONE, JSON.stringify(gone)) } catch {} }

/** Short, readable project code derived from its id, e.g. "K7P2-QX4M" (Crockford
 *  base32: no lookalike letters, no '-' or '_' from the base64 id). */
export function projectCode(id: string): string {
  const A = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  let bits = 0, n = 0, out = ''
  for (const byte of b64d(id).subarray(0, 5)) { // 40 bits -> 8 characters
    n = (n << 8) | byte; bits += 8
    while (bits >= 5) { out += A[(n >> (bits - 5)) & 31]; bits -= 5 }
  }
  return `${out.slice(0, 4)}-${out.slice(4, 8)}`
}

export const projects = {
  list(): Recent[] {
    return read().sort((a, b) => b.at - a.at)
  },

  /** Insert or merge. An edit link is never downgraded by later opening a read-only one. */
  update(project: string, patch: Partial<Recent>) {
    const list = read()
    let r = list.find((x) => x.project === project)
    if (!r) {
      if (!patch.fragment) return
      r = { project, fragment: patch.fragment, name: '', at: Date.now(), created: Date.now(), writable: !!patch.writable }
      list.push(r)
    }
    const keep = r.writable && patch.writable === false ? r.fragment : null // don't lose the edit link
    Object.assign(r, Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined)))
    if (keep) { r.writable = true; r.fragment = keep }
    write(list)
  },

  onChange(f: () => void) { changed = f },

  /** This browser's list, in the form kept on the server. */
  asList(): ListDoc {
    const out: Record<string, ListEntry> = {}
    for (const r of read()) {
      const e: ListEntry = { f: r.fragment, n: r.name, at: r.at, w: r.writable }
      if (r.nick) e.nick = r.nick
      if (r.docTitle) e.t = r.docTitle
      if (r.createdBy) e.by = r.createdBy
      if (r.created) e.c = r.created
      if (r.modified) e.m = r.modified
      out[r.project] = e
    }
    return { v: 1, projects: out, gone: readGone() }
  },

  /** Take on the merged list. True if what this browser shows changed. */
  applyList(doc: ListDoc): boolean {
    const before = read()
    const keepFields = (r: Recent) => [r.project, r.fragment, r.name, r.at, r.writable, r.nick ?? '', r.docTitle ?? '', r.createdBy ?? '', r.created ?? 0, r.modified ?? 0]
    const same = (l: Recent[]) => JSON.stringify(l.map(keepFields).sort())
    const next = Object.entries(doc.projects).map(([project, e]): Recent => {
      const had = before.find((x) => x.project === project)
      return {
        ...had,
        project, fragment: e.f, name: e.n ?? '', at: e.at, writable: !!e.w, nick: e.nick, docTitle: e.t, createdBy: e.by,
        created: e.c ?? had?.created ?? e.at, modified: e.m ?? had?.modified,
      }
    }).sort((a, b) => b.at - a.at)
    write(next, true)
    writeGone(doc.gone)
    return same(before) !== same(next)
  },

  /** Remember collaborators' names seen in a project (excluding yourself). */
  sawPeople(project: string, names: string[], me: string) {
    const r = read().find((x) => x.project === project)
    if (!r) return
    const fresh = names.map((n) => String(n ?? '').slice(0, 64)).filter((n) => n && n !== me && !/^anon-\d+$/.test(n))
    if (!fresh.length) return
    const people = [...new Set([...fresh, ...(r.people ?? [])])].slice(0, 8)
    if (people.join('\n') !== (r.people ?? []).join('\n')) projects.update(project, { people })
  },

  /** The best link this browser has for a project. */
  fragmentOf(project: string): string | null {
    return read().find((r) => r.project === project)?.fragment ?? null
  },

  async forget(project: string) {
    await forgetLocalCopy(project)
    try { sessionStorage.removeItem(tabKeysItem(project)) } catch {}
    writeGone({ ...readGone(), [project]: Date.now() })
    write(read().filter((x) => x.project !== project))
    tags.forgetProject(project)
    await chats.forgetProject(project).catch(() => {})
    // every per-project setting (texloom.<what>.<project>) and the linked folder
    try { for (const k of Object.keys(localStorage)) if (k.startsWith('texloom.') && k.endsWith(`.${project}`)) localStorage.removeItem(k) } catch {}
    await rememberFolder(project, null).catch(() => {})
  },
}

// Entries from before dates were kept: when they were last opened is the best guess there is.
if (read().some((r) => !r.created)) write(read().map((r) => ({ ...r, created: r.created ?? r.at })), true)

/** Pull a plain-text title out of a LaTeX source, if it has \title{…}. */
export function latexTitle(src: string): string | undefined {
  const m = /\\title\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}/.exec(src)
  if (!m) return undefined
  const t = m[1]
    .replace(/\\\\/g, ' ')
    .replace(/\\[a-zA-Z]+\*?\s*/g, '')
    .replace(/[{}~]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return t ? t.slice(0, 120) : undefined
}

export function whenOpened(at: number): string {
  const d = new Date(at), now = new Date()
  const time = d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day(now) - day(d)) / 86400000)
  if (diff === 0) return t('today {{time}}', { time })
  if (diff === 1) return t('yesterday {{time}}', { time })
  if (diff < 7) return d.toLocaleDateString(locale, { weekday: 'long' })
  return d.toLocaleDateString(locale, { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}
