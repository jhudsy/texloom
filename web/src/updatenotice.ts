// A page that is already running keeps the code it loaded with. The service worker (sw.js) fetches a new
// version in the background and takes over for the next load, but nothing reloads a tab that was left open,
// so someone who keeps a project open for days stays on the old version. This says a new one is ready and
// offers the reload; it never reloads by itself, though changes not yet sent are kept in the browser either way.
import { h } from './dom'
import { t } from './i18n'
import { quiet } from './quiet'

const CHECK_EVERY = 60 * 60 * 1000 // browsers check for a new sw.js when the page loads and about daily; this is for a tab that stays open

/** `hadController`: the page was already under the service worker when it loaded (else its first
 *  takeover is the installation, not an update). */
export function watchForNewVersion(reg: ServiceWorkerRegistration, hadController: boolean): void {
  let first = !hadController
  let notice: HTMLElement | null = null
  const show = () => {
    if (notice) return
    notice = h('div', { class: 'update-notice', role: 'status' },
      h('span', {}, t('A new version of TeXLoom is ready.')),
      h('button', { class: 'primary', onclick: () => location.reload() }, t('Reload')),
      h('button', { class: 'plain', 'aria-label': t('Dismiss'), title: t('Dismiss'), onclick: () => { notice?.remove(); notice = null } }, '×')) // (a still newer version shows it again)
    document.body.append(notice)
  }
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (first) { first = false; return }
    show()
  })
  const check = () => { if (navigator.onLine) reg.update().catch((e) => quiet('no new version could be looked for now', e)) }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check() })
  setInterval(check, CHECK_EVERY)
}
