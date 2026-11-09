// The project list's dialogs, and the links that open them. The list itself is in listsync.ts.
import { b64d, b64e } from './crypto'
import { dialog, h } from './dom'
import { locale, t, tn } from './i18n'
import {
  addSlot, adopt, createPasskey, deleteList, lastProblem, linkFor, ListError, MIN_PASSWORD, newSecret, passkeySupported, readSlots,
  removeSlot, replaceList, savedSecret, saveSecret, setPassword, syncNow, unlockWithPasskey, unlockWithPassword, type Slot,
} from './listsync'
import { projects } from './projects'
import { creationCode } from './replica'
import { askCode, serverInfo } from './storagenotice'

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
const COPY_WARNING = () => t('Keep this link somewhere safe, such as a password manager: it opens every project on the list.')
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

export function listDialog(onClose?: () => void) {
  const body = h('div', {})
  const ways = h('div', { class: 'list-ways' })
  const msg = h('p', { class: 'err small', role: 'status' })
  const dlg = dialog('settings', h('h3', {}, t('Project list')), body, msg, h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Close'))))
  if (onClose) dlg.addEventListener('close', onClose)
  const say = (text: string) => { msg.textContent = text }

  /** A button whose work may fail, after which the dialog shows the new state. */
  const btn = (label: string, work: () => Promise<unknown>, attrs: Record<string, string> = {}) => h('button', { ...attrs, onclick: async () => {
    say('')
    try { await work() } catch (e) { say(problem(e)) }
    render()
  } }, label)
  const removeBtn = (secret: Uint8Array, id: string, slot: Slot) => btn(t('Remove'), async () => {
    if (confirm(slot.kind === 'passkey' ? t('Stop this passkey opening the list? You can also delete it from your password manager.') : t('Remove this password?'))) await removeSlot(secret, id, slot)
  })

  async function showWays(secret: Uint8Array) {
    const slots = await readSlots(secret).catch(() => ({} as Record<string, Slot>))
    const rows = Object.entries(slots).sort((a, b) => a[1].at - b[1].at).map(([id, slot]) => h('div', { class: 'row' },
      h('span', { class: 'spacer' }, slot.kind === 'passkey' ? t('Passkey, added {{when}}', { when: whenAdded(slot.at) }) : t('Password, set {{when}}', { when: whenAdded(slot.at) })),
      removeBtn(secret, id, slot)))
    ways.replaceChildren(...(rows.length ? [h('h5', {}, t('Other ways to open this list')), ...rows] : []))
  }

  function renderOpen(secret: Uint8Array) {
    const count = projects.list().length
    body.replaceChildren(
      h('p', {}, tn(count, 'This browser keeps its list of {{n}} project on the server, encrypted. The server can’t read it.', 'This browser keeps its list of {{n}} projects on the server, encrypted. The server can’t read it.')),
      lastProblem() ? h('p', { class: 'err small' }, explain(lastProblem())) : '',
      h('p', { class: 'recovery-warning', role: 'note' }, NO_RECOVERY()),
      h('p', { class: 'muted small' }, COPY_WARNING()),
      h('div', { class: 'row' },
        btn(t('Sync now'), syncNow),
        h('button', { onclick: (e: any) => { navigator.clipboard.writeText(linkFor(secret)); e.target.textContent = t('Copied') } }, t('Copy sync link')),
        passkeySupported() ? btn(t('Add a passkey'), async () => {
          const unlocking = await createPasskey()
          if (unlocking && await withCode(() => addSlot(secret, unlocking, 'passkey'))) say(t('Passkey added. Keep the sync link as well: it still opens the list if you lose this passkey.'))
        }) : '',
        btn(t('Set a password'), async () => {
          const asked = await askPassword(true)
          if (!asked) return
          say(t('Working…'))
          await withCode(() => setPassword(secret, asked.username, asked.password).then(() => say(t('Password set.'))))
        }),
        h('button', { onclick: () => { saveSecret(null); render() } }, t('Stop on this browser')),
      ),
      ways,
      h('h5', {}, t('If the link has got out, or you’re finished with the list')),
      h('div', { class: 'row' },
        btn(t('Replace the sync link…'), async () => {
          if (!confirm(t('Replace the sync link? The old link, and every passkey and password, will stop working. Other devices will need the new link.'))) return
          await withCode(() => replaceList(secret).then(() => say(t('Done. Copy the new sync link and keep it safe. Add any passkeys and password again.'))))
        }),
        btn(t('Delete this list…'), async () => {
          if (confirm(t('Delete this list from the server, for every device? Your projects aren’t deleted, and this browser keeps its own list.'))) await deleteList(secret)
        }, { class: 'danger' }),
      ),
    )
    showWays(secret)
  }

  function renderClosed() {
    ways.replaceChildren()
    const input = h('input', { type: 'password', placeholder: t('Sync link'), 'aria-label': t('Sync link'), autocomplete: 'off' }) as HTMLInputElement
    const useIfMine = async (secret: Uint8Array) => { if (confirm(ADD_WARNING())) await adopt(secret) }
    body.replaceChildren(
      h('p', {}, t('Keep the list of your projects on the server, encrypted, so it follows you to other computers and browsers. The server can’t read it.')),
      h('p', { class: 'muted small' }, t('It is opened by a sync link, and optionally by a passkey or a password.')),
      h('p', { class: 'recovery-warning', role: 'note' }, NO_RECOVERY()),
      h('h5', {}, t('Start a new list')),
      h('div', { class: 'row' },
        passkeySupported() ? btn(t('Start with a passkey'), async () => {
          if (!await haveCode()) return
          const unlocking = await createPasskey()
          if (unlocking && await startList((secret) => addSlot(secret, unlocking, 'passkey'))) say(t('List started. Copy its sync link and keep it safe: it’s your way back in if you lose this passkey.'))
        }, { class: 'primary' }) : '',
        btn(t('Start with a password'), async () => {
          if (!await haveCode()) return
          const asked = await askPassword(true)
          if (!asked) return
          say(t('Working…'))
          try {
            const started = await startList((secret) => setPassword(secret, asked.username, asked.password))
            say(started ? t('List started. Copy its sync link and keep it safe: it’s your way back in if you forget this password.') : '')
          } catch (e) {
            if (!(e instanceof ListError && e.message === 'password-taken')) throw e
            say(t('That username and password are already in use. If the list is yours, use “Unlock with a password” instead.'))
          }
        }, passkeySupported() ? {} : { class: 'primary' }),
        btn(t('Sync link only'), async () => {
          if (await startList()) say(t('List started. Copy its sync link and keep it safe.'))
        }),
      ),
      h('h5', {}, t('Open a list you already have')),
      h('div', { class: 'row' },
        passkeySupported() ? btn(t('Unlock with a passkey'), async () => {
          const unlocked = await unlockWithPasskey()
          if (unlocked === 'none') say(t('That passkey doesn’t open a list on this server.'))
          else if (unlocked) await useIfMine(unlocked)
        }) : '',
        btn(t('Unlock with a password'), async () => {
          const asked = await askPassword(false)
          if (!asked) return
          say(t('Working…'))
          const unlocked = await unlockWithPassword(asked.username, asked.password)
          say('')
          await useIfMine(unlocked)
        }),
      ),
      h('div', { class: 'row' }, input, btn(t('Use this link'), async () => {
        const secret = secretFromLink(input.value)
        if (typeof secret === 'string') say(secret)
        else await useIfMine(secret)
      })),
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
  const mine = savedSecret()
  if (mine && b64e(mine) === b64e(secret)) return
  if (confirm(ADD_WARNING())) adopt(secret).catch((e) => alert(problem(e)))
}

/** The desktop app can't use a passkey itself: unlock here, then hand it the link. */
function sendToAppDialog() {
  const msg = h('p', { class: 'err small', role: 'status' })
  const dlg = dialog('settings',
    h('h3', {}, t('Project list')),
    h('p', {}, t('The TeXLoom app on this computer asked for your project list. Continue only if you just asked for this in the app.')),
    msg,
    h('div', { class: 'row end' },
      h('button', { onclick: () => dlg.close() }, t('Cancel')),
      passkeySupported() ? h('button', { class: 'primary', onclick: async () => {
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
