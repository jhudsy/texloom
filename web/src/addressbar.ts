// The address bar shows only a project's id, never its keys, so that copying the address to
// someone doesn't hand them the project (an owner's address would otherwise carry the owner key).
// Links from Share still contain the keys: opening one moves them from the address into the browser.
import { Keys } from './crypto'
import { dialog, h } from './dom'
import { t } from './i18n'
import { projects, tabKeysItem } from './projects'

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

/** The project the address opens: null if none, 'no-key' if this browser doesn't have its keys. */
export function projectFromAddress(): Keys | 'no-key' | null {
  const fragment = location.hash.slice(1)
  const params = new URLSearchParams(fragment)
  const project = params.get('p')
  if (!project) return null
  if (params.get('k')) {
    const keys = Keys.fromFragment(fragment)
    if (keys && rememberForTab(project, fragment)) history.replaceState(null, '', `${location.pathname}${location.search}#p=${project}`)
    return keys
  }
  const known = linkKnownHere(project)
  if (!known) return 'no-key'
  rememberForTab(project, known)
  return Keys.fromFragment(known)
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
