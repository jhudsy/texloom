import { getString, readJson, setString, writeJson } from './store'
// An account with an outside service (a Zotero key, a GitHub token), or a setting such as your
// name or a creation code: yours alone, kept in this browser with when it was set, so that the
// latest follows the project list (listsync.ts).

export interface AccountStore<A> {
  get(): A | null
  at(): number
  set(a: A | null, at?: number): void
}

/** When the value under `key` was set (kept under `key`At; 0: before this was recorded). */
const setAt = (key: string) => Number(getString(`${key}At`)) || 0
const stamp = (key: string, stored: boolean, at: number) => { if (stored) setString(`${key}At`, String(at)) }

/** The account kept under `key` as JSON (and `key`At for when); `valid` tells a stored one apart from junk. */
export function accountStore<A>(key: string, valid: (a: any) => boolean): AccountStore<A> {
  return {
    get() {
      const a = readJson<any>(key, null)
      return a && valid(a) ? a : null
    },
    at: () => setAt(key),
    set(a, at = Date.now()) { stamp(key, writeJson(key, a || null), at) },
  }
}

/** A text kept under `key` as it is ('' if none), with when it was set. */
export function textStore(key: string) {
  return {
    get: (): string => getString(key) || '',
    at: () => setAt(key),
    set(v: string, at = Date.now()) { stamp(key, setString(key, v), at) },
  }
}
