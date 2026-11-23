// TeXLoom web app entry: the start page or a project, whichever the address asks for.
import { missingKeysDialog, projectFromAddress } from './addressbar'
import { handleListHash, takeListHash } from './listdialog'
import { startListSync } from './listsync'
import { openPdfWindow, pdfWindowProject } from './pdfwindow'
import { refreshProjects } from './appinfo'
import { landing } from './startpage'
import { openProject } from './projectview'
import './style.css'

// Offline app: a service worker caches the app itself (projects already live in IndexedDB).
const noSW = (() => { try { return localStorage.getItem('texloom.nosw') === '1' } catch { return false } })() // debugging switch
if ('serviceWorker' in navigator && !noSW && (location.protocol === 'https:' || location.hostname === 'localhost')) {
  navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('service worker:', e))
}

window.addEventListener('hashchange', () => location.reload())
const pdfWindowFor = pdfWindowProject() // this page is only a window showing a project's PDF
const listHash = pdfWindowFor ? null : takeListHash()
const opened = (() => { try { return projectFromAddress() } catch (e) { alert(e); return null } })()
const openedProject = opened === 'no-key' ? null : opened

// Last, so that everything above is defined by the time the page is built.
if (pdfWindowFor) openPdfWindow(pdfWindowFor)
else {
  if (openedProject) openProject(openedProject.keys, openedProject.server)
  else landing()
  startListSync(() => refreshProjects())
  if (listHash) handleListHash(listHash)
  if (opened === 'no-key') missingKeysDialog()
}
