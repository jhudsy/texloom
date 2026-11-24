// The git hosts a project can be kept in step with from the browser (github.ts does the syncing):
// GitHub, GitLab (gitlab.com or your own) and Gitea or Forgejo (Codeberg, gitea.com or your own).
// Each answers the same few questions through its own API: whose token this is, a branch's latest
// commit, a commit's files (git blob ids, which TeXLoom compares with its own files' ids), a file's
// content, and making a commit on top of the one TeXLoom last saw. All three allow these calls from
// a web page (CORS); a self-hosted Gitea or Forgejo only once its admin has turned that on.
//
// Bitbucket isn't here: its API doesn't give files' git ids, so every file would have to be
// downloaded each minute to see what changed. The TeXLoom app's git bridge works with any host.
import { t } from './i18n'
import { quiet } from './quiet'
import { b64, unb64 } from './bytes'

export type Kind = 'github' | 'gitlab' | 'gitea'

/** A token for one host: yours alone (see github.ts). `host` is the web address, e.g. https://gitlab.com. */
export interface Account { token: string; login: string; kind?: Kind; host?: string }

export interface Entry { sha: string; mode: string; size: number } // size -1: not known (GitLab doesn't say)
export interface Tree { tree: string; files: Map<string, Entry> } // tree: the host's own id for the file tree, if it has one

/** A change to send: the file's new content, or null to delete it; `was`, its entry at the parent. */
export interface Change { path: string; data: Uint8Array | null; was: Entry | undefined }

export class GitError extends Error { constructor(msg: string, readonly status = 0) { super(msg) } }
/** The branch moved since TeXLoom last saw it: bring that in first. */
export const MOVED = 422

export const GITHUB = 'https://github.com'
export const kindOf = (a: { kind?: Kind } | null | undefined): Kind => a?.kind ?? 'github'
export const hostOf = (a: { kind?: Kind; host?: string } | null | undefined): string => (kindOf(a) === 'github' ? GITHUB : a?.host ?? '')

/** The service's name as people know it: GitHub, GitLab, Codeberg, or the server's name. */
export function serviceName(a: { kind?: Kind; host?: string } | null | undefined): string {
  const k = kindOf(a), host = hostOf(a)
  if (k === 'github') return 'GitHub'
  const name = host.replace(/^https?:\/\//, '')
  if (k === 'gitlab') return name === 'gitlab.com' ? 'GitLab' : `GitLab (${name})`
  return name === 'codeberg.org' ? 'Codeberg' : name
}

/** Where to make a token, with what it needs. */
export function tokenPage(kind: Kind, host: string): string {
  if (kind === 'github') return 'https://github.com/settings/personal-access-tokens/new'
  if (kind === 'gitlab') return `${host}/-/user_settings/personal_access_tokens?name=TeXLoom&scopes=api`
  return `${host}/user/settings/applications`
}

/** A server address as typed (gitlab.example.org, https://…/), as https://host; null if it isn't one.
 *  Plain http only for this computer (a stand-in when testing). */
export function normalHost(s: string): string | null {
  let v = s.trim().replace(/\/+$/, '')
  if (!v) return null
  if (!/^[a-z]+:\/\//i.test(v)) v = 'https://' + v
  try {
    const u = new URL(v)
    const local = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname)
    if (u.protocol !== 'https:' && !(local && u.protocol === 'http:')) return null
    if (u.username || u.password || u.search || u.hash) return null
    return (u.origin + u.pathname).replace(/\/+$/, '') // GitLab can live under a path
  } catch { return null }
}

/** The repository's path on its host from what was typed: owner/name (GitLab: group/…/name), or
 *  its web or clone address on that host. */
export function repoPath(kind: Kind, host: string, s: string): string | null {
  let v = s.trim().replace(/\/+$/, '').replace(/\.git$/, '')
  const bare = host.replace(/^https?:\/\//, '')
  for (const p of [host + '/', `http://${bare}/`, `https://${bare}/`, `git@${bare.split('/')[0]}:`, `ssh://git@${bare.split('/')[0]}/`]) {
    if (v.toLowerCase().startsWith(p.toLowerCase())) { v = v.slice(p.length); break }
  }
  if (kind === 'gitlab') v = v.split('/-/')[0] // …/group/name/-/tree/main
  else v = v.split('/').slice(0, 2).join('/') // …/owner/name/tree/main
  const parts = v.split('/')
  const ok = parts.length >= 2 && (kind === 'gitlab' || parts.length === 2) && parts.every((x) => /^[A-Za-z0-9_.-]+$/.test(x) && x !== '.' && x !== '..')
  return ok ? v : null
}

const enc = (s: string) => s.split('/').map(encodeURIComponent).join('/')
const FILE = (mode: string) => mode === '100644' || mode === '100755'

export interface Forge {
  readonly name: string
  /** Whose token this is (checks it). */
  login(): Promise<string>
  repoInfo(repo: string): Promise<{ defaultBranch: string; canPush: boolean; empty: boolean }>
  /** The branch's latest commit; null if there is no such branch (or the repository is empty). */
  headOf(repo: string, branch: string): Promise<string | null>
  /** Start a branch at a commit. */
  branchFrom(repo: string, branch: string, commit: string): Promise<void>
  /** A commit's files (path -> entry), every folder down; ordinary files only. */
  filesAt(repo: string, commit: string): Promise<Tree>
  blob(repo: string, sha: string): Promise<Uint8Array>
  /** Commit `changes` on the branch, whose latest commit must still be `parent` (null: the repository
   *  is empty). Returns the new commit, and whether it went straight on top of `parent` (if someone
   *  pushed in between, it lands on top of theirs, and bringing in sorts that out). Throws a
   *  GitError with status MOVED if the branch has moved on. */
  commit(repo: string, branch: string, parent: Tree & { sha: string | null }, changes: Change[], message: string): Promise<{ sha: string; onTop: boolean }>
}

/** Calls an API with the token, turning failures into GitErrors that say what to do. */
function caller(name: string, base: string, auth: string, accept?: string) {
  return async function call(method: string, path: string, body?: unknown, raw = false): Promise<any> {
    let res: Response
    try {
      res = await fetch(base + path, {
        method, cache: 'no-store', body: body ? JSON.stringify(body) : undefined,
        headers: { Authorization: auth, ...(accept ? { Accept: accept } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) },
      })
    } catch { throw new GitError(t('Couldn’t reach {{service}} (offline?).', { service: name })) }
    if (res.ok) return raw ? res : res.status === 204 ? null : res.json()
    const why = await res.json().then((j) => String(j?.message ?? j?.error ?? ''), () => '')
    if (res.status === 401) throw new GitError(t('{{service}} refused the token: it may have expired. Connect again with a new one.', { service: name }), 401)
    if ((res.status === 403 || res.status === 429) && /rate limit|too many/i.test(why + res.status)) throw new GitError(t('{{service}} asks to slow down: trying again later.', { service: name }), 429)
    if (res.status === 403) throw new GitError(t('The token doesn’t allow this: it needs read and write access to the repository’s contents.'), 403)
    if (res.status === 404) throw new GitError(t('{{service}} doesn’t have that repository, or the token can’t see it.', { service: name }), 404)
    throw new GitError(t('{{service}} answered {{status}}{{why}}.', { service: name, status: res.status, why: why ? `: ${why}` : '' }), res.status)
  }
}
const missing = (e: unknown) => e instanceof GitError && (e.status === 404 || e.status === 409)
const tooMany = () => new GitError(t('The repository has too many files for TeXLoom to keep in step.'))

// ---- GitHub ------------------------------------------------------------------------------------
/** api.github.com, or a stand-in when testing on this computer (set texloom.githubApi in localStorage). */
function githubApi(): string {
  try {
    const o = localStorage.getItem('texloom.githubApi')
    if (o && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) return o.replace(/\/+$/, '')
  } catch (e) { quiet('no stand-in for GitHub (testing only)', e) }
  return 'https://api.github.com'
}

function github(token: string): Forge {
  const name = 'GitHub'
  const call = caller(name, githubApi(), `Bearer ${token}`, 'application/vnd.github+json')
  const r = (repo: string) => `/repos/${enc(repo)}`
  const self: Forge = {
    name,
    async login() { return String((await call('GET', '/user'))?.login ?? '') },
    async repoInfo(repo) {
      const j = await call('GET', r(repo))
      return { defaultBranch: String(j.default_branch ?? 'main'), canPush: !!j.permissions?.push, empty: j.size === 0 }
    },
    async headOf(repo, branch) {
      try { return String((await call('GET', `${r(repo)}/git/ref/heads/${enc(branch)}`)).object.sha) }
      catch (e) { if (missing(e)) return null; throw e }
    },
    async branchFrom(repo, branch, commit) { await call('POST', `${r(repo)}/git/refs`, { ref: `refs/heads/${branch}`, sha: commit }) },
    async filesAt(repo, commit) {
      const c = await call('GET', `${r(repo)}/git/commits/${commit}`)
      const tr = await call('GET', `${r(repo)}/git/trees/${c.tree.sha}?recursive=1`)
      if (tr.truncated) throw tooMany()
      const files = new Map<string, Entry>()
      for (const e of tr.tree ?? []) if (e.type === 'blob' && FILE(e.mode)) files.set(e.path, { sha: e.sha, mode: e.mode, size: e.size ?? 0 })
      return { tree: String(c.tree.sha), files }
    },
    async blob(repo, sha) { return unb64(String((await call('GET', `${r(repo)}/git/blobs/${sha}`)).content)) },
    async commit(repo, branch, parent, changes, message) {
      if (!parent.sha) {
        // an empty repository takes no commits through the git API: its first goes through the
        // contents API (one file), and the rest on top of it
        const first = changes.find((c) => c.data && c.path.endsWith('main.tex')) ?? changes.find((c) => c.data)
        if (!first) throw new GitError(t('Nothing to send.'))
        try {
          const j = await call('PUT', `${r(repo)}/contents/${enc(first.path)}`, { message: 'TeXLoom: first commit', content: b64(first.data!), branch })
          const sha = String(j.commit.sha)
          const rest = changes.filter((c) => c !== first)
          if (!rest.length) return { sha, onTop: true }
          return self.commit(repo, branch, { ...(await self.filesAt(repo, sha)), sha }, rest, message)
        } catch (e) { if (e instanceof GitError && e.status === 422) throw new GitError(e.message, MOVED); throw e } // the repository isn't empty after all
      }
      const entries = []
      for (const c of changes) {
        entries.push({ path: c.path, mode: c.was?.mode ?? '100644', type: 'blob',
          sha: c.data ? (await call('POST', `${r(repo)}/git/blobs`, { content: b64(c.data), encoding: 'base64' })).sha : null })
      }
      const tree = await call('POST', `${r(repo)}/git/trees`, { base_tree: parent.tree, tree: entries })
      const commit = await call('POST', `${r(repo)}/git/commits`, { message, tree: tree.sha, parents: [parent.sha] })
      try { await call('PATCH', `${r(repo)}/git/refs/heads/${enc(branch)}`, { sha: commit.sha, force: false }) } // only if it's on top: GitHub checks
      catch (e) { if (e instanceof GitError && e.status === 422) throw new GitError(e.message, MOVED); throw e }
      return { sha: String(commit.sha), onTop: true }
    },
  }
  return self
}

// ---- GitLab ------------------------------------------------------------------------------------
function gitlab(host: string, token: string): Forge {
  const name = serviceName({ kind: 'gitlab', host })
  const call = caller(name, `${host}/api/v4`, `Bearer ${token}`)
  const p = (repo: string) => `/projects/${encodeURIComponent(repo)}`
  const self: Forge = {
    name,
    async login() { return String((await call('GET', '/user'))?.username ?? '') },
    async repoInfo(repo) {
      const j = await call('GET', p(repo))
      const level = Math.max(j.permissions?.project_access?.access_level ?? 0, j.permissions?.group_access?.access_level ?? 0)
      return { defaultBranch: String(j.default_branch ?? 'main'), canPush: level >= 30, empty: !!j.empty_repo } // 30: developer
    },
    async headOf(repo, branch) {
      try { return String((await call('GET', `${p(repo)}/repository/branches/${encodeURIComponent(branch)}`)).commit.id) }
      catch (e) { if (missing(e)) return null; throw e }
    },
    async branchFrom(repo, branch, commit) {
      await call('POST', `${p(repo)}/repository/branches?branch=${encodeURIComponent(branch)}&ref=${commit}`)
    },
    async filesAt(repo, commit) {
      const files = new Map<string, Entry>()
      let url: string | null = `${p(repo)}/repository/tree?ref=${commit}&recursive=true&per_page=100&pagination=keyset`
      for (let pages = 0; url; pages++) {
        if (pages > 500) throw tooMany()
        const res: Response = await call('GET', url, undefined, true)
        for (const e of await res.json()) if (e.type === 'blob' && FILE(e.mode)) files.set(e.path, { sha: e.id, mode: e.mode, size: -1 })
        const next = /<([^>]+)>;\s*rel="next"/.exec(res.headers.get('Link') ?? '')?.[1]
        url = next ? next.slice(next.indexOf('/projects/')) : null
      }
      return { tree: '', files }
    },
    async blob(repo, sha) {
      const res: Response = await call('GET', `${p(repo)}/repository/blobs/${sha}/raw`, undefined, true)
      return new Uint8Array(await res.arrayBuffer())
    },
    async commit(repo, branch, parent, changes, message) {
      // GitLab commits on top of the branch as it is: check it's still where TeXLoom saw it first
      if ((await self.headOf(repo, branch)) !== parent.sha) throw new GitError(t('{{service}} has new commits: bringing them in first.', { service: name }), MOVED)
      const actions = changes.map((c) => c.data
        ? { action: c.was ? 'update' : 'create', file_path: c.path, content: b64(c.data), encoding: 'base64' }
        : { action: 'delete', file_path: c.path })
      const j = await call('POST', `${p(repo)}/repository/commits`, { branch, commit_message: message, actions })
      return { sha: String(j.id), onTop: (j.parent_ids?.[0] ?? null) === parent.sha }
    },
  }
  return self
}

// ---- Gitea and Forgejo -------------------------------------------------------------------------
function gitea(host: string, token: string): Forge {
  const name = serviceName({ kind: 'gitea', host })
  const call = caller(name, `${host}/api/v1`, `token ${token}`)
  const r = (repo: string) => `/repos/${enc(repo)}`
  const self: Forge = {
    name,
    async login() { return String((await call('GET', '/user'))?.login ?? '') },
    async repoInfo(repo) {
      const j = await call('GET', r(repo))
      return { defaultBranch: String(j.default_branch || 'main'), canPush: !!j.permissions?.push, empty: !!j.empty }
    },
    async headOf(repo, branch) {
      try { return String((await call('GET', `${r(repo)}/branches/${encodeURIComponent(branch)}`)).commit.id) }
      catch (e) { if (missing(e)) return null; throw e }
    },
    async branchFrom(repo, branch, commit) {
      await call('POST', `${r(repo)}/branches`, { new_branch_name: branch, old_ref_name: commit })
    },
    async filesAt(repo, commit) {
      const files = new Map<string, Entry>()
      let tree = ''
      for (let page = 1; ; page++) {
        if (page > 500) throw tooMany()
        const j = await call('GET', `${r(repo)}/git/trees/${commit}?recursive=true&per_page=1000&page=${page}`)
        tree = String(j.sha ?? '')
        for (const e of j.tree ?? []) if (e.type === 'blob' && FILE(e.mode)) files.set(e.path, { sha: e.sha, mode: e.mode, size: e.size ?? 0 })
        if (!j.truncated || !(j.tree ?? []).length) break
      }
      return { tree, files }
    },
    async blob(repo, sha) { return unb64(String((await call('GET', `${r(repo)}/git/blobs/${sha}`)).content ?? '')) },
    async commit(repo, branch, parent, changes, message) {
      if ((await self.headOf(repo, branch)) !== parent.sha) throw new GitError(t('{{service}} has new commits: bringing them in first.', { service: name }), MOVED)
      // each changed file names the version it replaces, so a change pushed meanwhile to the same
      // file is never overwritten
      const files = changes.map((c) => c.data
        ? { operation: c.was ? 'update' : 'create', path: c.path, content: b64(c.data), ...(c.was ? { sha: c.was.sha } : {}) }
        : { operation: 'delete', path: c.path, sha: c.was!.sha })
      try {
        const j = await call('POST', `${r(repo)}/contents`, { branch, message, files })
        return { sha: String(j.commit.sha), onTop: (j.commit.parents?.[0]?.sha ?? null) === parent.sha }
      } catch (e) { if (e instanceof GitError && (e.status === 409 || e.status === 422)) throw new GitError(e.message, MOVED); throw e }
    },
  }
  return self
}

/** The API for an account. */
export function forge(a: Account): Forge {
  const k = kindOf(a)
  return k === 'gitlab' ? gitlab(hostOf(a), a.token) : k === 'gitea' ? gitea(hostOf(a), a.token) : github(a.token)
}
