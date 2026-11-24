// What keeping a project in step with a git repository (github.ts) and with a Dropbox folder
// (dropbox.ts) share: the project's files as both compare them, applying what came in (taken
// files and three-way merges) in one transaction, what this browser approved, and the tab's id
// for the leases.
import * as Y from 'yjs'
import type { Replica } from './replica'
import { applyText, merge3, merge3Strict } from './diff'
import { isText } from './projectfiles'
import { readJson, session, writeJson } from './store'
import type { ShaCache } from './gitshacache'

/** Files larger than this aren't synced (as everywhere in TeXLoom). */
export const MAX = 50 * 1024 * 1024

/** A project file as syncing compares it: a text's content, or a stored file's id and size. */
export type Mine = { text: string } | { h: string; size: number }

/** The project's files that `keep` takes (given the path, and the size: 0 for a text), by `at(path)`. */
export function projectFiles(r: Replica, keep: (rel: string, size: number) => boolean, at = (rel: string) => rel): Map<string, Mine> {
  const out = new Map<string, Mine>()
  r.files.forEach((yt, p) => { if (keep(p, 0)) out.set(at(p), { text: yt.toString() }) })
  for (const [p, b] of r.bin.entries()) if (b && typeof b.h === 'string' && keep(p, b.size ?? 0)) out.set(at(p), { h: b.h, size: b.size })
  return out
}

/** The file at `rel` as it is now. */
const mineNow = (r: Replica, rel: string): Mine | null => {
  const yt = r.files.get(rel), b = r.bin.get(rel)
  return yt ? { text: yt.toString() } : b ? { h: b.h, size: b.size } : null
}
const sameMine = (x: Mine | null, y: Mine | null) => (!x || !y ? x === y : 'text' in x ? 'text' in y && x.text === y.text : 'h' in y && x.h === y.h)

export const bytesOf = async (r: Replica, m: Mine) => ('text' in m ? new TextEncoder().encode(m.text) : await r.getBlob(m.h))

/** The TeXLoom ids of the project's stored files: all a hash cache needs to keep (gitshacache.ts). */
export function storedIds(r: Replica): string[] {
  const now: string[] = []
  for (const b of r.bin.values()) if (b && typeof b.h === 'string') now.push(b.h)
  return now
}

/** A file's hash by `hash` (git's id, Dropbox's content hash); a stored file's from `cache` once known. */
export async function hashOf(r: Replica, m: Mine, cache: ShaCache, hash: (d: Uint8Array) => Promise<string>): Promise<string> {
  if ('text' in m) return hash(new TextEncoder().encode(m.text))
  const known = cache.get(m.h)
  if (known) return known
  const h = await hash(await r.getBlob(m.h))
  cache.set(m.h, h)
  return h
}

/** What to do with a file the other side changed: take its version (`take`; null: it's deleted
 *  there), or merge texts three ways. `was`: the project's file when this was decided. */
export interface Step { rel: string; was: Mine | null; take?: Uint8Array | null; merge?: { base: string; theirs: string } }

/** The steps whose text merge clashes (none when the project's version wins). */
export const clashing = (steps: Step[], oursWins: boolean): string[] =>
  oursWins ? [] : steps.filter((st) => st.merge && merge3Strict(st.merge.base, st.merge.theirs, (st.was as { text: string }).text) === null).map((st) => st.rel)

/** Store the incoming files that aren't text (before the transaction: storing waits): their ids, by path. */
export async function storeBinaries(r: Replica, files: Iterable<readonly [string, Uint8Array | null | undefined]>): Promise<Map<string, string>> {
  const ids = new Map<string, string>()
  for (const [rel, d] of files) if (d && isText(rel, d) === null) ids.set(rel, await r.putBlob(d))
  return ids
}

/** Put a file that came in into the project: as its text, or the stored file (`ids` from storeBinaries). */
export function putFile(r: Replica, rel: string, data: Uint8Array, ids: Map<string, string>) {
  const text = isText(rel, data)
  if (text === null) { r.files.delete(rel); r.bin.set(rel, { h: ids.get(rel)!, size: data.length }); return }
  r.bin.delete(rel)
  const yt = r.files.get(rel)
  if (yt) applyText(yt, yt.toString(), text); else r.files.set(rel, new Y.Text(text))
}

/** Apply the steps (call it in a transaction): the files changed, or null, changing nothing, if
 *  the project changed underneath since they were decided (the next round decides again). */
export function applySteps(r: Replica, steps: Step[], ids: Map<string, string>, oursWins: boolean): string[] | null {
  const files: string[] = []
  const todo: (() => void)[] = []
  for (const st of steps) {
    const cur = mineNow(r, st.rel)
    if (st.merge) {
      if (!cur || !('text' in cur)) return null
      const merged = oursWins ? merge3(st.merge.base, st.merge.theirs, cur.text) : merge3Strict(st.merge.base, st.merge.theirs, cur.text)
      if (merged === null) return null
      if (merged !== cur.text) { const yt = r.files.get(st.rel)!; todo.push(() => applyText(yt, cur.text, merged)); files.push(st.rel) }
      continue
    }
    if (!sameMine(cur, st.was)) return null
    const data = st.take
    todo.push(data ? () => putFile(r, st.rel, data, ids) : () => { r.files.delete(st.rel); r.bin.delete(st.rel) })
    files.push(st.rel)
  }
  todo.forEach((f) => f())
  return files
}

// The link is in the project's shared settings, which any collaborator can write, and syncing uses
// this browser's own token: so a browser syncs only a link its user made or approved here, kept
// in this browser by project (texloom.<name>Approved.<project>; the start page shows them).
export const approval = <T>(name: string) => {
  const key = (r: Replica) => `texloom.${name}Approved.${r.keys.project}`
  return {
    get: (r: Replica): T | null => readJson<T | null>(key(r), null),
    set: (r: Replica, v: T | null) => { writeJson(key(r), v) },
  }
}

/** This tab's id for the leases (a reload keeps it, another tab gets its own). */
export function tabId(): string {
  let id = session.get('texloom.tab')
  if (!id) { id = 'web:' + crypto.randomUUID(); session.set('texloom.tab', id) }
  return id
}
