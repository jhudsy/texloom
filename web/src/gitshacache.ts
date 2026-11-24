// Git's ids (github.ts), or Dropbox's content hashes (dropbox.ts), of a project's stored (binary) files,
// by TeXLoom's id, kept in browser storage so that syncing doesn't download and hash every image each minute. Both ids
// name the content, so an entry never goes stale; but it would outlive its file, and a project whose
// images change often would fill the browser's storage with ids of old versions (issue 29). So each
// time a sync opens it, it keeps only the files the project has now: those are all a sync looks up.
// No imports that need a page: scripts/check-gitshacache.mjs runs it in node.
import { quiet, STORAGE } from './quiet'

export interface ShaCache {
  get(h: string): string | undefined
  set(h: string, sha: string): void
}

export const cacheKey = (project: string) => `texloom.gitsha.${project}`

/** The cache for `project`, trimmed to `current`: the TeXLoom ids of the files it has now. `key`: where
 *  it's kept (Dropbox's hashes are kept apart from git's). */
export function shaCache(project: string, current: Iterable<string>, key = cacheKey(project)): ShaCache {
  let m: Record<string, string> = {}
  try {
    const v = JSON.parse(localStorage.getItem(key) || '{}')
    if (v && typeof v === 'object' && !Array.isArray(v)) m = v
  } catch (e) { quiet(STORAGE, e) }
  const save = () => {
    try {
      if (Object.keys(m).length) localStorage.setItem(key, JSON.stringify(m))
      else localStorage.removeItem(key)
    } catch (e) { quiet(STORAGE, e) }
  }
  const keep = new Set(current)
  const before = Object.keys(m).length
  for (const h of Object.keys(m)) if (!keep.has(h) || typeof m[h] !== 'string') delete m[h]
  if (Object.keys(m).length !== before) save()
  return {
    get: (h) => m[h],
    set: (h, sha) => { if (m[h] === sha) return; m[h] = sha; save() },
  }
}
