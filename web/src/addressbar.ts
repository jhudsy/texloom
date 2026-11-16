// The address bar shows only a project's id, never its keys, so that copying the address to
// someone doesn't hand them the project (an owner's address would otherwise carry the owner key).
// Links from Share still contain the keys: opening one moves them from the address into the browser.
import { Keys } from './crypto'
import { dialog, h } from './dom'
import { t } from './i18n'
import { projects, tabKeysItem } from './projects'
import { normalServer, serverOr } from './servers'

/** Remember this tab's link, so that reloading keeps the same access. False if the browser won't store it. */
function rememberForTab(project: string, fragment: string): boolean {
  try {
    sessionStorage.setItem(tabKeysItem(project), fragment)
    return sessionStorage.getItem(tabKeysItem(project)) === fragment
  } catch { return false }
}

function linkKnownHere(project: string): string | null {
  try {
    const here = sessionStorage.getItem(tabKeysItem(project))
    if (here) return here
  } catch {}
  return projects.fragmentOf(project)
}

export interface Opened { keys: Keys; server: string }

/** Keys and server from a fragment: the server is "s", if it's another server's project (servers.ts). */
function opened(fragment: string): Opened | null {
  const s = new URLSearchParams(fragment).get('s')
  if (s && !normalServer(s)) throw new Error(t('This link names a server that can’t be used: {{server}}', { server: s }))
  const keys = Keys.fromFragment(fragment)
  return keys && { keys, server: serverOr(s) }
}

/** The project the address opens: null if none, 'no-key' if this browser doesn't have its keys. */
export function projectFromAddress(): Opened | 'no-key' | null {
  const fragment = location.hash.slice(1)
  const params = new URLSearchParams(fragment)
  const project = params.get('p')
  if (!project) return null
  if (params.get('k')) {
    const found = opened(fragment)
    if (found && rememberForTab(project, fragment)) history.replaceState(null, '', `${location.pathname}${location.search}#p=${project}`)
    return found
  }
  const known = linkKnownHere(project)
  if (!known) return 'no-key'
  rememberForTab(project, known)
  return opened(known)
}

/** Someone opened a copied address, which has no keys in it. */
export function missingKeysDialog() {
  history.replaceState(null, '', location.pathname + location.search)
  const dlg = dialog('settings',
    h('h3', {}, t('This address can’t open the project')),
    h('p', {}, t('For safety, the address bar doesn’t contain a project’s keys, so a copied address only works in the browser that opened the project.')),
    h('p', {}, t('Ask whoever sent it to use Share and send you a link. If this is your own project, open it from your project list or from its link.')),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('OK'))),
  )
}
