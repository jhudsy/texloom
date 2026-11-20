// Dropbox: keep the project in step with a Dropbox folder, from the browser (Dropbox's API allows
// that). As for GitHub (github.ts), two levels:
// - the account (a token to refresh, from signing in with PKCE: no secret anywhere) is yours alone:
//   kept in this browser, and with a project list it travels with the list, encrypted.
// - the link ("this project ⇄ that Dropbox folder") is the project's, in its shared settings under
//   `dropbox`, with each file's Dropbox revision as last synced (the shared map `dropboxBase`): the
//   base of the next three-way merge, for whoever syncs next. Only changed files are rewritten there.
//
// The folder is known by its id, `.texloom/folder-id` in it (made here if it has none), the same
// one the TeXLoom app gives a synced folder (texloom/sharedfolder.py): a shared Dropbox folder sits
// at a different path in each member's Dropbox, so each browser approves the link with its own
// path, and syncing checks it's the same folder. And when the app on someone's computer keeps that
// same folder in step (Dropbox bringing it there), browsers stand by: changes would otherwise come
// in twice, through the app and through Dropbox.
//
// Bringing changes in: each file whose Dropbox revision changed since it was last synced is merged
// three ways (base: that revision), so collaborators' edits survive; if any clashes, nothing comes
// in. Sending out: once the project has been quiet for a while, each file that differs from its
// last-synced revision is uploaded, only if Dropbox still has that revision (else bring in first).
// Only one copy syncs at a time (the lease `dropboxLease`, as `githubLease` in github.ts).
import * as Y from 'yjs'
import type { Replica } from './replica'
import { applyText, merge3, merge3Strict } from './diff'
import { BUILD, CONFLICT_COPY } from './foldersync'
import { cleanPath, isText } from './projectfiles'
import { t } from './i18n'
import { accountStore } from './accounts'
import { EXPIRE, RENEW, type Lease } from './github'

const MAX = 50 * 1024 * 1024 // files larger than this aren't synced (as everywhere in TeXLoom)
// (quiet: each send is a new version of the changed files in Dropbox, as each save is in its own app)
const PULL_EVERY = 60_000, QUIET = 10_000, TICK = 5000 // ms
const PARALLEL = 4 // files moved at once
const RECHECK = 10 * 60_000 // ms between checks that the approved path is still the linked folder
const APP_FRESH = 70_000 // ms an app's "I keep this folder in step" counts for (EXPIRE in sharedfolder.py)
const HEAR_APPS = 25_000 // ms to listen for apps keeping the folder before syncing it from here
const BLOCK = 4 * 1024 * 1024 // Dropbox's content hash works in blocks of this size
// Only what TeXLoom uses, whatever else the Dropbox app was given (a team permission would make
// Dropbox ask for a work account)
const SCOPES = 'account_info.read files.metadata.read files.content.read files.content.write'

export interface DropboxAccount { refresh: string; access: string; expires: number; id: string; name: string; key: string }
/** The project's link, in its shared settings under `dropbox`. */
export interface DropboxLink {
  folder: string // the folder's id (.texloom/folder-id in it)
  path: string; name: string // where it is in the Dropbox of whoever linked it, and its name
  by: string; at: number // who synced last, and when
  blocked?: string | null // the clashing files' revisions, while they clash (nothing brought in)
  clash?: string[]
}
interface Entry { rev: string; h: string; size: number } // a file in Dropbox: its revision, content hash, size

export const dropboxAccount = accountStore<DropboxAccount>('texloom.dropbox', (a) => typeof a.refresh === 'string' && typeof a.key === 'string')

export class DropboxError extends Error { constructor(msg: string, readonly status = 0, readonly tag = '') { super(msg) } }
const MOVED = 409

// ---- talking to Dropbox ----------------------------------------------------------------

const local = () => /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)
/** Dropbox's addresses, or stand-ins when testing on this computer (texloom.dropboxApi in localStorage). */
function ep(): { api: string; content: string; www: string; notify: string } {
  try {
    const o = JSON.parse(localStorage.getItem('texloom.dropboxApi') || 'null')
    if (o && local()) return { notify: o.api, ...o }
  } catch {}
  return { api: 'https://api.dropboxapi.com', content: 'https://content.dropboxapi.com', www: 'https://www.dropbox.com', notify: 'https://notify.dropboxapi.com' }
}

/** With debugging on (localStorage texloom.debug = 1): requests to Dropbox and the time they took, for the console. */
const debugging = () => { try { return localStorage.getItem('texloom.debug') === '1' } catch { return false } }
const requests = { n: 0, ms: 0 }

/** Run `fn` on each item, `n` at a time. */
async function pool<T, R>(items: T[], n: number, fn: (x: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length)
  let next = 0
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i]) }
  }))
  return out
}

let keyAsked: Promise<string> | null = null
/** This server's Dropbox app (its `dropbox_app_key` setting), or '' if it has none. */
export function dropboxAppKey(): Promise<string> {
  try { const k = localStorage.getItem('texloom.dropboxKey'); if (k && local()) return Promise.resolve(k) } catch {}
  keyAsked ??= fetch('/api/info', { cache: 'no-store' }).then((r) => r.json()).then((j) => String(j?.dropboxAppKey ?? ''), () => '')
  return keyAsked
}

const b64url = (d: Uint8Array) => btoa(String.fromCharCode(...d)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const hex = (d: ArrayBuffer) => [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('')
/** JSON for an HTTP header: anything outside ASCII escaped, as Dropbox asks. */
const headerJson = (v: unknown) => JSON.stringify(v).replace(/[\u007f-￿]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))

/** Sign in to Dropbox (OAuth with PKCE), in this tab: Dropbox's login can stall in a pop-up. This
 *  page goes to Dropbox, which sends the tab back to /dropbox-auth.html; that page swaps the code
 *  for the tokens (with the verifier, kept in this tab's sessionStorage, never sent anywhere else),
 *  keeps the account as dropboxAccount, and returns to `back` (this page, by default). Nothing is
 *  lost by leaving: the project is kept in this browser. */
export async function signIn(back = location.href): Promise<never> {
  const key = await dropboxAppKey()
  if (!key) throw new DropboxError(t('This server isn’t set up for Dropbox.'))
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)))
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))))
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)))
  const redirect = `${location.origin}/dropbox-auth.html`
  try { sessionStorage.setItem('texloom.dropboxPending', JSON.stringify({ verifier, state, key, back, redirect, api: ep().api })) }
  catch { throw new DropboxError(t('This browser won’t keep the sign-in for a moment (private browsing?), so Dropbox can’t be connected here.')) }
  const q = new URLSearchParams({ client_id: key, response_type: 'code', code_challenge: challenge, code_challenge_method: 'S256',
    token_access_type: 'offline', redirect_uri: redirect, state, scope: SCOPES })
  location.assign(`${ep().www}/oauth2/authorize?${q}`)
  return new Promise<never>(() => {}) // (the page is going)
}

/** What the last sign-in left to say, if it didn't work (dropbox-auth.html keeps it), once. */
export function signInProblem(): string {
  try {
    const why = sessionStorage.getItem('texloom.dropboxProblem')
    sessionStorage.removeItem('texloom.dropboxProblem')
    if (!why) return ''
    return why === 'access_denied' ? t('Dropbox access wasn’t allowed.') : t('Dropbox didn’t sign you in ({{why}}).', { why })
  } catch { return '' }
}

async function tokenRequest(form: Record<string, string>): Promise<any> {
  let res: Response
  try { res = await fetch(`${ep().api}/oauth2/token`, { method: 'POST', body: new URLSearchParams(form), cache: 'no-store' }) }
  catch { throw new DropboxError(t('Couldn’t reach Dropbox (offline?).')) }
  const j = await res.json().catch(() => ({}))
  if (!res.ok) throw new DropboxError(res.status === 400 && j?.error === 'invalid_grant'
    ? t('Dropbox no longer accepts this connection: connect again.') : t('Dropbox answered {{status}}{{why}}.', { status: res.status, why: j?.error_description ? `: ${j.error_description}` : '' }), res.status === 400 ? 401 : res.status)
  return j
}

/** A token that works now: refreshed when it has expired (and the account kept with it). */
async function access(a: DropboxAccount, force = false): Promise<string> {
  if (!force && Date.now() < a.expires) return a.access
  const tok = await tokenRequest({ grant_type: 'refresh_token', refresh_token: a.refresh, client_id: a.key })
  a.access = tok.access_token
  a.expires = Date.now() + tok.expires_in * 1000 - 60_000
  if (dropboxAccount.get()?.refresh === a.refresh) dropboxAccount.set(a, dropboxAccount.at()) // (the same account: not a newer setting)
  return a.access
}

async function send(a: DropboxAccount, url: string, init: (token: string) => RequestInit): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    let res: Response
    const began = performance.now()
    try { res = await fetch(url, { method: 'POST', cache: 'no-store', ...init(await access(a, attempt > 0)) }) }
    catch (e) { if (e instanceof DropboxError) throw e; throw new DropboxError(t('Couldn’t reach Dropbox (offline?).')) }
    finally { requests.n++; requests.ms += performance.now() - began }
    if (res.status === 401 && attempt === 0) continue // the token expired early: refresh once
    if (res.ok) return res
    const text = await res.text().catch(() => '')
    let j: any = null
    try { j = JSON.parse(text) } catch {}
    const summary = String(j?.error_summary ?? text ?? '')
    if (res.status === 401) throw new DropboxError(t('Dropbox no longer accepts this connection: connect again.'), 401)
    if (res.status === 429) throw new DropboxError(t('Dropbox asks to slow down: trying again later.'), 429)
    if (res.status === 409) throw new DropboxError(summary.startsWith('path/not_found') || summary.startsWith('path_lookup/not_found')
      ? t('Dropbox has no such folder or file.') : t('Dropbox refused: {{why}}', { why: summary }), 409, summary)
    throw new DropboxError(t('Dropbox answered {{status}}{{why}}.', { status: res.status, why: summary ? `: ${summary}` : '' }), res.status)
  }
}

const rpc = async (a: DropboxAccount, path: string, body: unknown) => (await send(a, ep().api + path, (tk) => ({
  headers: { Authorization: `Bearer ${tk}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) }))).json()

/** A file's content, by path or by revision ("rev:…"). */
async function download(a: DropboxAccount, path: string): Promise<Uint8Array> {
  const res = await send(a, `${ep().content}/2/files/download`, (tk) => ({ headers: { Authorization: `Bearer ${tk}`, 'Dropbox-API-Arg': headerJson({ path }) } }))
  return new Uint8Array(await res.arrayBuffer())
}

/** Upload a file: new (`rev` null; refused if one is there now) or replacing that revision (refused
 *  if it has changed since). Returns its new entry. */
async function upload(a: DropboxAccount, path: string, data: Uint8Array, rev: string | null): Promise<Entry> {
  const arg = { path, mode: rev ? { '.tag': 'update', update: rev } : 'add', autorename: false, mute: true, strict_conflict: true }
  try {
    const res = await send(a, `${ep().content}/2/files/upload`, (tk) => ({
      headers: { Authorization: `Bearer ${tk}`, 'Dropbox-API-Arg': headerJson(arg), 'Content-Type': 'application/octet-stream' }, body: data as BodyInit }))
    const j = await res.json()
    return { rev: String(j.rev), h: String(j.content_hash), size: Number(j.size) }
  } catch (e) {
    if (e instanceof DropboxError && e.status === 409 && /conflict/.test(e.tag)) throw new DropboxError(t('Dropbox has newer changes: bringing them in first.'), MOVED, 'moved')
    throw e
  }
}

/** Dropbox's content hash: SHA-256 of each 4 MiB block, then SHA-256 of those together. */
export async function contentHash(d: Uint8Array): Promise<string> {
  const parts: Uint8Array[] = [] // (an empty file has no blocks)
  for (let i = 0; i < d.length; i += BLOCK) parts.push(new Uint8Array(await crypto.subtle.digest('SHA-256', d.subarray(i, i + BLOCK) as BufferSource)))
  const all = new Uint8Array(parts.length * 32)
  parts.forEach((p, i) => all.set(p, i * 32))
  return hex(await crypto.subtle.digest('SHA-256', all as BufferSource))
}

/** A folder path as Dropbox wants it: "/Papers/thesis", no slash at the end; null if it isn't one. */
export function folderPath(s: string): string | null {
  const p = '/' + s.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '').replace(/\/+/g, '/')
  return p !== '/' && p.split('/').slice(1).every((x) => x && x !== '.' && x !== '..') ? p : null
}

/** The folders at the top of the Dropbox (for choosing one). */
export async function topFolders(a: DropboxAccount): Promise<string[]> {
  const j = await rpc(a, '/2/files/list_folder', { path: '', recursive: false, include_deleted: false, limit: 500 })
  return (j.entries ?? []).filter((e: any) => e['.tag'] === 'folder').map((e: any) => String(e.path_display)).sort()
}

/** The folder's listing as last fetched, and Dropbox's cursor for what changed since (per account and folder). */
const listings = new Map<string, { files: Map<string, Entry>; cursor: string }>()

/** Every file in the folder (path in it -> entry), every folder down. After the first time, only
 *  what changed since is asked for (Dropbox's cursor), and applied to the listing kept here. */
async function listFiles(a: DropboxAccount, root: string): Promise<{ files: Map<string, Entry>; cursor: string }> {
  const key = `${a.id} ${root.toLowerCase()}`
  const rootLower = root.toLowerCase()
  const known = listings.get(key)
  const files = new Map(known?.files ?? [])
  let j: any
  try {
    j = known ? await rpc(a, '/2/files/list_folder/continue', { cursor: known.cursor })
      : await rpc(a, '/2/files/list_folder', { path: root, recursive: true, include_deleted: true, limit: 2000 })
  } catch (e) {
    if (known && e instanceof DropboxError && e.status === 409 && /reset/.test(e.tag)) { listings.delete(key); return listFiles(a, root) } // (the cursor has expired)
    throw e
  }
  for (let pages = 0; ; pages++) {
    for (const e of j.entries ?? []) {
      const lower = String(e.path_lower)
      if (!lower.startsWith(rootLower + '/')) continue
      const relLower = lower.slice(rootLower.length + 1)
      if (e['.tag'] === 'deleted') { // a file or a folder (then everything in it)
        for (const k of [...files.keys()]) { const kl = k.toLowerCase(); if (kl === relLower || kl.startsWith(relLower + '/')) files.delete(k) }
        continue
      }
      if (e['.tag'] !== 'file') continue
      const rel = String(e.path_display).split('/').slice(-relLower.split('/').length).join('/') // (the display name's case)
      for (const k of [...files.keys()]) if (k !== rel && k.toLowerCase() === relLower) files.delete(k) // (renamed in case only)
      files.set(rel, { rev: String(e.rev), h: String(e.content_hash ?? ''), size: Number(e.size ?? 0) })
    }
    if (!j.has_more) break
    if (pages > 200) throw new DropboxError(t('The folder has too many files for TeXLoom to keep in step.'))
    j = await rpc(a, '/2/files/list_folder/continue', { cursor: j.cursor })
  }
  listings.set(key, { files, cursor: String(j.cursor) })
  return { files: new Map(files), cursor: String(j.cursor) }
}

/** Wait (up to `secs`) until something changes in the folder after `cursor`: Dropbox's long poll,
 *  so changes made there come in within seconds. Needs no token. True if something changed. */
async function changesAfter(cursor: string, secs: number): Promise<{ changes: boolean; backoff: number }> {
  const res = await fetch(`${ep().notify}/2/files/list_folder/longpoll`, {
    method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cursor, timeout: secs }) })
  if (!res.ok) throw new DropboxError(t('Dropbox answered {{status}}{{why}}.', { status: res.status, why: '' }), res.status)
  const j = await res.json()
  return { changes: !!j.changes, backoff: Number(j.backoff ?? 0) }
}

/** The folder's id (.texloom/folder-id in it), made if it has none. */
export async function folderIdOf(a: DropboxAccount, root: string, make: boolean): Promise<string | null> {
  const at = `${root}/.texloom/folder-id`
  try {
    const id = new TextDecoder().decode(await download(a, at)).trim()
    if (/^[0-9a-f]{32}$/.test(id)) return id
  } catch (e) { if (!(e instanceof DropboxError && e.status === 409)) throw e }
  if (!make) return null
  const id = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, '0')).join('')
  try { await upload(a, at, new TextEncoder().encode(id), null) }
  catch (e) { if (e instanceof DropboxError && e.tag === 'moved') return folderIdOf(a, root, false); throw e } // made elsewhere just now
  return id
}

// ---- the project's side ----------------------------------------------------------------

export function linkOf(r: Replica): DropboxLink | null {
  const v = r.meta.get('dropbox') as DropboxLink | undefined
  return v && typeof v === 'object' && typeof v.folder === 'string' && /^[0-9a-f]{32}$/.test(v.folder) && typeof v.path === 'string' ? v : null
}
export function setLink(r: Replica, l: DropboxLink | null) { if (l) r.meta.set('dropbox', l); else r.meta.delete('dropbox') }
const baseMap = (r: Replica) => r.doc.getMap<Entry>('dropboxBase')

// As for GitHub: the link is in the shared settings, and syncing uses this browser's own token, so
// a browser syncs only a link its user made or approved here: the folder, at this Dropbox's path.
const approvedKey = (r: Replica) => `texloom.dropboxApproved.${r.keys.project}`
export function approvedPath(r: Replica, l: DropboxLink | null = linkOf(r)): string | null {
  try {
    const v = JSON.parse(localStorage.getItem(approvedKey(r)) || 'null')
    return l && v && v.folder === l.folder && typeof v.path === 'string' && v.account === dropboxAccount.get()?.id ? v.path : null
  } catch { return null }
}
export function approve(r: Replica, l: { folder: string } | null, path = '') {
  try {
    if (l) localStorage.setItem(approvedKey(r), JSON.stringify({ folder: l.folder, path, account: dropboxAccount.get()?.id ?? '' }))
    else localStorage.removeItem(approvedKey(r))
  } catch {}
}

/** A Dropbox file the project keeps: a name TeXLoom accepts, not build output, not a conflicted copy, not too big. */
const kept = (rel: string, size: number) => cleanPath(rel) === rel && !BUILD.test(rel) && !CONFLICT_COPY.test(rel.split('/').pop()!) && size <= MAX

type Mine = { text: string } | { h: string; size: number }
function projectFiles(r: Replica): Map<string, Mine> {
  const out = new Map<string, Mine>()
  r.files.forEach((yt, p) => { if (kept(p, 0)) out.set(p, { text: yt.toString() }) })
  for (const [p, b] of r.bin.entries()) if (b && typeof b.h === 'string' && kept(p, b.size ?? 0)) out.set(p, { h: b.h, size: b.size })
  return out
}
const bytesOf = async (r: Replica, m: Mine) => ('text' in m ? new TextEncoder().encode(m.text) : await r.getBlob(m.h))
/** Content hashes of stored files, by TeXLoom id (both name the content, so this never goes stale). */
function hashCache(r: Replica) {
  const key = `texloom.dropboxHash.${r.keys.project}`
  let m: Record<string, string> = {}
  try { m = JSON.parse(localStorage.getItem(key) || '{}') } catch {}
  return {
    get: (h: string) => m[h],
    set: (h: string, v: string) => { m[h] = v; try { localStorage.setItem(key, JSON.stringify(m)) } catch {} },
  }
}
async function hashOfMine(r: Replica, m: Mine, cache: ReturnType<typeof hashCache>): Promise<string> {
  if ('text' in m) return contentHash(new TextEncoder().encode(m.text))
  const known = cache.get(m.h)
  if (known) return known
  const h = await contentHash(await r.getBlob(m.h))
  cache.set(m.h, h)
  return h
}

/** Whether the TeXLoom app on someone's computer keeps this folder in step (Dropbox bringing it there). */
export function appHolds(r: Replica, l: DropboxLink | null = linkOf(r)): { by: string } | null {
  const f = l ? r.folders.get(l.folder) : undefined
  return f && f.holding && Date.now() - f.at < APP_FRESH ? { by: f.by } : null
}

/** Where this browser syncs the link: its approved path, checked to be the same folder. */
const checked = new Map<string, number>() // folder, path and account -> when last found to match
async function where(r: Replica, a: DropboxAccount): Promise<{ link: DropboxLink; root: string }> {
  const link = linkOf(r)
  if (!link) throw new DropboxError(t('This project isn’t linked to a Dropbox folder.'))
  const root = approvedPath(r, link)
  if (!root) throw new DropboxError(t('This browser hasn’t approved syncing the project’s Dropbox folder.'))
  const key = `${link.folder} ${root} ${a.id}`
  if (Date.now() - (checked.get(key) ?? 0) > RECHECK) { // (not every round: once in a while is enough)
    const id = await folderIdOf(a, root, false)
    if (id !== link.folder) { checked.delete(key); throw new DropboxError(t('{{path}} in your Dropbox isn’t the folder this project is linked to.', { path: root })) }
    checked.set(key, Date.now())
  }
  return { link, root }
}

// ---- bringing changes in -----------------------------------------------------------------

export interface PullResult { files: string[]; clash: string[] }

/** Bring Dropbox's changes into the project. With `oursWins`, where both changed the same thing
 *  the project's version stays (and goes up with the next send) instead of nothing coming in. */
export async function pull(r: Replica, a: DropboxAccount, by: string, oursWins = false, still: () => boolean = () => true): Promise<PullResult> {
  const { link, root } = await where(r, a)
  const now = (await listFiles(a, root)).files
  const base = new Map(baseMap(r).entries())
  const cache = hashCache(r), mine = projectFiles(r)
  type Step = { rel: string; was: Mine | null; take?: Uint8Array | null; merge?: { base: string; theirs: string } }
  const steps: Step[] = [], clash: string[] = [], same: string[] = []
  const changed = [...new Set([...base.keys(), ...now.keys()])].filter((rel) => {
    const b = base.get(rel), th = now.get(rel)
    return (b?.h ?? null) !== (th?.h ?? null) && kept(rel, th?.size ?? b?.size ?? 0)
  })
  await pool(changed, PARALLEL, async (rel) => { // what to do with each file Dropbox changed (fetching what that needs)
    const b = base.get(rel), th = now.get(rel)
    const m = mine.get(rel) ?? null
    const h = m ? await hashOfMine(r, m, cache) : null
    if (h === (th?.h ?? null)) { same.push(rel); return } // the project has it already
    if (h === (b?.h ?? null)) { // unchanged here
      const take = th ? await download(a, `rev:${th.rev}`) : null
      if (!take || take.length <= MAX) steps.push({ rel, was: m, take })
      return
    }
    if (m && 'text' in m && b && th) {
      const [old, theirs] = await Promise.all([
        download(a, `rev:${b.rev}`).catch((e) => { if (e instanceof DropboxError && e.status === 409) return null; throw e }), // (kept 30 days or more)
        download(a, `rev:${th.rev}`)])
      const bt = old && isText(rel, old), tt = isText(rel, theirs)
      if (bt != null && tt !== null) { steps.push({ rel, was: m, merge: { base: bt, theirs: tt } }); return }
    }
    clash.push(rel)
  })
  if (!still()) return { files: [], clash: [] }
  for (const st of steps) if (st.merge && !oursWins && merge3Strict(st.merge.base, st.merge.theirs, (st.was as { text: string }).text) === null) clash.push(st.rel)
  const blocked = clash.sort().map((p) => `${p}@${now.get(p)?.rev ?? '-'}`).join(' ')
  if (clash.length && !oursWins) {
    setLink(r, { ...linkOf(r)!, blocked, clash, by, at: Date.now() })
    return { files: [], clash }
  }
  const uploads = new Map<string, string>()
  for (const st of steps) if (st.take && isText(st.rel, st.take) === null) uploads.set(st.rel, await r.putBlob(st.take))
  const files: string[] = []
  let moved = false
  r.doc.transact(() => {
    const cur = (rel: string): Mine | null => { const yt = r.files.get(rel), b = r.bin.get(rel); return yt ? { text: yt.toString() } : b ? { h: b.h, size: b.size } : null }
    const sameMine = (x: Mine | null, y: Mine | null) => (!x || !y ? x === y : 'text' in x ? 'text' in y && x.text === y.text : 'h' in y && x.h === y.h)
    const todo: (() => void)[] = []
    for (const st of steps) {
      const c = cur(st.rel)
      if (st.merge) {
        if (!c || !('text' in c)) { moved = true; return }
        const merged = oursWins ? merge3(st.merge.base, st.merge.theirs, c.text) : merge3Strict(st.merge.base, st.merge.theirs, c.text)
        if (merged === null) { moved = true; return }
        if (merged !== c.text) { const yt = r.files.get(st.rel)!; todo.push(() => applyText(yt, c.text, merged)); files.push(st.rel) }
        continue
      }
      if (!sameMine(c, st.was)) { moved = true; return } // changed here meanwhile: next round decides again
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
    // what was taken, merged or already the same is now in step with Dropbox's revision
    const bm = baseMap(r)
    for (const rel of [...steps.map((s) => s.rel), ...same]) { const th = now.get(rel); if (th) bm.set(rel, th); else bm.delete(rel) }
    if (link.blocked) setLink(r, { ...linkOf(r)!, blocked: null, clash: [], by, at: Date.now() })
  })
  return moved ? { files: [], clash: [] } : { files, clash: [] }
}

// ---- sending changes out -------------------------------------------------------------------

/** Send each file that differs from its last-synced revision (deleting those deleted here). Throws a
 *  DropboxError with status MOVED if Dropbox changed one meanwhile (bring that in first). */
export async function push(r: Replica, a: DropboxAccount, by: string, still: () => boolean = () => true): Promise<string[]> {
  const { root } = await where(r, a)
  const base = baseMap(r), cache = hashCache(r), mine = projectFiles(r)
  const sent: string[] = []
  const ups: [string, Mine][] = []
  for (const [rel, m] of mine) { const b = base.get(rel); if (!b || b.h !== await hashOfMine(r, m, cache)) ups.push([rel, m]) }
  const gone = [...base.entries()].filter(([rel, b]) => !mine.has(rel) && kept(rel, b.size))
  // each file on its own (each success noted at once, so a send cut short is finished by the next)
  await pool([...ups.map((u) => ['up', ...u] as const), ...gone.map((g) => ['rm', ...g] as const)], PARALLEL, async (job) => {
    if (!still()) return
    const rel = job[1]
    if (job[0] === 'up') {
      base.set(rel, await upload(a, `${root}/${rel}`, await bytesOf(r, job[2]), base.get(rel)?.rev ?? null))
    } else {
      try { await rpc(a, '/2/files/delete_v2', { path: `${root}/${rel}`, parent_rev: job[2].rev }) }
      catch (e) {
        if (!(e instanceof DropboxError && e.status === 409)) throw e
        if (!/not_found/.test(e.tag)) throw new DropboxError(t('Dropbox has newer changes: bringing them in first.'), MOVED, 'moved')
      }
      base.delete(rel)
    }
    sent.push(rel)
  })
  if (sent.length) setLink(r, { ...linkOf(r)!, by, at: Date.now() })
  return sent
}

/** Link the project to a Dropbox folder (made if it doesn't exist). Files only in one of the two
 *  end up in both; where both have a file but it differs, `prefer` says whose stays ('project':
 *  it goes up with the first send). Returns how many files came in. */
export async function linkFolder(r: Replica, a: DropboxAccount, by: string, path: string, prefer: 'dropbox' | 'project'): Promise<number> {
  try { await rpc(a, '/2/files/create_folder_v2', { path, autorename: false }) }
  catch (e) { if (!(e instanceof DropboxError && e.status === 409 && /conflict/.test(e.tag))) throw e } // (it's there)
  const folder = (await folderIdOf(a, path, true))!
  const name = path.split('/').pop() || path
  approve(r, { folder }, path) // linking it is approving it
  const now = (await listFiles(a, path)).files
  const cache = hashCache(r), mine = projectFiles(r)
  const take: [string, Uint8Array, Entry][] = []
  for (const [rel, e] of now) {
    if (!kept(rel, e.size)) continue
    const m = mine.get(rel)
    if (m && (prefer === 'project' || (await hashOfMine(r, m, cache)) === e.h)) continue
    take.push([rel, await download(a, `rev:${e.rev}`), e])
  }
  const uploads = new Map<string, string>()
  for (const [rel, d] of take) if (isText(rel, d) === null) uploads.set(rel, await r.putBlob(d))
  r.doc.transact(() => {
    const bm = baseMap(r)
    for (const k of [...bm.keys()]) bm.delete(k)
    for (const [rel, e] of now) if (kept(rel, e.size)) bm.set(rel, e) // (the project's differing files go up as changes to these)
    for (const [rel, d] of take) {
      const text = isText(rel, d)
      if (text === null) { r.files.delete(rel); r.bin.set(rel, { h: uploads.get(rel)!, size: d.length }); continue }
      r.bin.delete(rel)
      const yt = r.files.get(rel)
      if (yt) applyText(yt, yt.toString(), text); else r.files.set(rel, new Y.Text(text))
    }
    setLink(r, { folder, path, name, by, at: Date.now(), blocked: null, clash: [] })
  })
  return take.length
}

export function unlink(r: Replica) {
  r.doc.transact(() => { setLink(r, null); r.meta.delete('dropboxLease'); const bm = baseMap(r); for (const k of [...bm.keys()]) bm.delete(k) })
  approve(r, null)
}

// ---- who syncs: the lease -------------------------------------------------------------------

function tabId(): string {
  try {
    let id = sessionStorage.getItem('texloom.tab')
    if (!id) { id = 'web:' + crypto.randomUUID(); sessionStorage.setItem('texloom.tab', id) }
    return id
  } catch { return 'web:' + Math.random().toString(36).slice(2) }
}

export interface SyncState { role: 'off' | 'syncing' | 'standby' | 'waiting' | 'app'; holder: string | null; error: string | null; busy: boolean }

/** Run the link while the project is open, as startGitHub does for a repository: one copy at a
 *  time, bringing changes in every minute and sending after QUIET; standing by while the TeXLoom app
 *  on someone's computer keeps the same folder. */
export function startDropbox(r: Replica, me: () => string, onChange: () => void) {
  const id = tabId()
  const state: SyncState = { role: 'off', holder: null, error: null, busy: false }
  const started = Date.now()
  let seen = Date.now(), renewed = 0, claimed = 0, changedAt = 0, dirty = true, nextPull = 0, moved = 0
  const lease = () => { const l = r.meta.get('dropboxLease') as Lease | undefined; return l && typeof l.id === 'string' ? l : null }
  r.meta.observe((ev) => {
    if (ev.keysChanged.has('dropboxLease')) seen = Date.now()
    if (ev.keysChanged.has('dropbox')) dirty = true
  })
  const touched = (_e: unknown, tx: Y.Transaction) => { if (!tx.origin || r.isLocal(tx.origin) || r.isRemote(tx.origin)) { changedAt = Date.now(); dirty = true } }
  r.files.observeDeep(touched as any); r.bin.observe(touched as any)
  const set = (patch: Partial<SyncState>) => {
    if (Object.entries(patch).every(([k, v]) => (state as any)[k] === v)) return
    Object.assign(state, patch); onChange()
  }
  const mine = () => lease()?.id === id

  async function round(force = false) {
    const a = dropboxAccount.get(), link = linkOf(r)
    if (!a || !link || !approvedPath(r, link) || !r.keys.writable || r.deletedAt) { if (mine()) r.meta.delete('dropboxLease'); set({ role: 'off', holder: null }); return }
    const app = appHolds(r, link)
    if (app) { if (mine()) r.meta.delete('dropboxLease'); set({ role: 'app', holder: app.by }); return } // the app and Dropbox carry it
    if (Date.now() - started < HEAR_APPS && !force) { set({ role: 'waiting', holder: null }); return } // first, hear whether an app keeps it
    const l = lease()
    if (!mine()) {
      if (l && Date.now() - seen < EXPIRE) { set({ role: 'standby', holder: l.by }); return }
      if (r.apart) { set({ role: 'waiting', holder: l?.by ?? null }); return }
      r.meta.set('dropboxLease', { id, by: me(), n: 0 }); claimed = renewed = Date.now()
      set({ role: 'waiting', holder: me() })
      return
    }
    if (Date.now() - claimed < 2 * TICK && !force) return
    if (Date.now() - renewed >= RENEW) { r.meta.set('dropboxLease', { id, by: me(), n: (l?.n ?? 0) + 1 }); renewed = Date.now() }
    if (r.apart) { set({ role: 'waiting', holder: me() }); return }
    set({ role: 'syncing', holder: me() })
    if (state.busy) { if (force) again = true; return } // (a Sync now during a round: another straight after)
    if (!force && Date.now() < nextPull && !dirty) return // nothing to do this time (and the screen needn't redraw)
    set({ busy: true }) // (through set, so the screen hears it: and hears it end)
    const began = performance.now(), before = { ...requests }
    let came: string[] = [], went: string[] = []
    try {
      if (force || Date.now() >= nextPull || dirty) {
        const p = await pull(r, a, me(), false, mine)
        nextPull = Date.now() + PULL_EVERY
        came = p.files
        if (p.files.length) dirty = true
        if (p.clash.length) dirty = false // a clash waits for someone: look again when something changes, not every few seconds
      }
      const cur = linkOf(r)
      if (cur && !cur.blocked && (force || (dirty && Date.now() - changedAt >= QUIET))) {
        dirty = false
        try { went = await push(r, a, me(), mine); moved = 0 }
        catch (e) {
          if (!(e instanceof DropboxError) || e.status !== MOVED || ++moved > 3) { moved = 0; throw e }
          dirty = true; nextPull = 0
        }
      }
      set({ error: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) })
    } finally {
      set({ busy: false })
      if (debugging() && (force || came.length || went.length)) console.debug(`[dropbox] ${force ? 'sync now' : 'round'}: ${Math.round(performance.now() - began)} ms, `
        + `${requests.n - before.n} requests to Dropbox (${Math.round(requests.ms - before.ms)} ms in all, ${PARALLEL} at once); in: ${came.join(', ') || 'nothing'}; out: ${went.join(', ') || 'nothing'}`)
      if (again) { again = false; queueMicrotask(() => { round(true).catch(() => {}) }) }
    }
  }
  let again = false, stopped = false
  const timer = setInterval(() => { round().catch(() => {}) }, TICK)
  const release = () => { if (mine()) r.meta.delete('dropboxLease') }
  // leaving the tab (or closing it): send what's waiting now rather than after the quiet time
  const hidden = () => { if (document.visibilityState === 'hidden' && dirty && mine()) round(true).catch(() => {}) }
  document.addEventListener('visibilitychange', hidden)
  window.addEventListener('pagehide', release)
  // while this tab syncs, Dropbox tells it when the folder changes (long poll), so changes made
  // there come in within seconds rather than at the next minute's round
  ;(async () => {
    while (!stopped) {
      const a = dropboxAccount.get(), l = linkOf(r), root = l && approvedPath(r, l)
      const cursor = a && root ? listings.get(`${a.id} ${root.toLowerCase()}`)?.cursor : undefined
      if (state.role !== 'syncing' || !cursor) { await new Promise((ok) => setTimeout(ok, TICK)); continue }
      try {
        const { changes, backoff } = await changesAfter(cursor, 120)
        if (changes && !stopped) { nextPull = 0; await round().catch(() => {}) }
        if (backoff) await new Promise((ok) => setTimeout(ok, backoff * 1000))
      } catch { await new Promise((ok) => setTimeout(ok, 30_000)) } // offline, or Dropbox busy: the minute's round goes on
    }
  })()
  round().catch(() => {})
  return {
    state,
    now: () => round(true),
    stop() { stopped = true; clearInterval(timer); document.removeEventListener('visibilitychange', hidden); window.removeEventListener('pagehide', release); release() },
  }
}
