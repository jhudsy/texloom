// Same-name files created apart. The `files` and `bin` maps keep one value per path: when
// two people create (or upload) the same path without having seen each other's, Yjs keeps
// one and deletes the other everywhere, silently. This finds the deleted one and puts it
// back under a new name, `name (conflict).ext`.
//
// Yjs (13.6) gives each write to a map key an Item whose `origin` is the item that was the
// key's value when it was written. A later write that saw the old value points at it; an
// item that lost a race has no successor pointing at it. Only the device that already held
// the losing version restores it: it knows exactly what that version said, and the device
// that held the winner never saw the loser's text before it was deleted.

import * as Y from 'yjs'
import type { Replica } from './replica'
import { exists } from './projectfiles'

export interface Clash { path: string; copy: string; by: string | null }

const MAX_WALK = 64 // a path's chain of past values; long ones are old history

/** `dir/name (conflict).ext`, or `(conflict 2)` and so on if that is taken. */
export function copyName(path: string, taken: (p: string) => boolean): string {
  const slash = path.lastIndexOf('/') + 1, dot = path.lastIndexOf('.')
  const [stem, ext] = dot > slash ? [path.slice(0, dot), path.slice(dot)] : [path, '']
  for (let i = 1; ; i++) {
    const p = `${stem} (conflict${i > 1 ? ` ${i}` : ''})${ext}`
    if (!taken(p)) return p
  }
}

const existed = (tx: Y.Transaction, id: Y.ID) => id.clock < (tx.beforeState.get(id.client) ?? 0)

/** The text `t` had just before `tx` (its items are deleted along with it). */
function textBefore(tx: Y.Transaction, t: Y.Text): string {
  let s = ''
  for (let it = (t as any)._start as Y.Item | null; it; it = it.right) {
    if (it.content instanceof Y.ContentString && existed(tx, it.id) && (!it.deleted || Y.isDeleted(tx.deleteSet, it.id))) s += it.content.str
  }
  return s
}

/** Values of `map` that this transaction deleted because a concurrent write won. */
export function lostValues(map: Y.Map<any>, keys: Iterable<string>, tx: Y.Transaction): { key: string; lost: Y.Item; won: Y.Item }[] {
  const out: { key: string; lost: Y.Item; won: Y.Item }[] = []
  for (const key of keys) {
    const won = (map as any)._map.get(key) as Y.Item | undefined
    if (!won || won.deleted) continue
    const seen: Y.ID[] = won.origin ? [won.origin] : [] // origins of the items to the right
    let n = 0
    for (let it = won.left; it && n < MAX_WALK; it = it.left, n++) {
      if (it.deleted && Y.isDeleted(tx.deleteSet, it.id) && existed(tx, it.id) && !seen.some((o) => Y.compareIDs(o, it!.id))) {
        out.push({ key, lost: it, won })
      }
      if (it.origin) seen.push(it.origin)
    }
  }
  return out
}

/** Watch for lost files and restore them; `onClash` hears about each one restored. */
export function watchClashes(r: Replica, onClash: (c: Clash) => void) {
  const writer = (id: Y.ID) => r.clients.get(String(id.client)) ?? null
  const restore = (todo: (() => Clash | null)[]) => {
    if (!todo.length || !r.keys.writable) return
    // after this transaction: its observers are still running
    setTimeout(() => {
      const done: Clash[] = []
      r.doc.transact(() => { for (const f of todo) { const c = f(); if (c) done.push(c) } })
      done.forEach(onClash)
    })
  }
  r.files.observe((ev, tx) => {
    if (!r.isRemote(tx.origin)) return
    restore(lostValues(r.files, ev.keysChanged, tx).flatMap(({ key, lost, won }) => {
      const [was] = lost.content.getContent(), [now] = won.content.getContent()
      if (!(was instanceof Y.Text)) return []
      const text = textBefore(tx, was)
      if (now instanceof Y.Text && now.toString() === text) return [] // same content: nothing lost
      return [() => {
        const copy = copyName(key, (p) => exists(r, p))
        r.files.set(copy, new Y.Text(text))
        return { path: key, copy, by: writer(won.id) }
      }]
    }))
  })
  r.bin.observe((ev, tx) => {
    if (!r.isRemote(tx.origin)) return
    restore(lostValues(r.bin, ev.keysChanged, tx).flatMap(({ key, lost, won }) => {
      const [was] = lost.content.getContent(), [now] = won.content.getContent()
      if (typeof was?.h !== 'string' || was.h === now?.h) return []
      return [() => {
        const copy = copyName(key, (p) => exists(r, p))
        r.bin.set(copy, { h: was.h, size: was.size })
        return { path: key, copy, by: writer(won.id) }
      }]
    }))
  })
}
