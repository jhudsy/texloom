// Storage-related UI: creation codes, the "only in this browser" warning, and the
// new-project flow on servers that require a creation code.
import { creationCode, type Replica } from './replica'
import { dialog, h } from './settings'

const isSafari = /^((?!chrome|android|crios|fxios).)*safari/i.test(navigator.userAgent)
export const SAFARI_NOTE = isSafari ? ' Safari may also delete a site’s data after about 7 days without a visit.' : ''

export async function serverInfo(): Promise<{ creation: 'open' | 'code'; projectMaxBytes: number } | null> {
  try { const r = await fetch('/api/info'); return r.ok ? await r.json() : null } catch { return null }
}

/** Ask for a creation code, check it with the server, save it. Resolves true if a valid code was saved. */
export function askCode(intro?: string): Promise<boolean> {
  return new Promise((resolve) => {
    const input = h('input', { value: creationCode.get(), placeholder: 'e.g. 3PYW-WTHZ-PRCF', autocomplete: 'off', 'aria-label': 'Creation code' }) as HTMLInputElement
    const msg = h('p', { class: 'err small' })
    let ok = false
    const check = async () => {
      const code = input.value.trim()
      if (!code) return
      const r = await fetch('/api/code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) })
        .then((x) => x.json()).catch(() => null)
      if (r?.ok) { creationCode.set(code); ok = true; dlg.close() }
      else msg.textContent = r ? 'That code isn’t valid on this server.' : 'Couldn’t reach the server; try again when online.'
    }
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') check() })
    const dlg = dialog('settings',
      h('h3', {}, 'Creation code'),
      h('p', { class: 'muted' }, intro ?? 'This server stores new projects only for people with a creation code from its owner. You only need it once per browser; opening and editing projects shared with you never needs one.'),
      h('label', { class: 'field' }, 'Code', input),
      msg,
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, 'Cancel'), h('button', { class: 'primary', onclick: check }, 'Save code')),
    )
    dlg.addEventListener('close', () => resolve(ok))
    input.focus()
  })
}

/** "New project": on servers that need a code, ask for one or offer a project without a server copy. */
export async function chooseNewProject(): Promise<'server' | 'relay' | null> {
  const info = await serverInfo()
  if (!info || info.creation === 'open' || creationCode.get()) return 'server'
  return new Promise((resolve) => {
    let choice: 'server' | 'relay' | null = null
    const dlg = dialog('settings',
      h('h3', {}, 'New project'),
      h('p', {}, 'This server keeps an encrypted copy of new projects only for people with a creation code.'),
      h('div', { class: 'radios' },
        h('button', { class: 'radio choice', onclick: async () => { if (await askCode()) { choice = 'server'; dlg.close() } } },
          h('span', {}, h('b', {}, 'I have a code'), h('small', { class: 'muted' }, 'The server keeps an encrypted copy, so collaborators get your changes even when you’re offline, and the link always restores the project.'))),
        h('button', { class: 'radio choice', onclick: () => { choice = 'relay'; dlg.close() } },
          h('span', {}, h('b', {}, 'Continue without a server copy'), h('small', { class: 'muted' }, `Changes reach collaborators only while you’re both online. The project lives in your browser (and theirs): clearing browser data deletes your copy.${SAFARI_NOTE}`)))),
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, 'Cancel')),
    )
    dlg.addEventListener('close', () => resolve(choice))
  })
}

/** Banner shown when this browser may hold the project's only copy. */
export function riskBanner(r: Replica, writable: boolean, openSettings: () => void) {
  const el = h('div', { class: 'risk', hidden: true, role: 'status' })
  let dismissed = false
  const render = () => {
    const relay = r.storage === 'relay'
    const p = relay ? null : r.storeProblem // relay-only projects aren't meant to be stored
    if (!writable || dismissed || (!p && !relay)) { el.hidden = true; return }
    const MB = Math.round((r.storeInfo?.projectMax ?? 0) / 1048576)
    const why =
      p === 'needs-code' ? 'Not saved on the server: storing new projects here needs a creation code.'
      : p === 'project-full' ? `Not saved on the server: this project has reached the server’s ${MB} MB limit.`
      : p === 'server-full' ? 'Not saved on the server: the server is out of space. Changes are kept here and retried every few minutes.'
      : 'This project isn’t stored on the server (Settings → Server storage).'
    el.replaceChildren(
      h('span', {}, h('b', {}, '⚠ '), why,
        ` Its copies are in this browser and with collaborators: clearing this browser’s data deletes yours.${SAFARI_NOTE} Keep a folder copy to be safe.`),
      h('span', { class: 'actions' },
        p === 'needs-code' ? h('button', { class: 'primary', onclick: async () => { if (await askCode()) r.retryStore() } }, 'Enter code') : '',
        h('button', { onclick: openSettings }, 'Folder copy…'),
        h('button', { title: 'Hide until next time', 'aria-label': 'Hide this warning', onclick: () => { dismissed = true; render() } }, '✕')),
    )
    el.hidden = false
  }
  return { el, render }
}
