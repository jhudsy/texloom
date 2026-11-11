// GitHub: keep the project in step with a GitHub repository, from the browser (api.github.com
// allows that). Two levels, as for Zotero (zotero.ts):
// - the account (a token) is yours alone: kept in this browser, and with a project list it
//   travels with the list, encrypted. It never enters a project.
// - the link ("this project ⇄ owner/repo, branch, folder") is the project's, in its shared
//   settings under `github`, with the commit last brought in or sent (`head`): the base of the
//   next three-way merge, for whoever syncs next.
//
// Bringing commits in: each file GitHub changed since `head` is merged three ways (base: the
// file at `head`), so collaborators' edits survive. If any file clashes, nothing comes in and
// the commit is noted as `blocked` until GitHub moves on, or someone chooses to keep the
// project's version where they clash. Sending out: once the project has been quiet for a while,
// the files that differ from `head` go up as one commit.
//
// Only one copy syncs at a time, or the same commit could be merged in twice at once (two
// identical insertions both kept). The project holds a lease (`githubLease`): whoever holds it
// renews it every RENEW; another copy takes it over only when it has seen no renewal for
// EXPIRE by its own clock (so clocks needn't agree). The desktop app's git bridge
// (texloom/gitsync.py) takes part when its folder is a clone of the linked repository, so a
// folder bridged to the same repository and a browser never both bring the same commit in.
import * as Y from 'yjs'
import type { Replica } from './replica'
import { applyText, merge3, merge3Strict } from './diff'
import { BUILD } from './foldersync'
import { cleanPath, isText } from './projectfiles'
import { t } from './i18n'

const STORE = 'texloom.github', STORE_AT = 'texloom.githubAt'
const MAX = 50 * 1024 * 1024 // files larger than this aren't synced (as everywhere in TeXLoom)
export const RENEW = 30_000, EXPIRE = 100_000 // ms
const PULL_EVERY = 60_000, QUIET = 120_000, TICK = 5000 // ms

/** api.github.com, or a stand-in when testing on this computer (set texloom.githubApi in localStorage). */
function api(): string {
  try {
    const o = localStorage.getItem('texloom.githubApi')
    if (o && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) return o.replace(/\/+$/, '')
  } catch {}
  return 'https://api.github.com'
}

export interface Account { token: string; login: string }
/** The project's link, in its shared settings under `github`. */
export interface Link {
  repo: string; branch: string; dir: string // dir: the folder in the repository that holds the project ('' for all of it)
  head: string | null // the commit last brought in or sent
  by: string; at: number // who synced last, and when
  blocked?: string | null // a commit whose changes clash with the project's (not brought in)
  clash?: string[] // the files that clash
}
export interface Lease { id: string; by: string; n: number }

/** This browser's GitHub account, and when it was set (so the latest follows the project list). */
export const githubAccount = {
  get(): Account | null {
    try { const a = JSON.parse(localStorage.getItem(STORE) || 'null'); return a && typeof a.token === 'string' ? a : null } catch { return null }
  },
  at(): number { try { return Number(localStorage.getItem(STORE_AT)) || 0 } catch { return 0 } },
  set(a: Account | null, at = Date.now()) {
    try {
      if (a) localStorage.setItem(STORE, JSON.stringify(a)); else localStorage.removeItem(STORE)
      localStorage.setItem(STORE_AT, String(at))
    } catch {}
  },
}

export class GitHubError extends Error { constructor(msg: string, readonly status = 0) { super(msg) } }

async function call(a: Account, method: string, path: string, body?: unknown): Promise<any> {
  let res: Response
  try {
    res = await fetch(api() + path, {
      method, headers: { Authorization: `Bearer ${a.token}`, Accept: 'application/vnd.github+json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined, cache: 'no-store',
    })
  } catch { throw new GitHubError(t('Couldn’t reach GitHub (offline?).')) }
  if (res.ok) return res.status === 204 ? null : res.json()
  const why = await res.json().then((j) => String(j?.message ?? ''), () => '')
  if (res.status === 401) throw new GitHubError(t('GitHub refused the token: it may have expired. Connect again with a new one.'), 401)
  if (res.status === 403 && /rate limit/i.test(why)) throw new GitHubError(t('GitHub asks to slow down: trying again later.'), 403)
  if (res.status === 403) throw new GitHubError(t('The token doesn’t allow this: it needs read and write access to the repository’s contents.'), 403)
  if (res.status === 404) throw new GitHubError(t('GitHub doesn’t have that repository, or the token can’t see it.'), 404)
  throw new GitHubError(t('GitHub answered {{status}}{{why}}.', { status: res.status, why: why ? `: ${why}` : '' }), res.status)
}

/** Check a token and learn whose it is. */
export async function connect(token: string): Promise<Account> {
  const tok = token.trim()
  if (!/^[A-Za-z0-9_\-.]{20,255}$/.test(tok)) throw new GitHubError(t('That doesn’t look like a GitHub token.'))
  const me = await call({ token: tok, login: '' }, 'GET', '/user')
  return { token: tok, login: String(me?.login ?? '') }
}

/** owner/name from "owner/name", or a github.com address. */
export function repoName(s: string): string | null {
  const m = s.trim().match(/^(?:https?:\/\/github\.com\/|git@github\.com:)?([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/)
  return m ? `${m[1]}/${m[2]}` : null
}

const enc = (s: string) => s.split('/').map(encodeURIComponent).join('/')

export async function repoInfo(a: Account, repo: string): Promise<{ defaultBranch: string; canPush: boolean }> {
  const j = await call(a, 'GET', `/repos/${enc(repo)}`)
  return { defaultBranch: String(j.default_branch ?? 'main'), canPush: !!j.permissions?.push }
}

/** The branch's latest commit; null if there is no such branch (or the repository is empty). */
export async function headOf(a: Account, repo: string, branch: string): Promise<string | null> {
  try { return String((await call(a, 'GET', `/repos/${enc(repo)}/git/ref/heads/${enc(branch)}`)).object.sha) }
  catch (e) { if (e instanceof GitHubError && (e.status === 404 || e.status === 409)) return null; throw e }
}

interface Entry { sha: string; mode: string; size: number }
const trees = new Map<string, { tree: string; files: Map<string, Entry> }>() // commit -> its files (commits don't change)

/** A commit's tree and its files (path -> blob), every folder down. */
export async function filesAt(a: Account, repo: string, commit: string): Promise<{ tree: string; files: Map<string, Entry> }> {
  const key = `${repo}@${commit}`
  const known = trees.get(key)
  if (known) return known
  const c = await call(a, 'GET', `/repos/${enc(repo)}/git/commits/${commit}`)
  const tr = await call(a, 'GET', `/repos/${enc(repo)}/git/trees/${c.tree.sha}?recursive=1`)
  if (tr.truncated) throw new GitHubError(t('The repository has too many files for TeXLoom to keep in step.'))
  const files = new Map<string, Entry>()
  for (const e of tr.tree ?? []) if (e.type === 'blob' && (e.mode === '100644' || e.mode === '100755')) files.set(e.path, { sha: e.sha, mode: e.mode, size: e.size ?? 0 })
  const out = { tree: String(c.tree.sha), files }
  if (trees.size > 20) trees.delete(trees.keys().next().value!)
  trees.set(key, out)
  return out
}

async function blob(a: Account, repo: string, sha: string): Promise<Uint8Array> {
  const j = await call(a, 'GET', `/repos/${enc(repo)}/git/blobs/${sha}`)
  return Uint8Array.from(atob(String(j.content).replace(/\s/g, '')), (c) => c.charCodeAt(0))
}

const b64 = (d: Uint8Array) => { let s = ''; for (let i = 0; i < d.length; i += 0x8000) s += String.fromCharCode(...d.subarray(i, i + 0x8000)); return btoa(s) }

/** The id git gives this content: SHA-1 of "blob <size>\0<content>". */
export async function gitSha(d: Uint8Array): Promise<string> {
  const head = new TextEncoder().encode(`blob ${d.length}\0`)
  const all = new Uint8Array(head.length + d.length); all.set(head); all.set(d, head.length)
  return [...new Uint8Array(await crypto.subtle.digest('SHA-1', all))].map((b) => b.toString(16).padStart(2, '0')).join('')
}

// ---- the project's side ----------------------------------------------------------------

export function linkOf(r: Replica): Link | null {
  const v = r.meta.get('github') as Link | undefined
  return v && typeof v === 'object' && typeof v.repo === 'string' && typeof v.branch === 'string' ? { ...v, dir: v.dir ?? '' } : null
}
export function setLink(r: Replica, l: Link | null) { if (l) r.meta.set('github', l); else r.meta.delete('github') }

const inDir = (l: Link, path: string) => (l.dir ? (path.startsWith(l.dir + '/') ? path.slice(l.dir.length + 1) : null) : path)
const toRepo = (l: Link, p: string) => (l.dir ? `${l.dir}/${p}` : p)

/** A repository file the project keeps: inside the folder, a name TeXLoom accepts, not build output, not too big. */
function kept(l: Link, path: string, size: number): string | null {
  const p = inDir(l, path)
  return p && cleanPath(p) === p && !BUILD.test(p) && size <= MAX ? p : null
}

type Mine = { text: string } | { h: string; size: number }

/** The project's files, by repository path. */
function projectFiles(r: Replica, l: Link): Map<string, Mine> {
  const out = new Map<string, Mine>()
  r.files.forEach((yt, p) => { if (cleanPath(p) === p && !BUILD.test(p)) out.set(toRepo(l, p), { text: yt.toString() }) })
  for (const [p, b] of r.bin.entries()) if (cleanPath(p) === p && !BUILD.test(p) && b && typeof b.h === 'string' && (b.size ?? 0) <= MAX) out.set(toRepo(l, p), { h: b.h, size: b.size })
  return out
}

/** Git ids of stored files, by TeXLoom id (both name the content, so this never goes stale). */
function shaCache(r: Replica) {
  const key = `texloom.gitsha.${r.keys.project}`
  let m: Record<string, string> = {}
  try { m = JSON.parse(localStorage.getItem(key) || '{}') } catch {}
  return {
    get: (h: string) => m[h],
    set: (h: string, sha: string) => { m[h] = sha; try { localStorage.setItem(key, JSON.stringify(m)) } catch {} },
  }
}

async function shaOfMine(r: Replica, f: Mine, cache: ReturnType<typeof shaCache>): Promise<string> {
  if ('text' in f) return gitSha(new TextEncoder().encode(f.text))
  const known = cache.get(f.h)
  if (known) return known
  const sha = await gitSha(await r.getBlob(f.h))
  cache.set(f.h, sha)
  return sha
}

// ---- bringing commits in --------------------------------------------------------------

export interface PullResult { commit: string | null; files: string[]; clash: string[] }

/** Bring GitHub's new commits into the project. With `oursWins`, where both changed the same
 *  thing the project's version stays (and goes up with the next send) instead of nothing coming in. */
export async function pull(r: Replica, a: Account, by: string, oursWins = false, still: () => boolean = () => true): Promise<PullResult> {
  const link = linkOf(r)
  if (!link) throw new GitHubError(t('This project isn’t linked to GitHub.'))
  const c = await headOf(a, link.repo, link.branch)
  if (!c || c === link.head) return { commit: c, files: [], clash: [] }
  if (c === link.blocked && !oursWins) return { commit: c, files: [], clash: link.clash ?? [] }
  const none = { tree: '', files: new Map<string, Entry>() }
  const theirs = await filesAt(a, link.repo, c)
  const base = link.head ? await filesAt(a, link.repo, link.head).catch((e) => {
    if (e instanceof GitHubError && e.status === 404) return none // history rewritten: no base
    throw e
  }) : none
  const cache = shaCache(r)
  const mine = projectFiles(r, link)

  // 1. decide each file GitHub changed since `head`: take GitHub's, merge, or clash
  type Step = { rel: string; path: string; was: Mine | null; take?: Uint8Array | null; merge?: { base: string; theirs: string } }
  const steps: Step[] = [], clash: string[] = []
  for (const path of new Set([...base.files.keys(), ...theirs.files.keys()])) {
    const b = base.files.get(path), th = theirs.files.get(path)
    if (b?.sha === th?.sha) continue
    const rel = kept(link, path, th?.size ?? b?.size ?? 0)
    if (!rel) continue
    const m = mine.get(path) ?? null
    const sha = m ? await shaOfMine(r, m, cache) : null
    if (sha === (th?.sha ?? null)) continue // the project already has it
    if (sha === (b?.sha ?? null)) { steps.push({ rel, path, was: m, take: th ? await blob(a, link.repo, th.sha) : null }); continue } // unchanged here
    if (m && 'text' in m && b && th) {
      const bt = isText(rel, await blob(a, link.repo, b.sha)), tt = isText(rel, await blob(a, link.repo, th.sha))
      if (bt !== null && tt !== null) { steps.push({ rel, path, was: m, merge: { base: bt, theirs: tt } }); continue }
    }
    clash.push(rel) // changed on both sides (or deleted on one), and not text that merges
  }
  if (!still()) return { commit: c, files: [], clash: [] }
  for (const st of steps) { // text merges that clash
    if (!st.merge || oursWins) continue
    if (merge3Strict(st.merge.base, st.merge.theirs, (st.was as { text: string }).text) === null) clash.push(st.rel)
  }
  if (clash.length && !oursWins) {
    setLink(r, { ...linkOf(r)!, blocked: c, clash: clash.sort(), by, at: Date.now() })
    return { commit: c, files: [], clash }
  }

  // 2. apply, unless the project changed underneath (then next round decides again)
  const uploads = new Map<string, string>()
  for (const st of steps) if (st.take && isText(st.rel, st.take) === null) uploads.set(st.rel, await r.putBlob(st.take))
  const files: string[] = []
  let moved = false
  r.doc.transact(() => {
    if (linkOf(r)?.head !== link.head) { moved = true; return } // someone else brought it in meanwhile
    const now = (rel: string): Mine | null => {
      const yt = r.files.get(rel), b = r.bin.get(rel)
      return yt ? { text: yt.toString() } : b ? { h: b.h, size: b.size } : null
    }
    const same = (x: Mine | null, y: Mine | null) => (!x || !y ? x === y : 'text' in x ? 'text' in y && x.text === y.text : 'h' in y && x.h === y.h)
    const todo: (() => void)[] = []
    for (const st of steps) {
      const cur = now(st.rel)
      if (st.merge) {
        if (!cur || !('text' in cur)) { moved = true; return }
        const merged = oursWins ? merge3(st.merge.base, st.merge.theirs, cur.text) : merge3Strict(st.merge.base, st.merge.theirs, cur.text)
        if (merged === null) { moved = true; return }
        if (merged !== cur.text) { const yt = r.files.get(st.rel)!; todo.push(() => applyText(yt, cur.text, merged)); files.push(st.rel) }
        continue
      }
      if (!same(cur, st.was)) { moved = true; return }
      const data = st.take
      if (!data) todo.push(() => { r.files.delete(st.rel); r.bin.delete(st.rel) })
      else {
        const text = isText(st.rel, data)
        if (text === null) todo.push(() => { r.files.delete(st.rel); r.bin.set(st.rel, { h: uploads.get(st.rel)!, size: data.length }) })
        else todo.push(() => {
          r.bin.delete(st.rel)
          const yt = r.files.get(st.rel)
          if (yt) applyText(yt, yt.toString(), text); else r.files.set(st.rel, new Y.Text(text))
        })
      }
      files.push(st.rel)
    }
    todo.forEach((f) => f())
    setLink(r, { ...linkOf(r)!, head: c, blocked: null, clash: [], by, at: Date.now() })
  })
  return moved ? { commit: null, files: [], clash: [] } : { commit: c, files, clash: [] }
}

// ---- sending changes out ---------------------------------------------------------------

/** Send the project's changes since `head` as one commit. Null if nothing changed; throws a
 *  GitHubError with status 422 if GitHub moved meanwhile (bring that in first). */
export async function push(r: Replica, a: Account, by: string, still: () => boolean = () => true): Promise<{ commit: string; files: string[] } | null> {
  const link = linkOf(r)
  if (!link) throw new GitHubError(t('This project isn’t linked to GitHub.'))
  const repo = `/repos/${enc(link.repo)}`
  const base = link.head ? await filesAt(a, link.repo, link.head) : { tree: '', files: new Map<string, Entry>() }
  const cache = shaCache(r)
  const mine = projectFiles(r, link)
  const entries: { path: string; mode: string; type: 'blob'; sha: string | null }[] = []
  const names: string[] = []
  for (const [path, f] of mine) {
    const sha = await shaOfMine(r, f, cache)
    const was = base.files.get(path)
    if (was?.sha === sha) continue
    const data = 'text' in f ? new TextEncoder().encode(f.text) : await r.getBlob(f.h)
    entries.push({ path, mode: was?.mode ?? '100644', type: 'blob', sha: (await call(a, 'POST', `${repo}/git/blobs`, { content: b64(data), encoding: 'base64' })).sha })
    names.push(inDir(link, path)!)
  }
  for (const [path, e] of base.files) {
    if (mine.has(path) || !kept(link, path, e.size)) continue
    entries.push({ path, mode: e.mode, type: 'blob', sha: null })
    names.push(inDir(link, path)!)
  }
  if (!entries.length || !still()) return null
  const shown = names.sort().slice(0, 3).join(', ') + (names.length > 3 ? ` (+${names.length - 3})` : '')
  const tree = await call(a, 'POST', `${repo}/git/trees`, { ...(base.tree ? { base_tree: base.tree } : {}), tree: entries })
  const commit = await call(a, 'POST', `${repo}/git/commits`, {
    message: `TeXLoom: ${shown}\n\nSent from the TeXLoom project by ${by}.`, tree: tree.sha, parents: link.head ? [link.head] : [] })
  if (link.head) await call(a, 'PATCH', `${repo}/git/refs/heads/${enc(link.branch)}`, { sha: commit.sha, force: false })
  else await call(a, 'POST', `${repo}/git/refs`, { ref: `refs/heads/${link.branch}`, sha: commit.sha })
  if (linkOf(r)?.head === link.head) setLink(r, { ...linkOf(r)!, head: commit.sha, blocked: null, clash: [], by, at: Date.now() })
  return { commit: commit.sha, files: names }
}

/** An empty repository takes no commits through the git API: its first has to go through the
 *  contents API, one file at a time. This sends the main file (the rest follows with push). */
export async function firstCommit(r: Replica, a: Account, l: Link): Promise<string> {
  const mine = [...projectFiles(r, l)].filter(([, f]) => 'text' in f)
  const [path, f] = mine.find(([p]) => p.endsWith('main.tex')) ?? mine[0] ?? [toRepo(l, 'README.md'), { text: '' }]
  const j = await call(a, 'PUT', `/repos/${enc(l.repo)}/contents/${enc(path)}`, {
    message: 'TeXLoom: first commit', content: b64(new TextEncoder().encode((f as { text: string }).text)), branch: l.branch })
  return String(j.commit.sha)
}

/** Link the project to a repository. If the branch has commits, the two are combined now:
 *  files only on one side end up on both, and where both have a file but it differs, `prefer`
 *  says whose stays ('project': it goes up with the first send). Returns how many files came in. */
export async function linkRepo(r: Replica, a: Account, by: string, repo: string, branch: string, dir: string, prefer: 'repo' | 'project'): Promise<number> {
  const l: Link = { repo, branch, dir, head: null, by, at: Date.now(), blocked: null, clash: [] }
  const c = await headOf(a, repo, branch)
  if (!c) { setLink(r, l); return 0 } // an empty repository or a new branch: the project goes up on the first send
  const theirs = await filesAt(a, repo, c)
  const cache = shaCache(r), mine = projectFiles(r, l)
  const take: [string, Uint8Array][] = []
  for (const [path, e] of theirs.files) {
    const rel = kept(l, path, e.size)
    if (!rel) continue
    const m = mine.get(path)
    if (m && (prefer === 'project' || (await shaOfMine(r, m, cache)) === e.sha)) continue
    take.push([rel, await blob(a, repo, e.sha)])
  }
  const uploads = new Map<string, string>()
  for (const [rel, d] of take) if (isText(rel, d) === null) uploads.set(rel, await r.putBlob(d))
  r.doc.transact(() => {
    for (const [rel, d] of take) {
      const text = isText(rel, d)
      if (text === null) { r.files.delete(rel); r.bin.set(rel, { h: uploads.get(rel)!, size: d.length }); continue }
      r.bin.delete(rel)
      const yt = r.files.get(rel)
      if (yt) applyText(yt, yt.toString(), text); else r.files.set(rel, new Y.Text(text))
    }
    setLink(r, { ...l, head: c })
  })
  return take.length
}

// ---- who syncs: the lease ---------------------------------------------------------------

/** This tab's id for the lease (a reload keeps it, another tab gets its own). */
function tabId(): string {
  try {
    let id = sessionStorage.getItem('texloom.tab')
    if (!id) { id = 'web:' + crypto.randomUUID(); sessionStorage.setItem('texloom.tab', id) }
    return id
  } catch { return 'web:' + Math.random().toString(36).slice(2) }
}

export interface SyncState { role: 'off' | 'syncing' | 'standby' | 'waiting'; holder: string | null; error: string | null; busy: boolean; lastPull: number; lastPush: number }

/** Run the link while the project is open: hold the lease when nobody else does, bring commits
 *  in every minute, send changes after QUIET. `onChange` hears about every state change. */
export function startGitHub(r: Replica, me: () => string, onChange: () => void) {
  const id = tabId()
  const state: SyncState = { role: 'off', holder: null, error: null, busy: false, lastPull: 0, lastPush: 0 }
  let seen = Date.now() // when the lease last changed, by this tab's clock
  // a send is due at the start, when the link changes, and after edits (a send that finds nothing makes no commit)
  let renewed = 0, claimed = 0, changedAt = 0, dirty = true, nextPull = 0
  const lease = () => { const l = r.meta.get('githubLease') as Lease | undefined; return l && typeof l.id === 'string' ? l : null }
  r.meta.observe((ev) => {
    if (ev.keysChanged.has('githubLease')) seen = Date.now()
    if (ev.keysChanged.has('github')) dirty = true
  })
  const touched = (_e: unknown, tx: Y.Transaction) => { if (!tx.origin || r.isLocal(tx.origin) || r.isRemote(tx.origin)) { changedAt = Date.now(); dirty = true } }
  r.files.observeDeep(touched as any); r.bin.observe(touched as any)
  const set = (patch: Partial<SyncState>) => {
    if (Object.entries(patch).every(([k, v]) => (state as any)[k] === v)) return // only real changes
    Object.assign(state, patch); onChange()
  }
  const mine = () => lease()?.id === id

  async function round(force = false) {
    const a = githubAccount.get(), link = linkOf(r)
    if (!a || !link || !r.keys.writable || r.deletedAt) { if (mine()) r.meta.delete('githubLease'); set({ role: 'off', holder: null }); return }
    const l = lease()
    if (!mine()) {
      if (l && Date.now() - seen < EXPIRE) { set({ role: 'standby', holder: l.by }); return } // never taken from a live holder, even on Sync now
      if (r.apart) { set({ role: 'waiting', holder: l?.by ?? null }); return } // don't take it while unable to tell
      r.meta.set('githubLease', { id, by: me(), n: 0 }); claimed = renewed = Date.now()
      set({ role: 'waiting', holder: me() })
      return // act once the claim has had time to meet any other
    }
    if (Date.now() - claimed < 2 * TICK && !force) return
    if (Date.now() - renewed >= RENEW) { r.meta.set('githubLease', { id, by: me(), n: (l?.n ?? 0) + 1 }); renewed = Date.now() }
    if (r.apart) { set({ role: 'waiting', holder: me() }); return }
    set({ role: 'syncing', holder: me() })
    if (state.busy) return
    state.busy = true
    try {
      if (force || Date.now() >= nextPull || dirty) {
        const p = await pull(r, a, me(), false, mine)
        nextPull = Date.now() + PULL_EVERY
        state.lastPull = Date.now()
        if (p.files.length) dirty = true
      }
      const cur = linkOf(r)
      if (cur && !cur.blocked && (force || (dirty && Date.now() - changedAt >= QUIET))) {
        dirty = false
        try { if (await push(r, a, me(), mine)) state.lastPush = Date.now() }
        catch (e) {
          if (!(e instanceof GitHubError)) throw e
          if (e.status === 409 && !cur.head) { // an empty repository: its first commit, then the rest
            setLink(r, { ...cur, head: await firstCommit(r, a, cur) })
            if (await push(r, a, me(), mine)) state.lastPush = Date.now()
          } else if (e.status === 422) { dirty = true; nextPull = 0 } // GitHub moved: bring that in first
          else throw e
        }
      }
      set({ error: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) })
    } finally { state.busy = false; set({ busy: false }) }
  }
  const timer = setInterval(() => { round().catch(() => {}) }, TICK)
  const release = () => { if (mine()) r.meta.delete('githubLease') }
  window.addEventListener('pagehide', release)
  round().catch(() => {})
  return {
    state,
    /** Bring in and send now (Sync now), taking the lease if it's free. */
    now: () => round(true),
    stop() { clearInterval(timer); window.removeEventListener('pagehide', release); release() },
  }
}
