// Keeping a project in step with a git repository from the browser: on GitHub, GitLab, or Gitea or
// Forgejo (Codeberg), each through its own API (forges.ts). The file is named for GitHub, the
// first. Two levels, as for Zotero (zotero.ts):
// - the account (a token, one per host) is yours alone: kept in this browser, and with a project
//   list it travels with the list, encrypted. It never enters a project.
// - the link ("this project ⇄ host, owner/repo, branch, folder") is the project's, in its shared
//   settings under `github`, with the commit last brought in or sent (`head`): the base of the
//   next three-way merge, for whoever syncs next.
//
// Bringing commits in: each file the repository changed since `head` is merged three ways (base:
// the file at `head`), so collaborators' edits survive. If any file clashes, nothing comes in and
// the commit is noted as `blocked` until the repository moves on, or someone chooses to keep the
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
import { accountStore } from './accounts'
import { forge, GitError, hostOf, kindOf, MOVED, type Account, type Change, type Entry, type Forge, type Kind, type Tree } from './forges'
import { quiet, STORAGE } from './quiet'
import { shaCache as gitShaCache, type ShaCache } from './gitshacache'

const MAX = 50 * 1024 * 1024 // files larger than this aren't synced (as everywhere in TeXLoom)
export const RENEW = 30_000, EXPIRE = 100_000 // ms
const PULL_EVERY = 60_000, QUIET = 120_000, TICK = 5000 // ms

export type { Account } from './forges'
export { GitError } from './forges'

/** The project's link, in its shared settings under `github` (the name from when GitHub was the only
 *  host): `kind` and `host` say which (none: GitHub). */
export interface Link {
  kind?: Kind; host?: string
  repo: string; branch: string; dir: string // dir: the folder in the repository that holds the project ('' for all of it)
  head: string | null // the commit last brought in or sent
  by: string; at: number // who synced last, and when
  blocked?: string | null // a commit whose changes clash with the project's (not brought in)
  clash?: string[] // the files that clash
}
export interface Lease { id: string; by: string; n: number }

/** This browser's GitHub account (kept apart from the others, as it was before there were others), and when it was set (so the latest follows the project list). */
export const githubAccount = accountStore<Account>('texloom.github', (a) => typeof a.token === 'string')
/** Its accounts with other hosts (GitLab, Gitea, Forgejo), one per host, likewise. */
export const otherAccounts = accountStore<{ list: Account[] }>('texloom.gitAccounts', (a) => Array.isArray(a.list))

const sameHost = (a: { kind?: Kind; host?: string }, b: { kind?: Kind; host?: string }) =>
  kindOf(a) === kindOf(b) && hostOf(a).toLowerCase() === hostOf(b).toLowerCase()

/** Every account this browser has: GitHub's first. */
export function accounts(): Account[] {
  const gh = githubAccount.get()
  return [...(gh ? [{ ...gh, kind: 'github' as const }] : []), ...(otherAccounts.get()?.list ?? []).filter((a) => typeof a?.token === 'string' && kindOf(a) !== 'github')]
}

/** This browser's account for a host (a link's), if it has one. */
export function accountFor(where: { kind?: Kind; host?: string } | null): Account | null {
  return where ? accounts().find((a) => sameHost(a, where)) ?? null : null
}

/** Keep an account (null: forget the one for that host). */
export function setAccount(where: { kind?: Kind; host?: string }, a: Account | null) {
  if (kindOf(where) === 'github') { githubAccount.set(a && { token: a.token, login: a.login }); return }
  const list = (otherAccounts.get()?.list ?? []).filter((x) => !sameHost(x, where))
  otherAccounts.set({ list: a ? [...list, { ...a, kind: kindOf(where), host: hostOf(where) }] : list })
}

/** Check a token and learn whose it is. */
export async function connect(kind: Kind, host: string, token: string): Promise<Account> {
  const tok = token.trim()
  const where = { kind, host: kind === 'github' ? undefined : host }
  if (!/^[A-Za-z0-9_\-.]{8,255}$/.test(tok)) throw new GitError(t('That doesn’t look like a token.'))
  const login = await forge({ token: tok, login: '', ...where }).login()
  return { token: tok, login, ...where }
}

const trees = new Map<string, Tree>() // commit -> its files (commits don't change)

/** A commit's tree and its files (path -> blob), every folder down. */
async function filesAt(f: Forge, l: Link, commit: string): Promise<Tree> {
  const key = `${hostOf(l)} ${l.repo}@${commit}`
  const known = trees.get(key)
  if (known) return known
  const out = await f.filesAt(l.repo, commit)
  if (trees.size > 20) trees.delete(trees.keys().next().value!)
  trees.set(key, out)
  return out
}

/** The id git gives this content: SHA-1 of "blob <size>\0<content>". */
export async function gitSha(d: Uint8Array): Promise<string> {
  const head = new TextEncoder().encode(`blob ${d.length}\0`)
  const all = new Uint8Array(head.length + d.length); all.set(head); all.set(d, head.length)
  return [...new Uint8Array(await crypto.subtle.digest('SHA-1', all))].map((b) => b.toString(16).padStart(2, '0')).join('')
}

// ---- the project's side ----------------------------------------------------------------

export function linkOf(r: Replica): Link | null {
  const v = r.meta.get('github') as Link | undefined
  if (!v || typeof v !== 'object' || typeof v.repo !== 'string' || typeof v.branch !== 'string') return null
  const kind = v.kind === 'gitlab' || v.kind === 'gitea' ? v.kind : undefined
  if (kind && typeof v.host !== 'string') return null
  return { ...v, kind, host: kind ? v.host : undefined, dir: typeof v.dir === 'string' ? v.dir : '' }
}
export function setLink(r: Replica, l: Link | null) { if (l) r.meta.set('github', l); else r.meta.delete('github') }

// The link is in the project's shared settings, which any collaborator can write, and syncing uses
// this browser's own token: a collaborator could point it at a private repository of yours (bringing
// it into the project) or at any repository the token can write to. So a browser syncs only a link
// its user made or approved here: the repository, branch and folder approved, by project.
type Target = Pick<Link, 'kind' | 'host' | 'repo' | 'branch' | 'dir'>
const approvedKey = (r: Replica) => `texloom.githubApproved.${r.keys.project}`

/** What this browser's user approved syncing for this project, if anything. */
export function approvedTarget(r: Replica): Target | null {
  try {
    const v = JSON.parse(localStorage.getItem(approvedKey(r)) || 'null')
    return v && typeof v.repo === 'string' && typeof v.branch === 'string' && typeof v.dir === 'string' ? { ...v, kind: v.kind || undefined, host: v.host || undefined } : null
  } catch { return null }
}

/** Approve syncing this repository, branch and folder from this browser (null: none). */
export function approve(r: Replica, l: Target | null) {
  try {
    if (l) localStorage.setItem(approvedKey(r), JSON.stringify({ kind: kindOf(l), host: hostOf(l), repo: l.repo, branch: l.branch, dir: l.dir ?? '' }))
    else localStorage.removeItem(approvedKey(r))
  } catch (e) { quiet(STORAGE, e) }
}

/** Whether this browser's user approved the project's link (as it is now). */
export function approved(r: Replica, l: Link | null = linkOf(r)): boolean {
  const ok = approvedTarget(r)
  return !!l && !!ok && kindOf(ok) === kindOf(l) && hostOf(ok) === hostOf(l) && ok.repo === l.repo && ok.branch === l.branch && ok.dir === l.dir
}
const notApproved = () => new GitError(t('This browser hasn’t approved syncing the project’s repository link.'))
const notLinked = () => new GitError(t('This project isn’t linked to a repository.'))

const inDir = (l: Link, path: string) => (l.dir ? (path.startsWith(l.dir + '/') ? path.slice(l.dir.length + 1) : null) : path)
const toRepo = (l: Link, p: string) => (l.dir ? `${l.dir}/${p}` : p)

/** A repository file the project keeps: inside the folder, a name TeXLoom accepts, not build output, not too big
 *  (as far as the host says: GitLab doesn't, so its files are checked once fetched). */
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

/** Git ids of stored files, by TeXLoom id (gitshacache.ts): only the files the project has now. */
function shaCache(r: Replica): ShaCache {
  const now: string[] = []
  for (const b of r.bin.values()) if (b && typeof b.h === 'string') now.push(b.h)
  return gitShaCache(r.keys.project, now)
}

async function shaOfMine(r: Replica, f: Mine, cache: ShaCache): Promise<string> {
  if ('text' in f) return gitSha(new TextEncoder().encode(f.text))
  const known = cache.get(f.h)
  if (known) return known
  const sha = await gitSha(await r.getBlob(f.h))
  cache.set(f.h, sha)
  return sha
}

// ---- bringing commits in --------------------------------------------------------------

export interface PullResult { commit: string | null; files: string[]; clash: string[] }

/** Bring the repository's new commits into the project. With `oursWins`, where both changed the same
 *  thing the project's version stays (and goes up with the next send) instead of nothing coming in. */
export async function pull(r: Replica, a: Account, by: string, oursWins = false, still: () => boolean = () => true): Promise<PullResult> {
  const link = linkOf(r)
  if (!link) throw notLinked()
  if (!approved(r, link)) throw notApproved()
  const f = forge(a)
  const c = await f.headOf(link.repo, link.branch)
  if (!c || c === link.head) return { commit: c, files: [], clash: [] }
  if (c === link.blocked && !oursWins) return { commit: c, files: [], clash: link.clash ?? [] }
  const none: Tree = { tree: '', files: new Map<string, Entry>() }
  const theirs = await filesAt(f, link, c)
  const base = link.head ? await filesAt(f, link, link.head).catch((e) => {
    if (e instanceof GitError && e.status === 404) return none // history rewritten: no base
    throw e
  }) : none
  const cache = shaCache(r)
  const mine = projectFiles(r, link)

  // 1. decide each file the repository changed since `head`: take its version, merge, or clash
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
    if (sha === (b?.sha ?? null)) { // unchanged here
      const take = th ? await f.blob(link.repo, th.sha) : null
      if (take && take.length > MAX) continue
      steps.push({ rel, path, was: m, take }); continue
    }
    if (m && 'text' in m && b && th) {
      const bt = isText(rel, await f.blob(link.repo, b.sha)), tt = isText(rel, await f.blob(link.repo, th.sha))
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
 *  GitError with status MOVED if the branch moved meanwhile (bring that in first). `onTop` false:
 *  someone pushed just as it went up, so it landed on top of theirs; `head` stays, and bringing in
 *  next sorts that out. */
export async function push(r: Replica, a: Account, by: string, still: () => boolean = () => true): Promise<{ commit: string; files: string[]; onTop: boolean } | null> {
  const link = linkOf(r)
  if (!link) throw notLinked()
  if (!approved(r, link)) throw notApproved()
  const f = forge(a)
  const base: Tree = link.head ? await filesAt(f, link, link.head) : { tree: '', files: new Map<string, Entry>() }
  const cache = shaCache(r)
  const mine = projectFiles(r, link)
  const changes: Change[] = []
  const names: string[] = []
  for (const [path, m] of mine) {
    const sha = await shaOfMine(r, m, cache)
    const was = base.files.get(path)
    if (was?.sha === sha) continue
    changes.push({ path, was, data: 'text' in m ? new TextEncoder().encode(m.text) : await r.getBlob(m.h) })
    names.push(inDir(link, path)!)
  }
  for (const [path, e] of base.files) {
    if (mine.has(path) || !kept(link, path, e.size)) continue
    changes.push({ path, was: e, data: null })
    names.push(inDir(link, path)!)
  }
  if (!changes.length || !still()) return null
  const shown = names.sort().slice(0, 3).join(', ') + (names.length > 3 ? ` (+${names.length - 3})` : '')
  const c = await f.commit(link.repo, link.branch, { ...base, sha: link.head }, changes, `TeXLoom: ${shown}\n\nSent from the TeXLoom project by ${by}.`)
  if (c.onTop && linkOf(r)?.head === link.head) setLink(r, { ...linkOf(r)!, head: c.sha, blocked: null, clash: [], by, at: Date.now() })
  return { commit: c.sha, files: names, onTop: c.onTop }
}

/** Link the project to a repository. If the branch has commits, the two are combined now:
 *  files only on one side end up on both, and where both have a file but it differs, `prefer`
 *  says whose stays ('project': it goes up with the first send). A branch that doesn't exist yet
 *  starts from the repository's main branch. Returns how many files came in. */
export async function linkRepo(r: Replica, a: Account, by: string, repo: string, branch: string, dir: string, prefer: 'repo' | 'project'): Promise<number> {
  const where = kindOf(a) === 'github' ? {} : { kind: kindOf(a), host: hostOf(a) }
  const l: Link = { ...where, repo, branch, dir, head: null, by, at: Date.now(), blocked: null, clash: [] }
  approve(r, l) // linking it is approving it
  const f = forge(a)
  let c = await f.headOf(repo, branch)
  if (!c) {
    const info = await f.repoInfo(repo)
    const from = info.empty ? null : await f.headOf(repo, info.defaultBranch)
    if (from) { await f.branchFrom(repo, branch, from); c = from }
  }
  if (!c) { setLink(r, l); return 0 } // an empty repository: the project goes up on the first send
  const theirs = await filesAt(f, l, c)
  const cache = shaCache(r), mine = projectFiles(r, l)
  const take: [string, Uint8Array][] = []
  for (const [path, e] of theirs.files) {
    const rel = kept(l, path, e.size)
    if (!rel) continue
    const m = mine.get(path)
    if (m && (prefer === 'project' || (await shaOfMine(r, m, cache)) === e.sha)) continue
    const data = await f.blob(repo, e.sha)
    if (data.length <= MAX) take.push([rel, data])
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
  let renewed = 0, claimed = 0, changedAt = 0, dirty = true, nextPull = 0, moved = 0
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
    const link = linkOf(r), a = accountFor(link)
    // a link this browser's user didn't make or approve isn't synced from here (see approved())
    if (!a || !link || !approved(r, link) || !r.keys.writable || r.deletedAt) { if (mine()) r.meta.delete('githubLease'); set({ role: 'off', holder: null }); return }
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
    if (!force && Date.now() < nextPull && !dirty) return // nothing to do this time (and the screen needn't redraw)
    set({ busy: true }) // (through set, so the screen hears it: and hears it end)
    try {
      if (force || Date.now() >= nextPull || dirty) {
        const p = await pull(r, a, me(), false, mine)
        nextPull = Date.now() + PULL_EVERY
        state.lastPull = Date.now()
        if (p.files.length) dirty = true
        if (p.clash.length) dirty = false // a clash waits for someone: look again when something changes, not every few seconds
      }
      const cur = linkOf(r)
      if (cur && !cur.blocked && (force || (dirty && Date.now() - changedAt >= QUIET))) {
        dirty = false
        try {
          const sent = await push(r, a, me(), mine)
          if (sent) state.lastPush = Date.now()
          if (sent && !sent.onTop) { dirty = true; nextPull = 0 } // landed on top of someone's push: bring theirs in
          moved = 0
        } catch (e) {
          // the branch moved: bring that in first (unless that keeps happening: then say so)
          if (!(e instanceof GitError) || e.status !== MOVED || ++moved > 3) { moved = 0; throw e }
          dirty = true; nextPull = 0
        }
      }
      set({ error: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) })
    } finally { set({ busy: false }) }
  }
  const timer = setInterval(() => { round().catch((e) => console.error('git sync:', e)) }, TICK)
  const release = () => { if (mine()) r.meta.delete('githubLease') }
  window.addEventListener('pagehide', release)
  round().catch((e) => console.error('git sync:', e))
  return {
    state,
    /** Bring in and send now (Sync now), taking the lease if it's free. */
    now: () => round(true),
    stop() { clearInterval(timer); window.removeEventListener('pagehide', release); release() },
  }
}
