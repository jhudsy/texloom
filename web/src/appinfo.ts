// What the start page and the project view share: how the page was opened, the app's name and
// logo, and redrawing the project list.
import { h } from './dom'
import { t } from './i18n'

// The desktop app opens its projects with ?from=app (it already keeps them in a folder); the
// address is put back as it was, so a copied link doesn't carry it.
export const openedByApp = new URLSearchParams(location.search).get('from') === 'app'
export const appCompiles = openedByApp && new URLSearchParams(location.search).get('compile') === '1'
if (openedByApp) history.replaceState(null, '', location.pathname + location.hash)

/** The app opened this project here, so it keeps it in a folder on this computer (as far as this
 *  browser knows: the start page offers "In the TeXLoom app" instead of "Sync to a folder…"). */
export const inAppKey = (project: string) => `texloom.inApp.${project}`

export const APP = 'TeXLoom'
export const UNTITLED = 'Untitled project' // stored in the shared document (and compared in templates.ts): kept in English
export const brand = () => h('span', { class: 'logo', 'aria-label': APP }, 'T', h('span', { class: 'e' }, 'e'), 'XLoom')

/** A description from texscan.ts (kept in English there: it is also data), translated. */
const PROGRAM_WHAT: Record<string, () => string> = {
  'runs a shell command (\\write18)': () => t('runs a shell command (\\write18)'),
  'runs a shell command (\\ShellEscape)': () => t('runs a shell command (\\ShellEscape)'),
  'reads the output of a command (\\input|)': () => t('reads the output of a command (\\input|)'),
  'runs a program from Lua': () => t('runs a program from Lua'),
  'loads a package that runs programs': () => t('loads a package that runs programs'),
  'TikZ externalisation runs LaTeX as a command': () => t('TikZ externalisation runs LaTeX as a command'),
  'tells arara which programs to run': () => t('tells arara which programs to run'),
  'sets the program your LaTeX editor runs': () => t('sets the program your LaTeX editor runs'),
}
export const programWhat = (en: string) => PROGRAM_WHAT[en]?.() ?? en

/** Redraw the start page's list (it changed on another device, say), as the page now shown does it. */
let refresh = () => {}
export const refreshProjects = () => refresh()
export const onRefresh = (f: () => void) => { refresh = f }
