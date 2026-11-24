import { textStore } from './accounts'
// The name shown to collaborators. It follows the project list to this person's other browsers
// (listsync.ts), so it carries when it was set: the latest one wins.
export const NAME_CHANGED = 'texloom-name-changed'

const kept = textStore('texloom.name')
export const myName = {
  get: kept.get,
  /** When it was set here (0: before this was recorded). */
  at: kept.at,
  set(v: string, at = Date.now(), quietly = false) {
    kept.set(v, at)
    if (!quietly) window.dispatchEvent(new Event(NAME_CHANGED))
  },
}
