// Hand a project to the desktop app on this computer, which asks where to keep it. A page can't
// tell whether anything handles texloom:// links, so it always says what to do if nothing happens.
import { dialog, h } from './dom'
import { t } from './i18n'
import { creationCode } from './replica'

export const DOWNLOAD_APP = 'https://github.com/jhudsy/texloom/releases/latest'

export function openInApp(link: string) {
  const send = () => { location.href = `texloom://join?link=${encodeURIComponent(link)}` }
  const dlg = dialog('settings',
    h('h3', {}, t('Opening the TeXLoom app…')),
    h('p', {}, t('Your browser may ask for permission first. The app will then ask where to keep the project, and keeps that folder in sync even when this page is closed.')),
    h('p', { class: 'muted' }, t('If nothing happens, the app probably isn’t installed. Just installed it? Open it once, then press Try again.')),
    h('p', { class: 'muted' }, t('Or copy the project’s link and paste it in the app, under Add project ▸ Join a project with a link….')),
    h('div', { class: 'row end' },
      h('a', { class: 'button', href: DOWNLOAD_APP, target: '_blank', rel: 'noopener' }, t('Download the app')),
      h('button', { onclick: (e: Event) => { void navigator.clipboard.writeText(link); (e.currentTarget as HTMLElement).textContent = t('Copied') } }, t('Copy link')),
      h('button', { onclick: send }, t('Try again')),
      h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
  send()
}

/** Hand this browser's project list to the desktop app, which asks before using it. */
export function sendListToApp(link: string) {
  const send = () => { location.href = `texloom://sync?link=${encodeURIComponent(link)}` }
  const dlg = dialog('settings',
    h('h3', {}, t('Opening the TeXLoom app…')),
    h('p', {}, t('The app will ask before it uses this list. Then it shows the projects on the list that aren’t on this computer yet, and your creation code is filled in when you make a project there.')),
    h('p', { class: 'muted' }, t('If nothing happens, the app probably isn’t installed. Just installed it? Open it once, then press Try again.')),
    h('div', { class: 'row end' },
      h('a', { class: 'button', href: DOWNLOAD_APP, target: '_blank', rel: 'noopener' }, t('Download the app')),
      h('button', { onclick: send }, t('Try again')),
      h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
  send()
}

/** Make a project from a folder on this computer: the desktop app asks which folder, uploads it
 *  (encrypted, with keys it makes itself) and keeps it in sync. This server and the creation code
 *  entered here go with the request, so nothing needs typing again. */
export function newProjectInApp() {
  const q = new URLSearchParams({ server: location.origin })
  if (creationCode.get()) q.set('code', creationCode.get())
  const send = () => { location.href = `texloom://new?${q}` }
  const dlg = dialog('settings',
    h('h3', {}, t('Opening the TeXLoom app…')),
    h('p', {}, t('The app will ask which folder to use, then upload its files, encrypted, and keep the folder in sync with the project. Once it’s made, the app opens it in your browser, which also puts it under Your projects.')),
    h('p', { class: 'muted' }, t('If nothing happens, the app probably isn’t installed. Just installed it? Open it once, then press Try again.')),
    h('p', { class: 'muted' }, t('In the app, this is Add project ▸ New project from a folder….')),
    h('div', { class: 'row end' },
      h('a', { class: 'button', href: DOWNLOAD_APP, target: '_blank', rel: 'noopener' }, t('Download the app')),
      h('button', { onclick: send }, t('Try again')),
      h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
  send()
}
