// Browser storage (localStorage, sessionStorage), guarded: blocked (private browsing, site settings)
// or full, a read gives nothing and a write is skipped, through quiet(), never an error. No DOM,
// so the node checks can bundle the modules that use it.
import { quiet, STORAGE } from './quiet'

function kept(area: () => Storage) {
  /** The text kept under `key`, or null (none, or storage blocked). */
  const get = (key: string): string | null => {
    try { return area().getItem(key) } catch (e) { quiet(STORAGE, e); return null }
  }
  /** Keep `value` under `key` (null: remove it). False if storage refused it. */
  const set = (key: string, value: string | null): boolean => {
    try {
      if (value === null) area().removeItem(key); else area().setItem(key, value)
      return true
    } catch (e) { quiet(STORAGE, e); return false }
  }
  return {
    get, set,
    /** The text kept under `key`, removed as it's read (a note left for the next page). */
    take(key: string): string | null {
      const v = get(key)
      if (v !== null) set(key, null)
      return v
    },
    /** What's kept under `key` as JSON, or `fallback` if nothing is (or it isn't JSON). */
    json<T>(key: string, fallback: T): T {
      const s = get(key)
      if (!s) return fallback
      try { return JSON.parse(s) } catch (e) { quiet('a stored value isn’t JSON: the default is used', e); return fallback }
    },
    /** Keep `value` under `key` as JSON (null: remove it). */
    setJson: (key: string, value: unknown): boolean => set(key, value === null ? null : JSON.stringify(value)),
    /** Every key kept (none if storage is blocked). */
    keys(): string[] {
      try { return Object.keys(area()) } catch (e) { quiet(STORAGE, e); return [] }
    },
  }
}

/** localStorage: kept in this browser. */
export const local = kept(() => localStorage)
/** sessionStorage: kept for this tab. */
export const session = kept(() => sessionStorage)

export const getString = local.get, setString = local.set, readJson = local.json, writeJson = local.setJson, takeString = local.take
