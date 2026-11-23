// Storage-related UI: creation codes, the "only in this browser" warning, and the
// new-project flow on servers that require a creation code.
import { creationCode, type Replica } from './replica'
import { dialog, h } from './dom'
import { t } from './i18n'
import { normalServer, serverName } from './servers'
import { quiet, STORAGE } from './quiet'
import { bytes } from './usage'

const isSafari = /^((?!chrome|android|crios|fxios).)*safari/i.test(navigator.userAgent)
export const SAFARI_NOTE = isSafari ? ' ' + t('Safari may also delete a site’s data after about 7 days without a visit.') : ''

/** What a server says about itself (this app's own by default). */
export async function serverInfo(server = location.origin): Promise<{ creation: 'open' | 'code'; projectMaxBytes: number; projectLists?: boolean; private?: boolean; codesUrl?: string } | null> {
  try { const r = await fetch(`${server}/api/info`); return r.ok ? await r.json() : null } catch { return null }
}
const checkCode = (server: string, code: string) => fetch(`${server}/api/code`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) })
  .then((x) => x.json()).catch(() => null)
/** "on other.org", for a server that isn't this app's own. */
const onServer = (server: string) => (server === location.origin ? '' : ' ' + t('(on {{server}})', { server: serverName(server) }))

/** Where codes come from: whoever runs the server, with its page about them if it has one.
 *  Never a fixed address: each server says for itself. */
export function codeSource(info: { codesUrl?: string } | null): HTMLElement {
  const url = typeof info?.codesUrl === 'string' && /^https?:\/\//i.test(info.codesUrl) ? info.codesUrl : null
  return h('span', { class: 'muted small' }, t('Codes come from whoever runs this server.'),
    ...(url ? [' ', h('a', { href: url, target: '_blank', rel: 'noopener' }, t('Where to get one'))] : []))
}

const PRIVATE_INTRO = () => t('This server is private: new projects need a creation code from its owner. You only need it once per browser; opening projects shared with you never needs one.')

/** On a private server, make sure this browser holds a valid creation code (asking if not).
 *  False means nothing new may be created here. Anywhere else, or offline, there's nothing to check. */
export async function privateServerAllows(server = location.origin): Promise<boolean> {
  if (!(await serverInfo(server))?.private) return true
  const code = creationCode.get(server)
  const r = code ? await checkCode(server, code) : null
  return r?.ok === true || askCode(PRIVATE_INTRO(), server)
}

/** Ask for a creation code, check it with the server, save it. Resolves true if a valid code was saved. */
export function askCode(intro?: string, server = location.origin): Promise<boolean> {
  return new Promise((resolve) => {
    const input = h('input', { value: creationCode.get(server), placeholder: t('e.g. 3PYW-WTHZ-PRCF'), autocomplete: 'off', 'aria-label': t('Creation code') }) as HTMLInputElement
    const msg = h('p', { class: 'err small' })
    let ok = false
    const check = async () => {
      const code = input.value.trim()
      if (!code) return
      const r = await checkCode(server, code)
      if (r?.ok) {
        creationCode.set(code, Date.now(), server); ok = true
        if (r.allowance != null) alert(t('Code accepted. Its storage allowance is {{total}} MB, of which {{used}} MB is used.', { total: (r.allowance / 1048576).toFixed(0), used: (r.used / 1048576).toFixed(1) }))
        dlg.close()
      }
      else msg.textContent = r ? t('That code isn’t valid on this server.') : t('Couldn’t reach the server; try again when online.')
    }
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') check() })
    const source = h('p', {}, codeSource(null))
    serverInfo(server).then((info) => source.replaceChildren(codeSource(info)))
    const dlg = dialog('settings',
      h('h3', {}, t('Creation code') + onServer(server)),
      h('p', { class: 'muted' }, intro ?? t('A creation code lets this server keep an encrypted copy of projects you create, so co-authors can catch up while you’re offline. You don’t need one to open or edit projects shared with you; without one, a new project is sync-only.')),
      source,
      h('label', { class: 'field' }, t('Code'), input),
      msg,
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), h('button', { class: 'primary', onclick: check }, t('Save code'))),
    )
    dlg.addEventListener('close', () => resolve(ok))
    input.focus()
  })
}

/** The start page's settings: this browser's creation code for this server (a code goes with the
 *  person rather than a project), what it allows, and a way to enter or change it. */
export function codeBox(): HTMLElement {
  const box = h('div', { class: 'codebox' })
  const shown = (code: string) => (code.length > 10 ? `${code.slice(0, 4)}…${code.slice(-4)}` : code)
  async function render() {
    const code = creationCode.get()
    const status = h('span', { class: 'muted small' })
    const source = h('span', {})
    box.replaceChildren(
      h('p', { class: 'muted small' }, t('Used when you create a project, so the server keeps an encrypted copy of it. It follows your project list to your other devices. To move a project you own onto it, open that project’s Settings → Sync & storage.')),
      code ? h('div', { class: 'row' }, h('code', { title: t('Your creation code') }, shown(code)), status) : status,
      h('div', { class: 'row' }, h('button', { onclick: async () => { if (await askCode()) render() } }, code ? t('Change code…') : t('Enter code…')), source))
    const info = await serverInfo()
    source.replaceChildren(codeSource(info))
    if (!code) {
      status.textContent = info?.creation === 'open' ? t('None entered. This server stores new projects without one.') : t('None entered: new projects are sync-only until you enter one.')
      return
    }
    const r = await checkCode(location.origin, code)
    status.textContent = !r ? t('Couldn’t reach the server.') : !r.ok ? t('No longer valid on this server.')
      : r.allowance == null ? t('Unlimited storage.') : t('{{used}} of {{total}} used.', { used: bytes(r.used ?? 0), total: bytes(r.allowance) })
  }
  render()
  return box
}

/** "Create project": on servers that need a code, ask for one or offer a project without a server copy. */
export async function chooseNewProject(server = location.origin): Promise<'server' | 'relay' | null> {
  if (!(await privateServerAllows(server))) return null
  const info = await serverInfo(server)
  if (!info || info.creation === 'open' || creationCode.get(server)) return 'server'
  return new Promise((resolve) => {
    let choice: 'server' | 'relay' | null = null
    const dlg = dialog('settings',
      h('h3', {}, t('Create project') + onServer(server)),
      h('p', {}, t('A creation code lets this server keep an encrypted copy of projects you create.'), ' ', codeSource(info)),
      h('div', { class: 'radios' },
        h('button', { class: 'radio choice', onclick: async () => { if (await askCode(undefined, server)) { choice = 'server'; dlg.close() } } },
          h('span', {}, h('b', {}, t('I have a code')), h('small', { class: 'muted' }, t('The server keeps an encrypted copy, so collaborators get your changes even when you’re offline, and the link always restores the project.')))),
        h('button', { class: 'radio choice', onclick: () => { choice = 'relay'; dlg.close() } },
          h('span', {}, h('b', {}, t('Continue without a server copy')), h('small', { class: 'muted' }, t('Changes reach collaborators only while you’re both online. The project lives in your browser (and theirs): clearing browser data deletes your copy.') + SAFARI_NOTE)))),
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel'))),
    )
    dlg.addEventListener('close', () => resolve(choice))
  })
}

/** Banner shown when this browser may hold the project's only copy. */
export function riskBanner(r: Replica, writable: boolean, openSettings: () => void) {
  const el = h('div', { class: 'risk', hidden: true, role: 'status' })
  let dismissed = false
  const render = () => {
    if (r.denied && !dismissed) {
      el.replaceChildren(
        h('span', {}, h('b', {}, '⚠ '), r.denied === 'update-needed'
          ? (r.server === location.origin
            ? t('Not syncing: this server needs a newer version of TeXLoom ({{version}} or later). Reload the page to get it.', { version: r.minVersion || '?' })
            : t('Not syncing: this project’s server needs a newer version of TeXLoom ({{version}} or later) than this server’s web app. Open the project from {{server}} instead.', { version: r.minVersion || '?', server: serverName(r.server) }))
          : r.denied === 'code-expired'
          ? t('Not syncing: this server is private, and the creation code this project was created with has expired or been removed. Renewing it restores access.')
          : t('Not syncing: this server is private, and this project needs a creation code from its owner.'),
          ' ' + t('Your copy here is safe and you can keep working, but nobody else gets your changes.')),
        h('span', { class: 'actions' },
          r.denied === 'needs-code' ? h('button', { class: 'primary', onclick: async () => { if (await askCode(PRIVATE_INTRO(), r.server)) r.reconnect() } }, t('Enter code')) : '',
          r.denied === 'update-needed' && r.server === location.origin ? h('button', { class: 'primary', onclick: () => location.reload() }, t('Reload')) : '',
          h('button', { title: t('Hide until next time'), 'aria-label': t('Hide this warning'), onclick: () => { dismissed = true; render() } }, '✕')))
      el.hidden = false
      return
    }
    const relay = r.storage === 'relay'
    const p = relay ? null : r.storeProblem // relay-only projects aren't meant to be stored
    if (!writable || dismissed || (!p && !relay)) { el.hidden = true; return }
    const MB = Math.round((r.storeInfo?.projectMax ?? 0) / 1048576)
    const why =
      p === 'needs-code' ? t('Not saved on the server: storing new projects here needs a creation code.')
      : p === 'code-expired' ? t('Not saved on the server: the creation code this project was created with has expired. Renewing it restores storage.')
      : p === 'quota-full' ? t('Not saved on the server: the storage allowance of the creation code this project was created with is used up.')
      : p === 'project-full' ? t('Not saved on the server: this project has reached the server’s {{mb}} MB limit.', { mb: MB })
      : p === 'server-full' ? t('Not saved on the server: the server is out of space. Changes are kept here and retried every few minutes.')
      : t('This project isn’t stored on the server (Settings → Server storage).')
    el.replaceChildren(
      h('span', {}, h('b', {}, '⚠ '), why,
        ' ' + t('Its copies are in this browser and with collaborators: clearing this browser’s data deletes yours.') + SAFARI_NOTE + ' ' + t('Keep a folder copy to be safe.')),
      h('span', { class: 'actions' },
        p === 'needs-code' ? h('button', { class: 'primary', onclick: async () => { if (await askCode()) r.retryStore() } }, t('Enter code')) : '',
        h('button', { onclick: openSettings }, t('Folder copy…')),
        h('button', { title: t('Hide until next time'), 'aria-label': t('Hide this warning'), onclick: () => { dismissed = true; render() } }, '✕')),
    )
    el.hidden = false
  }
  return { el, render }
}

const LAST_SERVER = 'texloom.lastServer'

/** Where a new project is kept: asked only when this browser knows more than one server (its own,
 *  those of projects on its list, those it has a creation code for), defaulting to the one used
 *  last; "Another server…" takes any. The project is then made there and synced from this page. */
export function chooseServer(listed: Iterable<string | undefined>): Promise<string | null> {
  const home = location.origin
  const known = [...new Set([home, ...[...listed].map((s) => normalServer(s)).filter((s): s is string => !!s), ...creationCode.servers()])]
  let last = home
  try { last = normalServer(localStorage.getItem(LAST_SERVER)) ?? home } catch (e) { quiet(STORAGE, e) }
  if (!known.includes(last)) last = home
  if (known.length === 1) return Promise.resolve(home)
  return new Promise((resolve) => {
    let choice: string | null = null
    const other = h('input', { placeholder: 'https://…', 'aria-label': t('Another server’s address'), oninput: () => { otherRadio.checked = true } }) as HTMLInputElement
    const otherRadio = h('input', { type: 'radio', name: 'new-server', value: '' }) as HTMLInputElement
    const msg = h('p', { class: 'err small' })
    const option = (s: string) => h('label', { class: 'radio' },
      h('input', { type: 'radio', name: 'new-server', value: s, checked: s === last }),
      h('span', {}, h('b', {}, serverName(s)),
        h('small', { class: 'muted' }, [s === home ? t('this server') : '', creationCode.get(s) ? t('you have a creation code here') : ''].filter(Boolean).join(' · '))))
    const go = () => {
      const picked = (dlg.querySelector('input[name=new-server]:checked') as HTMLInputElement | null)?.value
      const server = picked ? picked : normalServer(other.value.trim())
      if (!server) { msg.textContent = t('Give a server’s address, starting with https://'); return }
      choice = server
      try { localStorage.setItem(LAST_SERVER, server) } catch (e) { quiet(STORAGE, e) }
      dlg.close()
    }
    const dlg = dialog('settings',
      h('h3', {}, t('Where should the new project be kept?')),
      h('p', { class: 'muted small' }, t('Its encrypted copy and its collaborators’ changes go through that server. This page syncs it from there, whichever server you choose.')),
      h('div', { class: 'radios' }, ...known.map(option),
        h('label', { class: 'radio' }, otherRadio, h('span', {}, h('b', {}, t('Another server…')), other))),
      msg,
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), h('button', { class: 'primary', onclick: go }, t('Continue'))),
    )
    dlg.addEventListener('close', () => resolve(choice))
  })
}
