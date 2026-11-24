// The address bar shows only a project's id, never its keys, so that copying the address to
// someone doesn't hand them the project (an owner's address would otherwise carry the owner key).
// Links from Share still contain the keys: opening one moves them from the address into the browser.
import { Keys } from './crypto'
import { dialog, h } from './dom'
import { t } from './i18n'
import { projects, sameKeys, tabKeysItem } from './projects'
import { fragmentFor, normalServer, serverName, serverOr } from './servers'
import { session } from './store'

/** Remember this tab's link, so that reloading keeps the same access. False if the browser won't store it. */
function rememberForTab(project: string, fragment: string): boolean {
  return session.set(tabKeysItem(project), fragment) && session.get(tabKeysItem(project)) === fragment
}

function linkKnownHere(project: string): string | null {
  return session.get(tabKeysItem(project)) || projects.fragmentOf(project)
}

export interface Opened { keys: Keys; server: string }

/** Keys and server from a fragment: the server is "s", if it's another server's project (servers.ts). */
function opened(fragment: string): Opened | null {
  const s = new URLSearchParams(fragment).get('s')
  if (s && !normalServer(s)) throw new Error(t('This link names a server that can’t be used: {{server}}', { server: s }))
  const keys = Keys.fromFragment(fragment)
  return keys && { keys, server: serverOr(s) }
}

/** A link to a project already in the list. Anyone can make a link naming a project you have and a
 *  server of their own: it opens on the server the list has unless the user says otherwise (for
 *  this tab only; the list keeps its server), and with the list's keys if the link's aren't this
 *  project's as the list has them. */
function forKnown(project: string, fragment: string): string {
  const stored = projects.list().find((x) => x.project === project)
  if (!stored) return fragment
  const keys = Keys.fromFragment(fragment)
  const own = keys && sameKeys(stored.fragment, keys.fragment()) ? keys.fragment() : stored.fragment
  const linked = serverOr(new URLSearchParams(fragment).get('s')), mine = serverOr(stored.server)
  if (linked === mine) return fragmentFor(own, mine)
  const keep = confirm(t('This link opens “{{name}}” on {{server}}, but your project list has it on {{stored}}. A link naming another server could come from someone who runs that server: only open it there if you trust it.\n\nOK: open it on {{stored}}, as before.\nCancel: open it on {{server}}, in this tab only.',
    { name: stored.nick || stored.name || project.slice(0, 8), server: serverName(linked), stored: serverName(mine) }))
  return fragmentFor(own, keep ? mine : linked)
}

/** The project the address opens: null if none, 'no-key' if this browser doesn't have its keys. */
export function projectFromAddress(): Opened | 'no-key' | null {
  const fragment = location.hash.slice(1)
  const params = new URLSearchParams(fragment)
  const project = params.get('p')
  if (!project) return null
  if (params.get('k')) {
    opened(fragment) // refuses a link with an unusable server or keys, whether the project is known or not
    const use = forKnown(project, fragment)
    const found = opened(use)
    if (found && rememberForTab(project, use)) history.replaceState(null, '', `${location.pathname}${location.search}#p=${project}`)
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
