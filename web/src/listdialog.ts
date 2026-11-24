// The project list's dialogs, and the links that open them. The list itself is in listsync.ts.
import { b64d, b64e } from './crypto'
import { dialog, h } from './dom'
import { locale, t, tn } from './i18n'
import {
  addSlot, adopt, copyProblems, copyServers, createPasskey, deleteList, lastProblem, linkFor, ListError, MIN_PASSWORD, newSecret, passkeySupported, readSlots,
  removeSlot, replaceList, savedSecret, saveSecret, setCopy, setPassword, syncNow, unlockWithPasskey, unlockWithPassword, type Slot,
} from './listsync'
import { projects } from './projects'
import { creationCode } from './replica'
import { normalServer, serverName } from './servers'
import { askCode } from './storagenotice'
import { serverInfo } from './servers'
import { sendListToApp } from './openinapp'

function explain(code: string | null): string {
  switch (code) {
    case null: return ''
    case 'needs-code': return t('This server needs a creation code to store a list.')
    case 'code-expired': return t('The creation code this list was created with has expired.')
    case 'list-full': return t('The server has no room for another list.')
    case 'list-retired': return t('This list was replaced or deleted. Stop on this browser, then use its new sync link.')
    case 'password-taken': return t('That username and password are already in use. Choose another password.')
    case 'wrong-password': return t('That username and password don’t open a list on this server.')
    case 'no-prf': return t('This browser or passkey can’t encrypt with a passkey (it needs the “PRF” feature).')
    case 'passkey-failed': return t('The passkey didn’t work. Try again.')
    case 'network': return t('Couldn’t reach the server; try again when online.')
    case 'update-needed': return t('This server needs a newer version of TeXLoom. Reload the page to get it.')
    default: return t('The server refused the list.')
  }
}
const problem = (e: unknown) => explain(e instanceof ListError ? e.message : 'network')

/** Run a step that stores something on the server, asking for a creation code if it needs one. */
async function withCode<T>(step: () => Promise<T>): Promise<T | null> {
  try { return await step() }
  catch (e) {
    if (!(e instanceof ListError && e.message === 'needs-code')) throw e
    if (!await askCode(CODE_FOR_LIST())) return null
    return step()
  }
}

const CODE_FOR_LIST = () => t('This server keeps project lists only for people with a creation code from its owner.')

/** Ask for a needed creation code up front, before making something that can't be taken back. */
async function haveCode(): Promise<boolean> {
  return (await serverInfo())?.creation !== 'code' || !!creationCode.get() || askCode(CODE_FOR_LIST())
}

/** Start a new list, first giving it a way in if there is one, so that a refused password leaves
 *  nothing behind. False if it wasn't started. */
async function startList(addWay?: (secret: Uint8Array) => Promise<unknown>): Promise<boolean> {
  const fresh = newSecret()
  if (addWay && !await withCode(() => addWay(fresh).then(() => true))) return false
  let done: boolean | null = null
  try { done = await withCode(() => adopt(fresh).then(() => true)) }
  finally { if (!done) saveSecret(null) }
  return !!done
}

const NO_RECOVERY = () => t('There is no way to recover a lost list. The server can’t read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren’t deleted, but you’ll need each project’s own link to open it.')
const ADD_WARNING = () => t('The projects in this browser will be added to this list, and anyone who has its link can open them. Use it only if the link is yours.')
const whenAdded = (at: number) => new Date(at).toLocaleDateString(locale, { day: 'numeric', month: 'short', year: 'numeric' })

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
      if (setting && new Set(password.value).size < 6) { msg.textContent = t('That password is too easy to guess.'); return }
      if (setting && password.value !== again.value) { msg.textContent = t('The two passwords don’t match.'); return }
      out = { username: username.value, password: password.value }
      dlg.close()
    }
    const form = h('form', { onsubmit: (e: Event) => { e.preventDefault(); submit() } },
      h('h3', {}, setting ? t('Set a password') : t('Unlock with a password')),
      h('p', { class: 'muted small' }, t('Your username and password stay on this device. Only a value worked out from them reaches the server, so it never learns either one.')),
      setting ? h('p', { class: 'recovery-warning', role: 'note' }, t('A forgotten password can’t be reset, by anyone. You’d need your sync link or a passkey to set a new one. Someone who got hold of the server’s data could try to guess it, so use a long password that you don’t use anywhere else.')) : '',
      h('label', { class: 'field' }, t('Username or email'), username),
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

export function listDialog(onClose?: () => void) {
  const title = h('h3', {})
  const body = h('div', { class: 'listsync' })
  const ways = h('div', {})
  const copies = h('div', {})
  const msg = h('p', { class: 'small', role: 'status' })
  const dlg = dialog('settings', title, body, msg, h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Close'))))
  if (onClose) dlg.addEventListener('close', onClose)
  /** A message under the dialog: a problem (red) or, with `ok`, news (green). */
  const say = (text: string, ok = false) => { msg.textContent = text; msg.className = `small ${ok ? 'ok' : 'err'}` }
  const done = (text: string) => say(text, true)

  /** A button whose work may fail, after which the dialog shows the new state. */
  const btn = (label: string, tip: string, work: () => Promise<unknown>, attrs: Record<string, string> = {}) => h('button', { ...attrs, title: tip, onclick: async () => {
    say('')
    try { await work() } catch (e) { say(problem(e)) }
    render()
  } }, label)
  const removeBtn = (secret: Uint8Array, id: string, slot: Slot) => btn(t('Remove'), slot.kind === 'passkey' ? t('Stop this passkey opening the list') : t('Stop this password opening the list'), async () => {
    if (confirm(slot.kind === 'passkey' ? t('Stop this passkey opening the list? You can also delete it from your password manager.') : t('Remove this password?'))) await removeSlot(secret, id, slot)
  })
  const section = (heading: string, ...kids: (Node | string)[]) => h('section', { class: 'listsync-step' }, h('h5', {}, heading), ...kids)
  const warning = () => h('p', { class: 'recovery-warning', role: 'note', title: NO_RECOVERY() }, t('Nobody can recover a lost list, not even the server’s owner: keep the sync link safe.'))

  async function showWays(secret: Uint8Array) {
    const slots = await readSlots(secret).catch(() => ({} as Record<string, Slot>))
    const rows = Object.entries(slots).sort((a, b) => a[1].at - b[1].at).map(([id, slot]) => h('div', { class: 'row' },
      h('span', { class: 'spacer' }, slot.kind === 'passkey' ? t('Passkey, added {{when}}', { when: whenAdded(slot.at) }) : t('Password, set {{when}}', { when: whenAdded(slot.at) })),
      removeBtn(secret, id, slot)))
    ways.replaceChildren(...(rows.length ? rows : [h('p', { class: 'muted small' }, t('None yet: only the sync link opens it.'))]))
  }

  /** The servers keeping a copy, and others this browser knows, to tick. */
  function showCopies() {
    const on = new Set(copyServers())
    const known = [...new Set([...on, location.origin, ...projects.list().map((r) => r.server ?? ''), ...creationCode.servers()].map((x) => normalServer(x)).filter((x): x is string => !!x))]
    const toggle = (server: string, box: HTMLInputElement) => async () => {
      say('')
      if (!box.checked && on.size === 1) { box.checked = true; say(t('The list needs at least one copy.')); return }
      try {
        await setCopy(server, box.checked)
        if (box.checked && copyProblems.get(server) === 'needs-code' && await askCode(CODE_FOR_LIST(), server)) await syncNow()
      } catch (e) { say(problem(e)) }
      showCopies()
    }
    const row = (server: string) => {
      const box = h('input', { type: 'checkbox', checked: on.has(server) }) as HTMLInputElement
      box.addEventListener('change', toggle(server, box))
      const trouble = on.has(server) ? copyProblems.get(server) : undefined
      return h('label', { class: 'check', title: t('Keep an encrypted copy of the list on {{server}}', { server: serverName(server) }) }, box, h('span', {}, serverName(server),
        server === location.origin ? h('small', { class: 'muted' }, ' · ' + t('this server')) : '',
        trouble ? h('small', { class: 'err' }, ' · ' + (trouble === 'network' ? t('not reached at the last sync') : explain(trouble))) : ''))
    }
    const other = h('input', { placeholder: 'https://…', 'aria-label': t('Another server’s address') }) as HTMLInputElement
    copies.replaceChildren(
      h('h5', {}, t('Copies on other TeXLoom servers')),
      h('p', { class: 'muted small' }, t('Your list is stored, encrypted, on this server. To keep it safe if this server ever goes away, you can keep copies on other TeXLoom servers too: tick one below, or type its address (such as https://texloom.example.org). Each server decides whether to take a copy; some ask for one of their creation codes. Passkeys only work on the server where they were added.')),
      ...known.map(row),
      h('div', { class: 'row' }, other, btn(t('Add'), t('Keep a copy of the list on that server too'), async () => {
        const server = normalServer(other.value.trim())
        if (!server) { say(t('Give a server’s address, starting with https://')); return }
        await setCopy(server, true)
        if (copyProblems.get(server) === 'needs-code' && await askCode(CODE_FOR_LIST(), server)) await syncNow()
      })),
    )
  }

  function renderOpen(secret: Uint8Array) {
    const count = projects.list().length
    title.textContent = t('Project list sync')
    const copyBtn = h('button', { class: 'primary', title: t('Copy the link, to paste into another browser or a password manager'), onclick: () => {
      navigator.clipboard.writeText(linkFor(secret)).then(() => done(t('Sync link copied.')), () => say(t('Couldn’t copy: your browser didn’t allow it.')))
    } }, t('Copy sync link'))
    body.replaceChildren(
      h('div', { class: 'row listsync-status' },
        h('span', { class: 'spacer' }, '✓ ', count ? tn(count, 'Your {{n}} project is synced, encrypted.', 'Your {{n}} projects are synced, encrypted.') : t('Your list is synced, encrypted.')),
        btn(t('Sync now'), t('Send and fetch changes to the list now. It also happens by itself.'), syncNow, { class: 'small' })),
      lastProblem() ? h('p', { class: 'err small' }, explain(lastProblem())) : '',
      section(t('Your sync link'),
        h('p', { class: 'muted small' }, t('It opens this list on any computer or browser. Save it in a password manager.')),
        h('div', { class: 'row' }, copyBtn,
          h('button', { title: t('Use this list, and your creation code, in the TeXLoom app on this computer'), onclick: () => sendListToApp(linkFor(secret)) }, t('Use in the TeXLoom app'))),
        warning()),
      section(t('Other ways to open it'),
        ways,
        h('div', { class: 'row' },
          passkeySupported() ? btn(t('Add a passkey'), t('Open the list with Touch ID, Windows Hello, your phone or a security key, on this server'), async () => {
            const unlocking = await createPasskey()
            if (unlocking && await withCode(() => addSlot(secret, unlocking, 'passkey'))) done(t('Passkey added. Keep the sync link as well: it still opens the list if you lose this passkey.'))
          }) : '',
          btn(t('Set a password'), t('Open the list with a username and password of your choice. Nobody can reset it if you forget it.'), async () => {
            const asked = await askPassword(true)
            if (!asked) return
            say(t('Working…'), true)
            await withCode(() => setPassword(secret, asked.username, asked.password).then(() => done(t('Password set.'))))
          }))),
      h('details', {},
        h('summary', {}, t('More options')),
        copies,
        h('h5', {}, t('Stop or start over')),
        h('div', { class: 'row' },
          h('button', { title: t('This browser keeps its projects but stops syncing the list. Other devices carry on.'), onclick: () => { say(''); saveSecret(null); render() } }, t('Stop on this browser')),
          btn(t('Replace the sync link…'), t('If the link has got out: a new link, and the old one stops working everywhere'), async () => {
            if (!confirm(t('Replace the sync link? The old link, and every passkey and password, will stop working. Other devices will need the new link.'))) return
            await withCode(() => replaceList(secret).then(() => done(t('Done. Copy the new sync link and keep it safe. Add any passkeys and password again.'))))
          }),
          btn(t('Delete this list…'), t('Delete the list from the server for every device. Projects aren’t deleted.'), async () => {
            if (confirm(t('Delete this list from the server, for every device? Your projects aren’t deleted, and this browser keeps its own list.'))) await deleteList(secret)
          }, { class: 'danger' }))),
    )
    showWays(secret)
    showCopies()
  }

  function renderClosed() {
    title.textContent = t('Sync your project list')
    ways.replaceChildren()
    const input = h('input', { type: 'password', placeholder: t('Paste a sync link'), 'aria-label': t('Sync link'), autocomplete: 'off' }) as HTMLInputElement
    const useIfMine = async (secret: Uint8Array, server?: string) => { if (confirm(ADD_WARNING())) await adopt(secret, server) }
    const PASSKEY_TIP = t('Touch ID, Windows Hello, your phone or a security key. Works on this server only.')
    const PASSWORD_TIP = t('A username and password of your choice. Nobody can reset it if you forget it.')
    body.replaceChildren(
      h('p', {}, t('See the same projects on all your computers and browsers. The list is encrypted: the server can’t read it.')),
      section(t('First time? Start syncing'),
        h('p', { class: 'muted small' }, t('Choose how you’ll open the list elsewhere. You also get a sync link, which always works.')),
        h('div', { class: 'row' },
          passkeySupported() ? btn(t('With a passkey'), PASSKEY_TIP, async () => {
            if (!await haveCode()) return
            const unlocking = await createPasskey()
            if (unlocking && await startList((secret) => addSlot(secret, unlocking, 'passkey'))) done(t('Syncing. Now copy the sync link and keep it safe: it’s your way back in if you lose this passkey.'))
          }, { class: 'primary' }) : '',
          btn(t('With a password'), PASSWORD_TIP, async () => {
            if (!await haveCode()) return
            const asked = await askPassword(true)
            if (!asked) return
            say(t('Working…'), true)
            try {
              const started = await startList((secret) => setPassword(secret, asked.username, asked.password))
              if (started) done(t('Syncing. Now copy the sync link and keep it safe: it’s your way back in if you forget this password.'))
              else say('')
            } catch (e) {
              if (!(e instanceof ListError && e.message === 'password-taken')) throw e
              say(t('That username and password are already in use. If the list is yours, open it with “Password” below instead.'))
            }
          }, passkeySupported() ? {} : { class: 'primary' }),
          btn(t('With the link only'), t('Paste the sync link into each browser where you want the list.'), async () => {
            if (await startList()) done(t('Syncing. Now copy the sync link and keep it safe.'))
          })),
        warning()),
      section(t('Already syncing on another device?'),
        h('p', { class: 'muted small' }, t('Open the same list here with:')),
        h('div', { class: 'row' },
          passkeySupported() ? btn(t('Passkey'), PASSKEY_TIP, async () => {
            const unlocked = await unlockWithPasskey()
            if (unlocked === 'none') say(t('That passkey doesn’t open a list on this server.'))
            else if (unlocked) await useIfMine(unlocked)
          }) : '',
          btn(t('Password'), t('The username and password you set for the list'), async () => {
            const asked = await askPassword(false)
            if (!asked) return
            say(t('Working…'), true)
            const unlocked = await unlockWithPassword(asked.username, asked.password)
            say('')
            await useIfMine(unlocked)
          })),
        h('div', { class: 'row' }, input, btn(t('Open'), t('Open the list this sync link belongs to'), async () => {
          const got = secretFromLink(input.value)
          if (typeof got === 'string') say(got)
          else await useIfMine(got.secret, got.server)
        }))),
    )
  }

  function render() {
    const secret = savedSecret()
    if (secret) renderOpen(secret)
    else renderClosed()
  }

  render()
  serverInfo().then((i) => { if (i?.projectLists === false) body.replaceChildren(h('p', {}, t('This server doesn’t keep project lists.'))) })
}

/** A sync link's secret, and the server it points to (one keeping a copy: any works, so a link to
 *  another server's list is used from here as it is). */
function secretFromLink(link: string): { secret: Uint8Array; server: string } | string {
  try {
    const u = new URL(link.trim())
    const v = new URLSearchParams(u.hash.slice(1)).get('sync')
    const secret = v ? b64d(v) : null
    const server = normalServer(u.origin)
    if (secret?.length !== 32 || !server) return t('That isn’t a sync link.')
    return { secret, server }
  } catch { return t('That isn’t a sync link.') }
}

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
  const got = secretFromLink(what.link)
  if (typeof got === 'string') { alert(got); return }
  const mine = savedSecret()
  if (mine && b64e(mine) === b64e(got.secret)) return
  if (confirm(ADD_WARNING())) adopt(got.secret, got.server).catch((e) => alert(problem(e)))
}

/** The desktop app can't use a passkey itself: unlock here (or use the list this browser already
 *  has), then hand it the link. */
function sendToAppDialog() {
  const mine = savedSecret()
  const msg = h('p', { class: 'err small', role: 'status' })
  const dlg = dialog('settings',
    h('h3', {}, t('Project list')),
    h('p', {}, t('The TeXLoom app on this computer asked for your project list. Continue only if you just asked for this in the app.')),
    msg,
    h('div', { class: 'row end' },
      h('button', { onclick: () => dlg.close() }, t('Cancel')),
      mine ? h('button', { class: 'primary', onclick: () => { location.href = `texloom://sync?link=${encodeURIComponent(linkFor(mine))}`; dlg.close() } }, t('Send this browser’s list')) : '',
      passkeySupported() ? h('button', { class: mine ? '' : 'primary', onclick: async () => {
        msg.textContent = ''
        try {
          const secret = await unlockWithPasskey()
          if (secret === 'none') msg.textContent = t('That passkey doesn’t open a list on this server.')
          else if (secret) { location.href = `texloom://sync?link=${encodeURIComponent(linkFor(secret))}`; dlg.close() }
        } catch (e) { msg.textContent = problem(e) }
      } }, t('Unlock with a passkey')) : '',
    ),
  )
}
