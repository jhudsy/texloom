// TeXLoom web app entry: the start page and the project view, wired from modules.
import * as Y from 'yjs'
import { Keys } from './crypto'
import { Replica } from './replica'
import { projects, projectCode, latexTitle, whenOpened } from './projects'
import { prefs, setupDialog, settingsDialog, type Device } from './settings'
import { createAssistantPanel } from './assistant/panel'
import { setupLayout } from './layout'
import { chooseNewProject, riskBanner, SAFARI_NOTE } from './storagenotice'
import { $, h, saveFile } from './dom'
import { fileBase, mainFile, projectZip } from './projectfiles'
import { createFileTree } from './filetree'
import { createEditorPane } from './editor'
import { createOutput } from './output'
import { pdfjs } from './pdfview'
import { ProjectIndex } from './latex'
import { autoSave, createHistoryPanel } from './history'
import './style.css'

const APP = 'TeXLoom'
const server = location.origin
const COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#469990', '#9a6324']
const brand = () => h('span', { class: 'logo', 'aria-label': APP }, 'T', h('span', { class: 'e' }, 'e'), 'XLoom')

// Offline app: a service worker caches the app itself (projects already live in IndexedDB).
const noSW = (() => { try { return localStorage.getItem('texloom.nosw') === '1' } catch { return false } })() // debugging switch
if ('serviceWorker' in navigator && !noSW && (location.protocol === 'https:' || location.hostname === 'localhost')) {
  navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('service worker:', e))
}

window.addEventListener('hashchange', () => location.reload())
const keys = (() => { try { return Keys.fromFragment(location.hash) } catch (e) { alert(e); return null } })()
if (keys) openProject(keys)
else landing()

// ---- start page ---------------------------------------------------------------
function landing() {
  document.title = APP
  const app = $('#app')
  app.className = 'landing'
  const nameIn = h('input', { value: prefs.name, placeholder: 'Your name (shown to collaborators)', oninput: (e: any) => (prefs.name = e.target.value) })
  const linkIn = h('input', { placeholder: 'Paste a project link…', 'aria-label': 'Project link' })
  app.append(
    h('h1', {}, brand()),
    h('p', { class: 'sub' }, 'Local-first collaborative LaTeX. Your files live on your devices; the server only relays encrypted changes.'),
    h('label', {}, 'Name', nameIn),
    h('div', { class: 'row' },
      h('button', { class: 'primary', onclick: async () => {
        const where = await chooseNewProject()
        if (!where) return
        const k = Keys.generate()
        if (where === 'relay') try { localStorage.setItem(`texloom.newRelay.${k.project}`, '1') } catch {}
        location.hash = k.fragment()
      } }, 'New project'),
      linkIn,
      h('button', { onclick: () => { const i = linkIn.value.indexOf('#'); if (i >= 0) location.hash = linkIn.value.slice(i) } }, 'Open'),
    ),
    h('h2', {}, 'Your projects', h('small', { class: 'muted' }, ' — opened in this browser')),
    h('p', { class: 'muted small' },
      `These are kept in this browser. Clearing this site’s data (or the browser’s history and site data) deletes them here.${SAFARI_NOTE} ` +
      'Keep the links somewhere safe: a project stored on the server can always be reopened from its link. Projects marked “not on server” exist only on devices.'),
    projectList(),
  )
}

function projectList(): HTMLElement {
  const list = projects.list()
  if (!list.length) return h('p', { class: 'muted' }, 'None yet. Projects you create or open from a link appear here.')
  const shown = (r: { nick?: string; name: string }) => r.nick || r.name || 'Untitled project'
  const dupes = new Set(list.map(shown).filter((n, i, all) => all.indexOf(n) !== i))
  return h('ul', { class: 'recent' }, ...list.map((r) => {
    const label = shown(r)
    const details: string[] = []
    if (r.nick && r.name) details.push(`shared name “${r.name}”`)
    if (r.docTitle && r.docTitle !== label && r.docTitle !== r.name) details.push(`title “${r.docTitle}”`)
    if (r.createdBy) details.push(`started by ${r.createdBy}`)
    if (r.people?.length) details.push(`with ${r.people.slice(0, 4).join(', ')}${r.people.length > 4 ? ` +${r.people.length - 4}` : ''}`)
    if (r.files) details.push(`${r.files} file${r.files === 1 ? '' : 's'}`)
    details.push(`opened ${whenOpened(r.at)}`)
    return h('li', { class: dupes.has(label) ? 'dupe' : '' },
      h('div', { class: 'top' },
        h('a', { href: '#' + r.fragment, class: 'pname' }, label),
        h('code', { class: 'pcode', title: 'Project code: derived from this project’s unique ID. Two projects with the same name always have different codes.' }, projectCode(r.project)),
        h('span', { class: `tag ${r.writable ? '' : 'ro'}` }, r.writable ? 'edit' : 'read-only'),
        r.atRisk || r.storage === 'relay' ? h('span', { class: 'tag warn', title: 'The server keeps no copy: this browser (and collaborators’ devices) hold the only copies. Clearing browser data deletes this one.' }, 'not on server') : null,
        h('span', { class: 'spacer' }),
        h('button', { class: 'icon', title: 'Private label (only you see it)', 'aria-label': `Label ${label}`, onclick: () => {
          const v = prompt(`Private label for “${r.name || 'this project'}” (${projectCode(r.project)}).\nOnly you see it, in this browser. Leave empty to use the shared name.`, r.nick ?? '')
          if (v === null) return
          projects.update(r.project, { nick: v.trim() })
          location.reload()
        } }, '✎'),
        h('button', { class: 'icon forget', title: 'Remove from this browser', 'aria-label': `Remove ${label} from this browser`, onclick: async () => {
          if (!confirm(`Remove “${label}” (${projectCode(r.project)}) from this browser?\n\nOnly this browser’s copy is deleted. Unsynced changes made here would be lost. Collaborators, folder copies and the server’s copy are not affected.`)) return
          await projects.forget(r.project); location.reload()
        } }, '✕')),
      h('div', { class: 'details muted' }, details.join(' · ')))
  }))
}

// ---- project view -----------------------------------------------------------------
async function openProject(keys: Keys) {
  let device: Device = prefs.device(keys.project)
  const r = new Replica(keys, server, device.sync)
  await r.open()
  const writable = keys.writable
  let removing = false // set while deleting, so the page doesn't re-save it to "recent"
  if (!device.setup) {
    if (r.syncMode === 'live') await new Promise((res) => setTimeout(res, 800)) // fetch the name first
    device = await setupDialog(r, keys.link(server, writable))
    r.setSyncMode(device.sync)
  }
  const me = { name: prefs.name || `anon-${r.doc.clientID % 1000}`, color: COLORS[r.doc.clientID % COLORS.length] }
  r.awareness.setLocalStateField('user', { ...me, colorLight: me.color + '33' })
  if (writable && r.clients.get(String(r.doc.clientID)) !== me.name) r.clients.set(String(r.doc.clientID), me.name) // for "who wrote this"
  autoSave(r, () => me.name)
  if (writable && !r.meta.get('name')) r.meta.set('name', 'Untitled project')
  try {
    if (writable && localStorage.getItem(`texloom.newRelay.${keys.project}`)) {
      localStorage.removeItem(`texloom.newRelay.${keys.project}`)
      r.setStorage('relay') // chosen at creation: no server copy
    }
  } catch {}
  if (writable && r.files.size === 0 && r.bin.size === 0 && !r.meta.get('seeded')) {
    setTimeout(() => { // brand-new project: give it a starter file
      if (r.files.size || r.bin.size) return
      r.doc.transact(() => {
        r.files.set('main.tex', new Y.Text('\\documentclass{article}\n\\title{Untitled}\n\\begin{document}\n\\maketitle\n\nHello, world.\n\n\\end{document}\n'))
        r.meta.set('main', 'main.tex'); r.meta.set('seeded', true)
        if (!r.meta.get('createdBy')) r.meta.set('createdBy', me.name)
      })
    }, 1500)
  }

  // ---- components ----
  const index = new ProjectIndex(r)
  const output = createOutput(r, writable, () => device, () => me.name)
  const editor = createEditorPane(r, writable, index, () => output.compile())
  const tree = createFileTree(r, writable, (p) => editor.open(p))
  // right-hand panel tabs; panels other than the PDF are created on first use
  type Tab = 'pdf' | 'ai' | 'history'
  const panes: Record<Tab, HTMLElement> = {
    pdf: output.pane,
    ai: h('div', { class: 'pane', hidden: true }),
    history: h('div', { class: 'pane', hidden: true }),
  }
  const makers: Partial<Record<Tab, () => HTMLElement>> = {
    ai: () => createAssistantPanel({
      replica: r, writable, editorContext: editor.context,
      compile: () => output.compile(false), lastLog: output.lastLog, userName: () => me.name,
    }),
    history: () => createHistoryPanel({
      replica: r, writable, userName: () => me.name,
      showAuthors: (on) => editor.showAuthors(on), openFile: (p) => tree.open(p),
    }),
  }
  const tabBtns = {
    pdf: h('button', { class: 'tab active', onclick: () => showTab('pdf') }, 'PDF'),
    ai: h('button', { class: 'tab', onclick: () => showTab('ai') }, 'Assistant'),
    history: h('button', { class: 'tab', onclick: () => showTab('history') }, 'History'),
  }
  function showTab(t: Tab) {
    for (const k of Object.keys(panes) as Tab[]) {
      panes[k].hidden = k !== t
      tabBtns[k].classList.toggle('active', k === t)
    }
    if (!panes[t].firstChild && makers[t]) panes[t].append(makers[t]!())
    try { localStorage.setItem('texloom.tab', t) } catch {}
  }

  // ---- header ----
  const status = h('span', { class: 'status', role: 'status' })
  const peers = h('span', { class: 'peers' })
  const title = h('input', { class: 'title', readonly: !writable, 'aria-label': 'Project name', onchange: (e: any) => r.meta.set('name', e.target.value) })
  const syncBtn = h('button', { onclick: async () => { syncBtn.setAttribute('disabled', ''); await r.syncNow(); syncBtn.removeAttribute('disabled') } }, 'Sync now')
  const zipBtn = h('button', { title: 'Download all files as a .zip', onclick: () => downloadZip() }, '⤓ Source')
  const leftBtn = h('button', { class: 'pane-toggle', onclick: () => layout.toggleLeft() }, '☰')
  const rightBtn = h('button', { class: 'pane-toggle', onclick: () => layout.toggleRight() }, '▥')
  const splitL = h('div', { class: 'split' })
  const splitR = h('div', { class: 'split' })
  const mainEl = h('main', {})
  const risk = riskBanner(r, writable, () => openSettings())

  const app = $('#app')
  app.className = 'project'
  app.append(
    h('header', {},
      leftBtn,
      h('a', { href: '/', class: 'brand', title: 'All projects', onclick: (e: Event) => { e.preventDefault(); location.href = '/' } }, brand()),
      title, writable ? null : h('span', { class: 'badge' }, 'read-only'),
      h('span', { class: 'spacer' }), peers, status,
      syncBtn,
      zipBtn,
      h('button', { onclick: () => share(keys) }, 'Share'),
      h('button', { title: 'Settings', onclick: () => openSettings() }, '⚙'),
      output.compileBtn,
      rightBtn,
    ),
    risk.el,
    mainEl,
  )
  mainEl.append(
    h('aside', {}, tree.el),
    splitL,
    editor.el,
    splitR,
    h('section', { class: 'right' }, h('div', { class: 'tabs' }, tabBtns.pdf, tabBtns.ai, tabBtns.history), panes.pdf, panes.ai, panes.history),
  )
  const layout = setupLayout(mainEl, splitL, splitR)
  layout.onChange = () => {
    leftBtn.title = layout.leftHidden ? 'Show the file list' : 'Hide the file list'
    rightBtn.title = layout.rightHidden ? 'Show the PDF and assistant' : 'Hide the PDF and assistant'
    leftBtn.classList.toggle('off', layout.leftHidden)
    rightBtn.classList.toggle('off', layout.rightHidden)
    leftBtn.setAttribute('aria-pressed', String(!layout.leftHidden))
    rightBtn.setAttribute('aria-pressed', String(!layout.rightHidden))
  }
  const savedTab = localStorage.getItem('texloom.tab') as Tab | null
  if (savedTab && savedTab in panes && savedTab !== 'pdf') showTab(savedTab)

  // ⌘S / Ctrl+S compiles from anywhere (the editor handles it itself when focused)
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 's') {
      e.preventDefault()
      output.compile()
    }
  })
  // stop the browser from opening files dropped outside the file tree
  app.addEventListener('dragover', (e) => e.preventDefault())
  app.addEventListener('drop', (e) => e.preventDefault())

  function openSettings() {
    settingsDialog(r, keys.link(server, writable), applyDevice, async () => {
      removing = true
      await r.close()
      await projects.forget(keys.project)
      location.href = '/'
    })
  }
  function applyDevice(d: Device) {
    device = d
    r.setSyncMode(d.sync)
    syncBtn.hidden = d.sync !== 'manual'
    output.compileBtn.hidden = d.compile === 'none'
  }

  async function downloadZip() {
    zipBtn.setAttribute('disabled', '')
    try {
      const { zip, missing } = await projectZip(r)
      saveFile(`${fileBase(r)}.zip`, zip, 'application/zip')
      if (missing.length) alert(`These files weren’t included because their content isn’t available yet (offline, and no collaborator online has them):\n\n${missing.join('\n')}`)
    } finally { zipBtn.removeAttribute('disabled') }
  }

  // ---- status / presence ----
  let lastRisk: boolean | null = null
  const renderStatus = () => {
    status.className = `status ${r.status}`
    const unsent = r.pending ? ` · ${r.pending} unsent` : ''
    const relay = r.storage === 'relay' ? ` · relay-only, ${r.peers} peer${r.peers === 1 ? '' : 's'} online` : ''
    status.textContent =
      r.status === 'online' ? (r.pending ? `syncing ${r.pending}…` : 'saved') + relay
      : r.status === 'connecting' ? 'connecting…'
      : r.status === 'paused' ? `manual sync${unsent}`
      : `offline${unsent} (changes kept locally)`
    status.title = r.storage === 'relay' ? 'The server keeps no copy: changes reach collaborators only while you are both online.' : 'Changes are stored encrypted on the server.'
    if (r.storeProblem && r.storage !== 'relay' && r.status === 'online') {
      status.textContent = 'not saved on server'
      status.className = 'status warn'
      status.title = 'Changes reach collaborators who are online, and stay in this browser, but the server isn’t storing them. See the warning above.'
    }
    risk.render()
    const atRisk = !!r.storeProblem || r.storage === 'relay'
    if (!removing && atRisk !== lastRisk) { lastRisk = atRisk; projects.update(keys.project, { atRisk }) }
  }
  r.onStatus = renderStatus; renderStatus()
  r.meta.observe(() => renderStatus()) // storage mode can change from Settings or a collaborator
  applyDevice(device)

  r.awareness.on('change', () => {
    const others = [...r.awareness.getStates().entries()].filter(([id, s]) => id !== r.doc.clientID && s.user)
    peers.replaceChildren(...others.map(([, s]) =>
      h('span', { class: 'peer', style: `background:${/^#[0-9a-f]{6}$/i.test(s.user.color) ? s.user.color : '#888'}`, title: String(s.user.name).slice(0, 64) }, String(s.user.name).slice(0, 2))))
    if (!removing) projects.sawPeople(keys.project, others.map(([, s]) => String(s.user.name)), me.name)
  })

  // keep the start page's summary (document title, file count) current
  let summaryTimer: ReturnType<typeof setTimeout> | undefined
  const summarize = () => {
    clearTimeout(summaryTimer)
    summaryTimer = setTimeout(() => {
      if (removing) return
      const main = mainFile(r)
      projects.update(keys.project, { docTitle: main ? latexTitle(r.files.get(main)!.toString()) : undefined, files: r.files.size + r.bin.size })
    }, 1500)
  }
  r.files.observeDeep(summarize); r.bin.observe(summarize); summarize()

  const renderMeta = () => {
    title.value = r.meta.get('name') || ''
    document.title = `${title.value} — ${APP}`
    if (!removing) projects.update(keys.project, {
      fragment: keys.fragment(), writable, at: Date.now(), name: title.value,
      createdBy: r.meta.get('createdBy'), storage: r.storage,
    })
    title.title = `Project name (shared with collaborators) · code ${projectCode(keys.project)}`
  }
  r.meta.observe(renderMeta); renderMeta()

  // debugging handle, only when enabled with localStorage['texloom.debug'] = '1'
  try { if (localStorage.getItem('texloom.debug') === '1') (window as any).texloom = { replica: r, Y, output, pdfjs } } catch {}
}

function share(keys: Keys) {
  const dlg = h('dialog', { class: 'share' },
    h('h3', {}, 'Share this project'),
    ...(keys.writable ? [h('label', {}, 'Edit link — anyone with it can edit'), copyRow(keys.link(server, true))] : []),
    h('label', {}, 'Read-only link'), copyRow(keys.link(server, false)),
    h('p', { class: 'muted' }, 'Keys stay in the part after “#”, which is never sent to the server. To revoke access, create a new project.'),
    h('button', { onclick: () => dlg.close() }, 'Close'),
  )
  document.body.append(dlg)
  dlg.addEventListener('close', () => dlg.remove())
  dlg.showModal()
}
function copyRow(text: string) {
  const inp = h('input', { value: text, readonly: true, onfocus: (e: any) => e.target.select() })
  return h('div', { class: 'row' }, inp, h('button', { onclick: (e: any) => { navigator.clipboard.writeText(text); e.target.textContent = 'Copied' } }, 'Copy'))
}
