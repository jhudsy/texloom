// Line endings in project texts: "\n" only. Files saved on Windows end lines with "\r\n"; the
// folder syncs (foldersync.ts, texloom/folder.py) write each file back with the endings it had.
// No DOM here, so scripts/check-props.mjs tests exactly this.
import * as Y from 'yjs'
import type { Replica } from './replica'

/** Text with every line ending as "\n". Files saved on Windows end lines with "\r\n": the project
 *  keeps "\n" only (the editors and merges count a line break as one character), and the desktop
 *  app and the browser's folder sync write a file back with the line endings it had. */
export const lf = (s: string) => (s.includes('\r') ? s.replace(/\r\n/g, '\n').replace(/\r/g, '') : s)

const LF = Symbol('texloom-lf') // the strips' own changes

/** Keep "\r" out of the project's text: take out any already there, any a collaborator's older app
 *  brings in, and any pasted. Each "\r" is deleted by itself, so nothing else (cursors, comments) moves. */
export function keepLF(r: Replica) {
  if (!r.keys.writable) return
  const strip = (texts: Iterable<Y.Text>) => {
    const todo = [...texts].filter((yt) => yt.toString().includes('\r'))
    if (!todo.length || r.deletedAt) return
    r.doc.transact(() => {
      for (const yt of todo) {
        const s = yt.toString()
        for (let i = s.length - 1; i >= 0; i--) {
          if (s[i] !== '\r') continue
          yt.delete(i, 1) // a lone "\r" too: left in mid-word by edits misplaced before 0.4.21 (Windows editors show it as a line break)
        }
      }
    }, LF)
  }
  strip(r.files.values())
  r.files.observeDeep((events, tx) => {
    if (tx.origin === LF || !(r.isRemote(tx.origin) || r.isLocal(tx.origin))) return
    const changed = new Set<Y.Text>()
    for (const ev of events) if (ev.target instanceof Y.Text) changed.add(ev.target); else for (const k of (ev as Y.YMapEvent<Y.Text>).keysChanged ?? []) { const t = r.files.get(k); if (t) changed.add(t) }
    if (changed.size) setTimeout(() => strip(changed)) // after this change: its observers are still running
  })
}
