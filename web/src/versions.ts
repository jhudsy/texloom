// Saved versions. A Yjs snapshot lists everything deleted so far, and the document never
// forgets anything stored in it, so keeping a whole snapshot per version made documents
// grow with the square of their history. Versions are now a chain: each link holds only
// what changed since the link it builds on. Versions saved before this keep their own
// whole snapshot (`snap`). No DOM here, so scripts/check-versions.mjs can test it.
import * as Y from 'yjs'
import type { Replica } from './replica'

/** Every link stays: later versions are built on it. Removing a version only unlists it. */
export interface Link { id: string; base: string | null; snap: Uint8Array }
export interface Version { at: number; by: string; label?: string; auto?: boolean; snap?: Uint8Array; link?: string }
type Saved = Pick<Replica, 'doc' | 'versions' | 'versionChain'>
type Deletions = ReturnType<typeof Y.createDeleteSet>

const MAX_AUTO = 300
const FULL_EVERY = 64 // every so many links, one holds the whole snapshot, so rebuilding one stays bounded

/** The deletions in `now` that aren't in `had`. Both sorted and merged, as Yjs makes them. */
function newDeletions(now: Deletions, had: Deletions): Deletions {
  const out = Y.createDeleteSet()
  now.clients.forEach((items, client) => {
    const old = had.clients.get(client) ?? []
    const kept: { clock: number; len: number }[] = []
    let first = 0
    for (const { clock, len } of items) {
      const end = clock + len
      let at = clock
      while (first < old.length && old[first].clock + old[first].len <= at) first++
      for (let i = first; i < old.length && old[i].clock < end; i++) {
        if (old[i].clock > at) kept.push({ clock: at, len: old[i].clock - at })
        at = Math.max(at, old[i].clock + old[i].len)
      }
      if (at < end) kept.push({ clock: at, len: end - at })
    }
    if (kept.length) out.clients.set(client, kept as typeof items)
  })
  return out
}

function newClocks(now: Map<number, number>, had: Map<number, number>): Map<number, number> {
  return new Map([...now].filter(([client, clock]) => clock > (had.get(client) ?? 0)))
}

const indexOf = (chain: Y.Array<Link>) => new Map(chain.toArray().map((l) => [l?.id, l]))

function combine(parts: Y.Snapshot[]): Y.Snapshot {
  const clocks = new Map<number, number>()
  for (const p of parts) p.sv.forEach((clock, client) => clocks.set(client, Math.max(clock, clocks.get(client) ?? 0)))
  return Y.createSnapshot(Y.mergeDeleteSets(parts.map((p) => p.ds)), clocks)
}

/** The whole snapshot at a link: everything from there back to a link that needs no base. */
function rebuild(links: Map<string, Link>, id: string): Y.Snapshot { return rebuildAt(links, id).snap }

function rebuildAt(links: Map<string, Link>, id: string): { snap: Y.Snapshot; depth: number } {
  const parts: Y.Snapshot[] = [], seen = new Set<string>()
  for (let at: string | null = id; at !== null;) {
    const link = links.get(at)
    if (!link || seen.has(at)) throw new Error('a link in its chain is missing')
    seen.add(at)
    parts.push(Y.decodeSnapshot(link.snap))
    at = link.base
  }
  return { snap: combine(parts), depth: parts.length - 1 }
}

/** The newest link's whole snapshot, kept from the last save, so the next save needn't rebuild
 *  the chain (which grows with every save). Only while that link is still the newest. */
const tipCache = new WeakMap<Y.Array<Link>, { id: string; snap: Y.Snapshot; depth: number }>()

/** A link for `now`. If the chain can't reproduce `now` exactly (damaged, or it knows of
 *  deletions this device hasn't seen yet), the link holds the whole snapshot instead. */
export function nextLink(chain: Y.Array<Link>, now: Y.Snapshot): Link {
  const id = crypto.randomUUID().slice(0, 13)
  try {
    const tip = chain.get(chain.length - 1)
    const cached = tipCache.get(chain)
    const { snap: had, depth } = cached?.id === tip.id ? cached : rebuildAt(indexOf(chain), tip.id)
    if (depth + 1 < FULL_EVERY) {
      const snap = Y.encodeSnapshot(Y.createSnapshot(newDeletions(now.ds, had.ds), newClocks(now.sv, had.sv)))
      if (Y.equalSnapshots(combine([had, Y.decodeSnapshot(snap)]), now)) {
        tipCache.set(chain, { id, snap: now, depth: depth + 1 })
        return { id, base: tip.id, snap }
      }
    }
  } catch { /* an empty or damaged chain: start again from a whole snapshot */ }
  tipCache.set(chain, { id, snap: now, depth: 0 })
  return { id, base: null, snap: Y.encodeSnapshot(now) }
}

/** Names given to versions after they were saved, by version (time and author). A map, so
 *  two people renaming the same version at once end with one name, not two copies of it.
 *  An empty name removes the name. */
const versionNames = (r: Pick<Replica, 'doc'>) => r.doc.getMap<string>('versionNames')
export const observeNames = (r: Pick<Replica, 'doc'>, fn: () => void) => versionNames(r).observe(fn)
const versionKey = (v: Pick<Version, 'at' | 'by'>) => `${v.at}:${v.by}`
export function versionLabel(r: Pick<Replica, 'doc'>, v: Version): string | undefined {
  const n = versionNames(r).get(versionKey(v))
  return typeof n === 'string' ? n || undefined : v.label
}
export function nameVersion(r: Pick<Replica, 'doc'>, v: Version, label: string) {
  versionNames(r).set(versionKey(v), label.trim().slice(0, 120))
}

export function saveVersion(r: Saved, by: string, label?: string, auto = false) {
  const link = nextLink(r.versionChain, Y.snapshot(r.doc))
  r.doc.transact(() => {
    r.versionChain.push([link])
    r.versions.push([{ at: Date.now(), by, ...(label ? { label } : {}), ...(auto ? { auto: true } : {}), link: link.id }])
    // keep the newest automatic versions; named ones (also named later) are kept forever
    const prunable = (v: any) => v?.auto && !versionLabel(r, v)
    let excess = r.versions.toArray().filter(prunable).length - MAX_AUTO
    for (let i = 0; i < r.versions.length && excess > 0;) {
      if (prunable(r.versions.get(i))) { r.versions.delete(i, 1); excess-- } else i++
    }
  })
}

/** The project as it was at a version (a read-only Y.Doc). */
export function docAt(r: Saved, v: Version): Y.Doc {
  return Y.createDocFromSnapshot(r.doc, v.link ? rebuild(indexOf(r.versionChain), v.link) : Y.decodeSnapshot(v.snap!))
}
