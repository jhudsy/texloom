// Keep the list of your projects on the server, encrypted, so it follows you to other browsers
// and computers. Must match texloom/listsync.py exactly.
//
// A list is opened by one random secret, carried in a "sync link" (…/#sync=…: like a project
// link, the part after # never reaches the server). From it come an encryption key and a signing
// key whose public half is the list's id, so the server holds something it can neither read nor
// change. Passkeys and passwords are other ways in: each yields a secret that opens a small entry
// of the same kind, holding the list's secret. A further entry, opened by the list's secret,
// records those entries' signing keys, so that they can be removed.
import { ed25519 } from '@noble/curves/ed25519.js'
import { scryptAsync } from '@noble/hashes/scrypt.js'
import { b64d, b64e } from './crypto'
import { dialog, h } from './dom'
import { locale, t, tn } from './i18n'
import { projects } from './projects'
import { creationCode } from './replica'
import { askCode } from './storagenotice'

export interface ListEntry { f: string; n: string; at: number; w: boolean; nick?: string; t?: string; by?: string }
export interface ListDoc { v: 1; projects: Record<string, ListEntry>; gone: Record<string, number> }

const STORE = 'texloom.listKey'
const YEAR = 365 * 86400_000
const PASSKEY_SALT = new TextEncoder().encode('texloom project list v1')
const enc = new TextEncoder(), dec = new TextDecoder()

class ListError extends Error {}

// ---- keys ----------------------------------------------------------------------------------
const hmac = async (key: Uint8Array, label: string) => new Uint8Array(await crypto.subtle.sign('HMAC',
  await crypto.subtle.importKey('raw', key as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']), enc.encode(label) as BufferSource))

class ListKey {
  private constructor(readonly secret: Uint8Array, readonly id: string, private seed: Uint8Array, private aes: CryptoKey) {}

  static async of(secret: Uint8Array): Promise<ListKey> {
    const seed = await hmac(secret, 'texloom list sign')
    const aes = await crypto.subtle.importKey('raw', await hmac(secret, 'texloom list encrypt') as BufferSource, 'AES-GCM', false, ['encrypt', 'decrypt'])
    return new ListKey(secret, b64e(ed25519.getPublicKey(seed)), seed, aes)
  }

  async encrypt(plain: Uint8Array): Promise<Uint8Array> {
    const iv = crypto.getRandomValues(new Uint8Array(12))
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, this.aes, plain as BufferSource))
    const out = new Uint8Array(12 + ct.length)
    out.set(iv); out.set(ct, 12)
    return out
  }

  async decrypt(data: Uint8Array): Promise<Uint8Array> {
    return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: data.subarray(0, 12) as BufferSource }, this.aes, data.subarray(12) as BufferSource))
  }

  get signingSeed(): string { return b64e(this.seed) }

  signPut(seq: number, data: Uint8Array): string {
    const head = enc.encode(`list:${this.id}:${seq}:`)
    const msg = new Uint8Array(head.length + data.length)
    msg.set(head); msg.set(data, head.length)
    return b64e(ed25519.sign(msg, this.seed))
  }
}

const newSecret = () => crypto.getRandomValues(new Uint8Array(32))
const linkFor = (secret: Uint8Array) => `${location.origin}/#sync=${b64e(secret)}`

function savedSecret(): Uint8Array | null {
  try {
    const s = localStorage.getItem(STORE)
    return s ? b64d(s) : null
  } catch { return null }
}
function saveSecret(secret: Uint8Array | null) {
  try {
    if (secret) localStorage.setItem(STORE, b64e(secret))
    else localStorage.removeItem(STORE)
  } catch {}
}

// ---- merging -------------------------------------------------------------------------------
const emptyList = (): ListDoc => ({ v: 1, projects: {}, gone: {} })

/** One spelling of a document, to tell whether two are the same. */
export function canon(x: unknown): string {
  if (Array.isArray(x)) return `[${x.map(canon).join(',')}]`
  if (x && typeof x === 'object') {
    const o = x as Record<string, unknown>
    return `{${Object.keys(o).filter((k) => o[k] !== undefined).sort().map((k) => `${JSON.stringify(k)}:${canon(o[k])}`).join(',')}}`
  }
  return JSON.stringify(x)
}

/** Two lists as one, the same whichever way round. The most recently opened copy of an entry
 *  wins, except that an edit link is never replaced by a read-only one; removed projects stay
 *  removed unless they were opened again afterwards. */
export function mergeLists(a: ListDoc, b: ListDoc): ListDoc {
  const out = emptyList()
  for (const doc of [a, b]) for (const [p, when] of Object.entries(doc.gone ?? {})) out.gone[p] = Math.max(out.gone[p] ?? 0, when)
  for (const p of new Set([...Object.keys(a.projects ?? {}), ...Object.keys(b.projects ?? {})])) {
    const x = a.projects?.[p], y = b.projects?.[p]
    let e: ListEntry
    if (x && y) {
      const xFirst = x.at !== y.at ? x.at > y.at : canon(x) >= canon(y)
      const [fresh, old] = xFirst ? [x, y] : [y, x]
      e = { ...old, ...Object.fromEntries(Object.entries(fresh).filter(([, v]) => v !== undefined && v !== '')) } as ListEntry
      if (old.w && !fresh.w) { e.f = old.f; e.w = true }
    } else e = { ...(x ?? y)! }
    if (e.at > (out.gone[p] ?? 0)) out.projects[p] = e
  }
  return out
}

function forgetOldRemovals(doc: ListDoc): ListDoc {
  const cutoff = Date.now() - YEAR
  return { ...doc, gone: Object.fromEntries(Object.entries(doc.gone).filter(([, when]) => when >= cutoff)) }
}

// ---- the server ----------------------------------------------------------------------------
async function pull(key: ListKey): Promise<{ seq: number; plain: Uint8Array } | null> {
  const r = await fetch(`/api/list/${key.id}`, { cache: 'no-store' })
  if (r.status === 404) return null
  if (r.status === 410) throw new ListError('list-retired')
  if (!r.ok) throw new ListError('server')
  return { seq: Number(r.headers.get('X-Seq')), plain: await key.decrypt(new Uint8Array(await r.arrayBuffer())) }
}

/** False if someone else got there first. */
async function push(key: ListKey, seq: number, plain: Uint8Array): Promise<boolean> {
  const data = await key.encrypt(plain)
  const headers: Record<string, string> = { 'X-Seq': String(seq), 'X-Sig': key.signPut(seq, data), 'Content-Type': 'application/octet-stream' }
  const code = creationCode.get()
  if (code) headers['X-Create-Code'] = code
  const r = await fetch(`/api/list/${key.id}`, { method: 'PUT', headers, body: data as BodyInit })
  if (r.status === 409) return false
  if (!r.ok) throw new ListError(await r.json().then((x) => String(x.code ?? 'server'), () => 'server'))
  return true
}

/** Merge `local` with the server's list and update the server if that changed it. */
async function exchange(key: ListKey, local: ListDoc): Promise<ListDoc> {
  for (let i = 0; i < 5; i++) {
    const remote = await pull(key)
    const had = remote ? JSON.parse(dec.decode(remote.plain)) as ListDoc : null
    const merged = forgetOldRemovals(mergeLists(had ?? emptyList(), local))
    if (had && canon(merged) === canon(had)) return merged
    if (await push(key, (remote?.seq ?? 0) + 1, enc.encode(canon(merged)))) return merged
  }
  throw new ListError('busy')
}

// ---- this browser --------------------------------------------------------------------------
let lastSynced = ''
let lastAttempt = 0
let lastError: string | null = null
let timer: ReturnType<typeof setTimeout> | undefined
let running: Promise<void> | null = null
let changed: () => void = () => {}

function syncNow(): Promise<void> {
  if (!savedSecret()) return Promise.resolve()
  return (running ??= (async () => {
    try {
      lastAttempt = Date.now()
      const doc = await exchange(await ListKey.of(savedSecret()!), projects.asList())
      lastSynced = canon(doc)
      lastError = null
      if (projects.applyList(doc)) changed()
    } catch (e) {
      lastError = e instanceof ListError ? e.message : 'network'
      throw e
    } finally { running = null }
  })())
}
const syncQuietly = () => { syncNow().catch(() => {}) }

function schedule() {
  if (!savedSecret() || canon(projects.asList()) === lastSynced) return
  clearTimeout(timer)
  timer = setTimeout(syncQuietly, 3000)
}

/** Keep this browser's list up to date; `onChange` runs when another device changed it. */
export function startListSync(onChange: () => void) {
  changed = onChange
  projects.onChange(schedule)
  syncQuietly()
  window.addEventListener('focus', () => { if (Date.now() - lastAttempt > 60_000) syncQuietly() })
  window.addEventListener('online', syncQuietly)
}

function explain(code: string | null): string {
  switch (code) {
    case 'needs-code': return t('This server needs a creation code to store a list.')
    case 'code-expired': return t('The creation code this list was created with has expired.')
    case 'list-full': return t('The server has no room for another list.')
    case 'list-retired': return t('This list was replaced or deleted. Stop on this browser, then use its new sync link.')
    case 'password-taken': return t('That username and password are already in use. Choose another password.')
    case 'wrong-password': return t('That username and password don’t open a list on this server.')
    case 'network': return t('Couldn’t reach the server; try again when online.')
    case null: return ''
    default: return t('The server refused the list.')
  }
}
const problem = (e: unknown) => explain(e instanceof ListError ? e.message : 'network')

/** Run a step that stores something on the server, asking for a creation code if it needs one. */
async function withCode<T>(step: () => Promise<T>): Promise<T | null> {
  try { return await step() }
  catch (e) {
    if (!(e instanceof ListError && e.message === 'needs-code')) throw e
    if (!await askCode(t('This server keeps project lists only for people with a creation code from its owner.'))) return null
    return step()
  }
}

// ---- passkeys ------------------------------------------------------------------------------
const passkeySupported = () => 'credentials' in navigator && 'PublicKeyCredential' in window

class PasskeyError extends Error {}

function prfOf(cred: PublicKeyCredential): Uint8Array | null {
  const first = (cred.getClientExtensionResults() as { prf?: { results?: { first?: BufferSource } } }).prf?.results?.first
  return first ? new Uint8Array(first as ArrayBuffer) : null
}

const challenge = () => crypto.getRandomValues(new Uint8Array(32))
const prfRequest = { prf: { eval: { first: PASSKEY_SALT } } } as AuthenticationExtensionsClientInputs

async function createPasskey(): Promise<Uint8Array | null> {
  let cred: PublicKeyCredential | null
  try {
    cred = await navigator.credentials.create({ publicKey: {
      rp: { name: 'TeXLoom' },
      user: { id: crypto.getRandomValues(new Uint8Array(16)), name: 'TeXLoom project list', displayName: 'TeXLoom project list' },
      challenge: challenge(),
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }, { type: 'public-key', alg: -257 }],
      authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
      extensions: prfRequest,
    } }) as PublicKeyCredential | null
  } catch (e) {
    if ((e as Error).name === 'NotAllowedError') return null
    throw new PasskeyError(String(e))
  }
  if (!cred) return null
  const made = prfOf(cred)
  if (made) return made
  if (!(cred.getClientExtensionResults() as { prf?: { enabled?: boolean } }).prf?.enabled) throw new PasskeyError(t('This browser or passkey can’t encrypt with a passkey (it needs the “PRF” feature).'))
  return getPasskey([cred.rawId]) // many passkeys only give their secret when used, not when made
}

async function getPasskey(only: BufferSource[] = []): Promise<Uint8Array | null> {
  let cred: PublicKeyCredential | null
  try {
    cred = await navigator.credentials.get({ publicKey: {
      challenge: challenge(),
      userVerification: 'required',
      allowCredentials: only.map((id) => ({ type: 'public-key', id })),
      extensions: prfRequest,
    } }) as PublicKeyCredential | null
  } catch (e) {
    if ((e as Error).name === 'NotAllowedError') return null
    throw new PasskeyError(String(e))
  }
  if (!cred) return null
  const out = prfOf(cred)
  if (!out) throw new PasskeyError(t('This browser or passkey can’t encrypt with a passkey (it needs the “PRF” feature).'))
  return out
}

// ---- other ways in, and removing them -------------------------------------------------------
interface Slot { kind: 'passkey' | 'password'; at: number; seed: string }
type Registry = { v: 1; slots: Record<string, Slot> }

const registryKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list slots'))

async function readRegistry(secret: Uint8Array): Promise<Registry> {
  const had = await pull(await registryKey(secret))
  return had ? JSON.parse(dec.decode(had.plain)) as Registry : { v: 1, slots: {} }
}

async function updateRegistry(secret: Uint8Array, change: (r: Registry) => void) {
  const key = await registryKey(secret)
  for (let i = 0; i < 5; i++) {
    const had = await pull(key)
    const doc: Registry = had ? JSON.parse(dec.decode(had.plain)) : { v: 1, slots: {} }
    change(doc)
    if (await push(key, (had?.seq ?? 0) + 1, enc.encode(canon(doc)))) return
  }
  throw new ListError('busy')
}

/** Delete an entry, keeping its id out of use, given the seed of its signing key. */
async function retire(id: string, seed: string) {
  const sig = b64e(ed25519.sign(enc.encode(`listdel:${id}`), b64d(seed)))
  const r = await fetch(`/api/list/${id}`, { method: 'DELETE', headers: { 'X-Sig': sig } })
  if (!r.ok) throw new ListError('server')
}

/** Make `unlocking` a way into the list. */
async function addSlot(secret: Uint8Array, unlocking: Uint8Array, kind: Slot['kind']): Promise<string> {
  const key = await ListKey.of(unlocking)
  const plain = enc.encode(JSON.stringify({ v: 1, wrap: b64e(secret) }))
  await withCode(async () => {
    const had = await pull(key)
    if (had && kind === 'password') throw new ListError('password-taken')
    if (!await push(key, (had?.seq ?? 0) + 1, plain)) throw new ListError('busy')
    await updateRegistry(secret, (r) => { r.slots[key.id] = { kind, at: Date.now(), seed: key.signingSeed } })
  })
  return key.id
}

async function removeSlot(secret: Uint8Array, id: string, slot: Slot) {
  await retire(id, slot.seed)
  await updateRegistry(secret, (r) => { delete r.slots[id] })
}

/** Remove every way in, and the record of them. */
async function removeAllSlots(secret: Uint8Array) {
  const { slots } = await readRegistry(secret)
  for (const [id, slot] of Object.entries(slots)) await retire(id, slot.seed)
  const key = await registryKey(secret)
  await retire(key.id, key.signingSeed)
}

/** Let a passkey open the list. */
async function addPasskey(secret: Uint8Array): Promise<boolean> {
  const unlocking = await createPasskey()
  if (!unlocking) return false
  await addSlot(secret, unlocking, 'passkey')
  return true
}

const MIN_PASSWORD = 12

/** What a username and password stand for. Slow on purpose, and done on this device: neither is
 *  ever sent anywhere. Must match texloom/listsync.py. */
const passwordSecret = (username: string, password: string) => scryptAsync(
  enc.encode(password.normalize('NFKC')),
  enc.encode(`texloom list password v1:${username.trim().toLowerCase().normalize('NFKC')}`),
  { N: 2 ** 17, r: 8, p: 1, dkLen: 32, maxmem: 256 * 1024 * 1024 + 1024 },
)

/** Set the list's one password, replacing any earlier one. */
async function setPassword(secret: Uint8Array, username: string, password: string) {
  const fresh = await addSlot(secret, await passwordSecret(username, password), 'password')
  const { slots } = await readRegistry(secret)
  for (const [id, slot] of Object.entries(slots)) if (slot.kind === 'password' && id !== fresh) await removeSlot(secret, id, slot)
}

async function unlockWithPassword(username: string, password: string): Promise<Uint8Array> {
  const had = await pull(await ListKey.of(await passwordSecret(username, password))).catch(() => null) // removed, or wrong
  if (!had) throw new ListError('wrong-password')
  return b64d(JSON.parse(dec.decode(had.plain)).wrap)
}

/** Move the list to a new secret, for when the link has got out. Everything that opened the old
 *  one stops working, including passkeys and passwords: whoever has the old link could read them. */
async function replaceList(secret: Uint8Array) {
  await syncNow()
  const fresh = newSecret()
  const key = await ListKey.of(fresh)
  await withCode(() => exchange(key, projects.asList()))
  await removeAllSlots(secret)
  const old = await ListKey.of(secret)
  await retire(old.id, old.signingSeed)
  saveSecret(fresh)
  lastSynced = ''
  lastError = null
  await syncNow()
}

async function deleteList(secret: Uint8Array) {
  await removeAllSlots(secret)
  const key = await ListKey.of(secret)
  await retire(key.id, key.signingSeed)
  saveSecret(null)
  lastSynced = ''
  lastError = null
}

/** The list's secret, from a passkey added earlier; null if the person cancelled. */
async function unlockWithPasskey(): Promise<Uint8Array | null> {
  const unlocking = await getPasskey()
  if (!unlocking) return null
  const had = await pull(await ListKey.of(unlocking)).catch(() => null) // removed, or never added here
  if (!had) throw new PasskeyError(t('That passkey doesn’t open a list on this server.'))
  return b64d(JSON.parse(dec.decode(had.plain)).wrap)
}

// ---- the dialog ----------------------------------------------------------------------------
const ADD_WARNING = () => t('The projects in this browser will be added to this list, and anyone who has its link can open them. Use it only if the link is yours.')
const NO_RECOVERY = () => t('There is no way to recover a lost list. The server can’t read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren’t deleted, but you’ll need each project’s own link to open it.')
const COPY_WARNING = () => t('Keep this link somewhere safe, such as a password manager: it opens every project on the list.')

async function adopt(secret: Uint8Array) {
  saveSecret(secret)
  lastSynced = ''
  await syncNow()
}

/** Ask for a username and password; `setting` also confirms it and explains the limits. */
function askPassword(setting: boolean): Promise<{ username: string; password: string } | null> {
  return new Promise((resolve) => {
    const username = h('input', { autocomplete: 'username', spellcheck: 'false' }) as HTMLInputElement
    const password = h('input', { type: 'password', autocomplete: setting ? 'new-password' : 'current-password' }) as HTMLInputElement
    const again = h('input', { type: 'password', autocomplete: 'new-password' }) as HTMLInputElement
    const msg = h('p', { class: 'err small', role: 'status' })
    let out: { username: string; password: string } | null = null
    const submit = () => {
      if (!username.value.trim() || !password.value) return
      if (setting && password.value.length < MIN_PASSWORD) { msg.textContent = t('Use at least {{n}} characters.', { n: MIN_PASSWORD }); return }
      if (setting && password.value !== again.value) { msg.textContent = t('The two passwords don’t match.'); return }
      out = { username: username.value, password: password.value }
      dlg.close()
    }
    const form = h('form', { onsubmit: (e: Event) => { e.preventDefault(); submit() } },
      h('h3', {}, setting ? t('Set a password') : t('Unlock with a password')),
      h('p', { class: 'muted small' }, t('Your username and password stay on this device. Only a value worked out from them reaches the server, so it never learns either one.')),
      setting ? h('p', { class: 'recovery-warning', role: 'note' }, t('A forgotten password can’t be reset, by anyone. You’d need your sync link or a passkey to set a new one. Someone who got hold of the server’s data could try to guess it, so use a long password that you don’t use anywhere else.')) : '',
      h('label', { class: 'field' }, t('Username or email (not checked, and never stored)'), username),
      h('label', { class: 'field' }, t('Password'), password),
      setting ? h('label', { class: 'field' }, t('Password again'), again) : '',
      msg,
      h('div', { class: 'row end' }, h('button', { type: 'button', onclick: () => dlg.close() }, t('Cancel')), h('button', { class: 'primary', type: 'submit' }, t('OK'))),
    )
    const dlg = dialog('settings', form)
    dlg.addEventListener('close', () => resolve(out))
    username.focus()
  })
}

const whenAdded = (at: number) => new Date(at).toLocaleDateString(locale, { day: 'numeric', month: 'short', year: 'numeric' })

export function listDialog() {
  const body = h('div', {})
  const msg = h('p', { class: 'err small', role: 'status' })
  const dlg = dialog('settings', h('h3', {}, t('Project list')), body, msg, h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Close'))))

  const act = (f: () => Promise<unknown>) => async () => {
    msg.textContent = ''
    try { await f() } catch (e) { msg.textContent = e instanceof PasskeyError ? e.message : problem(e) }
    render()
    refreshWays()
  }

  const ways = h('div', { class: 'list-ways' })
  async function refreshWays() {
    const secret = savedSecret()
    ways.replaceChildren()
    if (!secret) return
    const slots = await readRegistry(secret).then((r) => r.slots, () => ({} as Registry['slots']))
    const rows = Object.entries(slots).sort((a, b) => a[1].at - b[1].at).map(([id, slot]) => h('div', { class: 'row' },
      h('span', { class: 'spacer' }, slot.kind === 'passkey' ? t('Passkey, added {{when}}', { when: whenAdded(slot.at) }) : t('Password, set {{when}}', { when: whenAdded(slot.at) })),
      h('button', { onclick: act(async () => {
        if (!confirm(slot.kind === 'passkey' ? t('Stop this passkey opening the list? You can also delete it from your password manager.') : t('Remove this password?'))) return
        await removeSlot(secret, id, slot)
      }) }, t('Remove'))))
    ways.replaceChildren(...(rows.length ? [h('h5', {}, t('Other ways to open this list')), ...rows] : []))
  }

  function render() {
    const secret = savedSecret()
    const passkey = passkeySupported()
    if (secret) {
      const count = projects.list().length
      body.replaceChildren(
        h('p', {}, tn(count, 'This browser keeps its list of {{n}} project on the server, encrypted. The server can’t read it.', 'This browser keeps its list of {{n}} projects on the server, encrypted. The server can’t read it.')),
        lastError ? h('p', { class: 'err small' }, explain(lastError)) : '',
        h('p', { class: 'recovery-warning', role: 'note' }, NO_RECOVERY()),
        h('p', { class: 'muted small' }, COPY_WARNING()),
        h('div', { class: 'row' },
          h('button', { onclick: act(syncNow) }, t('Sync now')),
          h('button', { onclick: (e: any) => { navigator.clipboard.writeText(linkFor(secret)); e.target.textContent = t('Copied') } }, t('Copy sync link')),
          passkey ? h('button', { onclick: act(async () => { if (await addPasskey(secret)) msg.textContent = t('Passkey added. Keep the sync link as well: it still opens the list if you lose this passkey.') }) }, t('Add a passkey')) : '',
          h('button', { onclick: act(async () => {
            const asked = await askPassword(true)
            if (!asked) return
            msg.textContent = t('Working…')
            await setPassword(secret, asked.username, asked.password)
            msg.textContent = t('Password set.')
          }) }, t('Set a password')),
          h('button', { onclick: () => { saveSecret(null); lastSynced = ''; lastError = null; render(); refreshWays() } }, t('Stop on this browser')),
        ),
        ways,
        h('h5', {}, t('If the link has got out, or you’re finished with the list')),
        h('div', { class: 'row' },
          h('button', { onclick: act(async () => {
            if (!confirm(t('Replace the sync link? The old link, and every passkey and password, will stop working. Other devices will need the new link.'))) return
            await replaceList(secret)
            msg.textContent = t('Done. Copy the new sync link and keep it safe. Add any passkeys and password again.')
          }) }, t('Replace the sync link…')),
          h('button', { class: 'danger', onclick: act(async () => {
            if (!confirm(t('Delete this list from the server, for every device? Your projects aren’t deleted, and this browser keeps its own list.'))) return
            await deleteList(secret)
          }) }, t('Delete this list…')),
        ),
      )
      return
    }
    const input = h('input', { type: 'password', placeholder: t('Sync link'), 'aria-label': t('Sync link'), autocomplete: 'off' }) as HTMLInputElement
    body.replaceChildren(
      h('p', {}, t('Keep the list of your projects on the server, encrypted, so it follows you to other computers and browsers. The server can’t read it.')),
      h('p', { class: 'muted small' }, t('It is opened by a sync link, and optionally by a passkey.')),
      h('p', { class: 'recovery-warning', role: 'note' }, NO_RECOVERY()),
      h('div', { class: 'row' },
        h('button', { class: 'primary', onclick: act(async () => {
          const fresh = newSecret()
          let done: boolean | null = null
          try { done = await withCode(() => adopt(fresh).then(() => true)) }
          finally { if (!done) { saveSecret(null); lastError = null } }
          if (done) msg.textContent = t('List started. Copy its sync link and keep it safe.')
        }) }, t('Start a new list')),
        passkey ? h('button', { onclick: act(async () => {
          const unlocked = await unlockWithPasskey()
          if (unlocked && confirm(ADD_WARNING())) await adopt(unlocked)
        }) }, t('Unlock with a passkey')) : '',
        h('button', { onclick: act(async () => {
          const asked = await askPassword(false)
          if (!asked) return
          msg.textContent = t('Working…')
          const unlocked = await unlockWithPassword(asked.username, asked.password)
          msg.textContent = ''
          if (confirm(ADD_WARNING())) await adopt(unlocked)
        }) }, t('Unlock with a password')),
      ),
      h('div', { class: 'row' }, input, h('button', { onclick: act(async () => {
        const secret = secretFromLink(input.value)
        if (typeof secret === 'string') { msg.textContent = secret; return }
        if (confirm(ADD_WARNING())) await adopt(secret)
      }) }, t('Use this link'))),
    )
  }

  render()
  refreshWays()
  fetch('/api/info').then((r) => r.json()).then((i) => { if (i.projectLists === false) { body.replaceChildren(h('p', {}, t('This server doesn’t keep project lists.'))) } }).catch(() => {})
}

function secretFromLink(link: string): Uint8Array | string {
  try {
    const u = new URL(link.trim())
    const v = new URLSearchParams(u.hash.slice(1)).get('sync')
    const secret = v ? b64d(v) : null
    if (secret?.length !== 32) return t('That isn’t a sync link.')
    if (u.origin !== location.origin) return t('That list is kept on {{server}}: open the link there.', { server: u.origin })
    return secret
  } catch { return t('That isn’t a sync link.') }
}

// ---- links that open this page -------------------------------------------------------------
type ListHash = { kind: 'sync'; link: string } | { kind: 'app' }

/** A #sync=… or #unlock-for-app address, taken out of the address bar; null for any other. */
export function takeListHash(): ListHash | null {
  const hash = location.hash
  if (hash !== '#unlock-for-app' && !hash.startsWith('#sync=')) return null
  history.replaceState(null, '', location.pathname + location.search)
  return hash === '#unlock-for-app' ? { kind: 'app' } : { kind: 'sync', link: location.href + hash }
}

export function handleListHash(what: ListHash) {
  if (what.kind === 'app') { sendToAppDialog(); return }
  const secret = secretFromLink(what.link)
  if (typeof secret === 'string') { alert(secret); return }
  if (savedSecret() && b64e(savedSecret()!) === b64e(secret)) return
  if (confirm(ADD_WARNING())) adopt(secret).catch((e) => alert(problem(e)))
}

/** The desktop app can't use a passkey itself: unlock here, then hand it the link. */
function sendToAppDialog() {
  const msg = h('p', { class: 'err small', role: 'status' })
  const send = (secret: Uint8Array) => { location.href = `texloom://sync?link=${encodeURIComponent(linkFor(secret))}`; dlg.close() }
  const dlg = dialog('settings',
    h('h3', {}, t('Project list')),
    h('p', {}, t('The TeXLoom app on this computer asked for your project list. Continue only if you just asked for this in the app.')),
    msg,
    h('div', { class: 'row end' },
      h('button', { onclick: () => dlg.close() }, t('Cancel')),
      passkeySupported() ? h('button', { class: 'primary', onclick: async () => {
        msg.textContent = ''
        try { const s = await unlockWithPasskey(); if (s) send(s) }
        catch (e) { msg.textContent = e instanceof PasskeyError ? e.message : problem(e) }
      } }, t('Unlock with a passkey')) : '',
    ),
  )
}
