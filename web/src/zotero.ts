// Zotero: keep a .bib file in step with a Zotero collection, through Zotero's web API, from the
// browser (api.zotero.org allows that). Two levels, as for any integration:
// - the account (an API key) is yours alone: kept in this browser, and with a project list it
//   travels with the list, encrypted, like the creation code. It never enters a project.
// - the link ("this collection → refs.bib") is the project's, in its shared settings, so
//   collaborators see the file is generated and who keeps it. Refreshing needs access to the
//   collection: the person who linked it, or anyone in the same Zotero group.
import * as Y from 'yjs'
import type { Replica } from './replica'
import { applyText } from './diff'
import { t } from './i18n'

const API = 'https://api.zotero.org'
const STORE = 'texloom.zotero'
const STORE_AT = 'texloom.zoteroAt'

export interface Account { key: string; userID: number; username: string }
export interface Library { id: string; name: string } // id: users/<n> or groups/<n>
export interface Collection { key: string; name: string } // name includes its parents: "Thesis / Chapter 2"
/** A link, kept in the project's settings under `zotero`, by file. */
export interface Link { lib: string; libName: string; coll: string | null; collName: string; format: 'biblatex' | 'bibtex'; by: string; at: number }

/** This browser's Zotero account, and when it was set (so the latest follows the project list). */
export const zoteroAccount = {
  get(): Account | null {
    try { const a = JSON.parse(localStorage.getItem(STORE) || 'null'); return a && typeof a.key === 'string' ? a : null } catch { return null }
  },
  at(): number { try { return Number(localStorage.getItem(STORE_AT)) || 0 } catch { return 0 } },
  set(a: Account | null, at = Date.now()) {
    try {
      if (a) localStorage.setItem(STORE, JSON.stringify(a)); else localStorage.removeItem(STORE)
      localStorage.setItem(STORE_AT, String(at))
    } catch {}
  },
}

async function call(path: string, key: string | null, accept = 'application/json'): Promise<Response> {
  const res = await fetch(API + path, { headers: { 'Zotero-API-Version': '3', Accept: accept, ...(key ? { 'Zotero-API-Key': key } : {}) } })
  if (res.status === 403) throw new Error(t('Zotero refused: the API key doesn’t give access to this library.'))
  if (res.status === 404) throw new Error(t('Zotero doesn’t have that library or collection any more.'))
  if (res.status === 429) throw new Error(t('Zotero asks to slow down: try again in a minute.'))
  if (!res.ok) throw new Error(t('Zotero answered {{status}}.', { status: res.status }))
  return res
}

/** Every page of a list (Zotero gives at most 100 at a time). */
async function all<T>(path: string, key: string, accept?: string, each = 100): Promise<T[]> {
  const out: T[] = []
  for (let start = 0; ; start += each) {
    const res = await call(`${path}${path.includes('?') ? '&' : '?'}limit=${each}&start=${start}`, key, accept)
    const page = accept ? [await res.text()] as unknown as T[] : await res.json() as T[]
    out.push(...page)
    const total = Number(res.headers.get('Total-Results') ?? 0)
    if (start + each >= total) return out
  }
}

/** Check a key and learn whose it is. */
export async function connect(key: string): Promise<Account> {
  const k = key.trim()
  if (!/^[A-Za-z0-9]{20,40}$/.test(k)) throw new Error(t('That doesn’t look like a Zotero API key.'))
  const res = await fetch(`${API}/keys/${encodeURIComponent(k)}`, { headers: { 'Zotero-API-Version': '3' } })
  if (res.status === 404 || res.status === 403) throw new Error(t('Zotero doesn’t know that key. Check it, or make a new one.'))
  if (!res.ok) throw new Error(t('Zotero answered {{status}}.', { status: res.status }))
  const info = await res.json()
  if (typeof info?.userID !== 'number') throw new Error(t('Zotero doesn’t know that key. Check it, or make a new one.'))
  return { key: k, userID: info.userID, username: String(info.username ?? '') }
}

/** Your own library, and the groups the key can read. */
export async function libraries(a: Account): Promise<Library[]> {
  const groups = await all<{ id: number; data: { name: string } }>(`/users/${a.userID}/groups`, a.key)
  return [{ id: `users/${a.userID}`, name: t('My Library') }, ...groups.map((g) => ({ id: `groups/${g.id}`, name: g.data.name }))]
}

/** A library's collections, with their parents' names, in order. */
export async function collections(a: Account, lib: string): Promise<Collection[]> {
  const list = await all<{ key: string; data: { name: string; parentCollection: string | false } }>(`/${lib}/collections`, a.key)
  const byKey = new Map(list.map((c) => [c.key, c]))
  const path = (c: (typeof list)[number], depth = 0): string => {
    const parent = c.data.parentCollection && byKey.get(c.data.parentCollection)
    return parent && depth < 20 ? `${path(parent, depth + 1)} / ${c.data.name}` : c.data.name
  }
  return list.map((c) => ({ key: c.key, name: path(c) })).sort((x, y) => x.name.localeCompare(y.name))
}

/** The collection (or whole library) as BibTeX or BibLaTeX, with Zotero's own citation keys. */
export async function exportBib(a: Account, link: Pick<Link, 'lib' | 'coll' | 'format'>): Promise<string> {
  const where = link.coll ? `/${link.lib}/collections/${encodeURIComponent(link.coll)}/items/top` : `/${link.lib}/items/top`
  const pages = await all<string>(`${where}?format=${link.format}`, a.key, 'text/plain')
  return pages.map((p) => p.trim()).filter(Boolean).join('\n\n') + '\n'
}

/** The project's links: file → collection. */
export function links(r: Replica): Record<string, Link> {
  const v = r.meta.get('zotero')
  return v && typeof v === 'object' ? { ...(v as Record<string, Link>) } : {}
}

export function setLink(r: Replica, file: string, link: Link | null) {
  const next = links(r)
  if (link) next[file] = link; else delete next[file]
  r.meta.set('zotero', next)
}

/** Fetch the collection again and update the file with what changed. True if the file changed. */
export async function refresh(r: Replica, file: string, by: string): Promise<boolean> {
  const a = zoteroAccount.get()
  if (!a) throw new Error(t('Connect your Zotero account first (⚙ Settings on the start page, or here).'))
  const link = links(r)[file]
  if (!link) throw new Error(t('{{file}} isn’t linked to Zotero.', { file }))
  const text = await exportBib(a, link)
  let changed = false
  r.doc.transact(() => {
    let yt = r.files.get(file)
    const now = yt?.toString() ?? ''
    if (yt && now === text) return
    if (!yt) { yt = new Y.Text(); r.files.set(file, yt) }
    applyText(yt, now, text) // a generated file: changed directly, even while suggesting
    changed = true
  })
  setLink(r, file, { ...link, by, at: Date.now() })
  return changed
}
