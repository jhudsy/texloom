// Storage-related UI: creation codes, the "only in this browser" warning, and the
// new-project flow on servers that require a creation code.
import { creationCode, type Replica } from './replica'
import { dialog, h } from './dom'
import { t } from './i18n'

const isSafari = /^((?!chrome|android|crios|fxios).)*safari/i.test(navigator.userAgent)
export const SAFARI_NOTE = isSafari ? ' ' + t('Safari may also delete a site’s data after about 7 days without a visit.') : ''

export async function serverInfo(): Promise<{ creation: 'open' | 'code'; projectMaxBytes: number; projectLists?: boolean; private?: boolean; codesUrl?: string } | null> {
  try { const r = await fetch('/api/info'); return r.ok ? await r.json() : null } catch { return null }
}

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
export async function privateServerAllows(): Promise<boolean> {
  if (!(await serverInfo())?.private) return true
  const code = creationCode.get()
  const r = code ? await fetch('/api/code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) }).then((x) => x.json()).catch(() => null) : null
  return r?.ok === true || askCode(PRIVATE_INTRO())
}

/** Ask for a creation code, check it with the server, save it. Resolves true if a valid code was saved. */
export function askCode(intro?: string): Promise<boolean> {
  return new Promise((resolve) => {
    const input = h('input', { value: creationCode.get(), placeholder: t('e.g. 3PYW-WTHZ-PRCF'), autocomplete: 'off', 'aria-label': t('Creation code') }) as HTMLInputElement
    const msg = h('p', { class: 'err small' })
    let ok = false
    const check = async () => {
      const code = input.value.trim()
      if (!code) return
      const r = await fetch('/api/code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) })
        .then((x) => x.json()).catch(() => null)
      if (r?.ok) {
        creationCode.set(code); ok = true
        if (r.allowance != null) alert(t('Code accepted. Its storage allowance is {{total}} MB, of which {{used}} MB is used.', { total: (r.allowance / 1048576).toFixed(0), used: (r.used / 1048576).toFixed(1) }))
        dlg.close()
      }
      else msg.textContent = r ? t('That code isn’t valid on this server.') : t('Couldn’t reach the server; try again when online.')
    }
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') check() })
    const source = h('p', {}, codeSource(null))
    serverInfo().then((info) => source.replaceChildren(codeSource(info)))
    const dlg = dialog('settings',
      h('h3', {}, t('Creation code')),
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

/** "Create project": on servers that need a code, ask for one or offer a project without a server copy. */
export async function chooseNewProject(): Promise<'server' | 'relay' | null> {
  if (!(await privateServerAllows())) return null
  const info = await serverInfo()
  if (!info || info.creation === 'open' || creationCode.get()) return 'server'
  return new Promise((resolve) => {
    let choice: 'server' | 'relay' | null = null
    const dlg = dialog('settings',
      h('h3', {}, t('Create project')),
      h('p', {}, t('A creation code lets this server keep an encrypted copy of projects you create.'), ' ', codeSource(info)),
      h('div', { class: 'radios' },
        h('button', { class: 'radio choice', onclick: async () => { if (await askCode()) { choice = 'server'; dlg.close() } } },
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
        h('span', {}, h('b', {}, '⚠ '), r.denied === 'code-expired'
          ? t('Not syncing: this server is private, and the creation code this project was created with has expired or been removed. Renewing it restores access.')
          : t('Not syncing: this server is private, and this project needs a creation code from its owner.'),
          ' ' + t('Your copy here is safe and you can keep working, but nobody else gets your changes.')),
        h('span', { class: 'actions' },
          r.denied === 'needs-code' ? h('button', { class: 'primary', onclick: async () => { if (await askCode(PRIVATE_INTRO())) r.reconnect() } }, t('Enter code')) : '',
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
