import { quiet, STORAGE } from './quiet'
// An account with an outside service (a Zotero key, a GitHub token): yours alone, kept in this
// browser with when it was set, so that the latest follows the project list (listsync.ts).

export interface AccountStore<A> {
  get(): A | null
  at(): number
  set(a: A | null, at?: number): void
}

/** The account kept under `key` (and `key`At for when); `valid` tells a stored one apart from junk. */
export function accountStore<A>(key: string, valid: (a: any) => boolean): AccountStore<A> {
  const atKey = `${key}At`
  return {
    get() {
      try { const a = JSON.parse(localStorage.getItem(key) || 'null'); return a && valid(a) ? a : null } catch { return null }
    },
    at() { try { return Number(localStorage.getItem(atKey)) || 0 } catch { return 0 } },
    set(a, at = Date.now()) {
      try {
        if (a) localStorage.setItem(key, JSON.stringify(a)); else localStorage.removeItem(key)
        localStorage.setItem(atKey, String(at))
      } catch (e) { quiet(STORAGE, e) }
    },
  }
}
