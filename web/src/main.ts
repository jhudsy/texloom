// TeXLoom web app entry: the start page and the project view, wired from modules.
import * as Y from 'yjs'
import { Keys } from './crypto'
import { Replica } from './replica'
import { projects, projectCode, latexTitle, whenOpened, type Recent } from './projects'
import { removeDialog } from './remove'
import { prefs, setupDialog, settingsDialog, languagePicker, type Device } from './settings'
import { createAssistantPanel } from './assistant/panel'
import { setupLayout } from './layout'
import { askCode, chooseNewProject, riskBanner, SAFARI_NOTE } from './storagenotice'
import { $, h, saveFile } from './dom'
import { t, tn, tNodes, locale } from './i18n'
import { fileBase, mainFile, projectZip } from './projectfiles'
import { createFileTree } from './filetree'
import { missingKeysDialog, projectFromAddress } from './addressbar'
import { menuItems, openContextMenu, type MenuItem } from './contextmenu'
import { menuTargets } from './editormenu'
import { FilePreview, isPreviewable } from './preview'
import { isTexFile } from './projectfiles'
import { handleListHash, listDialog, takeListHash } from './listdialog'
import { savedSecret, startListSync, tagsProblem, syncNow as syncList } from './listsync'
import { copyDialog } from './copyproject'
import { tags, type Tag } from './tags'
import { manageTags, pickTags } from './tagdialogs'
import { openInApp } from './openinapp'
import { createEditorPane } from './editor'
import { createOutput } from './output'
import { pdfjs } from './pdfview'
import { SyncTex } from './synctex'
import type { ProgramLine } from './texscan'
import { ProjectIndex } from './latex'
import { autoSave, createHistoryPanel } from './history'
import { referenceDialog } from './references'
import { watchClashes, type Clash } from './clashes'
import { shareDialog } from './sharing'
import { folderFiles, folderPermission, folderSyncSupported, pickFolder, rememberFolder, savedFolder, startFolderSync, type FolderSync } from './foldersync'
import { applyTemplate, chooseTemplate, importDialog, TEMPLATES } from './templates'
import { createOutlinePanel, createSearchPanel, blankNonProse, countWords } from './navigate'
import { createReviewPanel, reviewExtension, type ReviewPanel } from './review'
import './style.css'

const APP = 'TeXLoom'
const server = location.origin
const COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#469990', '#9a6324']
const UNTITLED = 'Untitled project' // stored in the shared document (and compared in templates.ts): kept in English
const brand = () => h('span', { class: 'logo', 'aria-label': APP }, 'T', h('span', { class: 'e' }, 'e'), 'XLoom')

// Offline app: a service worker caches the app itself (projects already live in IndexedDB).
const noSW = (() => { try { return localStorage.getItem('texloom.nosw') === '1' } catch { return false } })() // debugging switch
if ('serviceWorker' in navigator && !noSW && (location.protocol === 'https:' || location.hostname === 'localhost')) {
  navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('service worker:', e))
}

window.addEventListener('hashchange', () => location.reload())
const listHash = takeListHash()
const opened = (() => { try { return projectFromAddress() } catch (e) { alert(e); return null } })()
const keys = opened === 'no-key' ? null : opened
let refreshProjects = () => {}
/** What the start page's list is narrowed to. */
const finding = { text: '', tags: new Set<string>() }
const foldSearch = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()
if (keys) openProject(keys)
else landing()
startListSync(() => refreshProjects())
if (listHash) handleListHash(listHash)
if (opened === 'no-key') missingKeysDialog()

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
const programWhat = (en: string) => PROGRAM_WHAT[en]?.() ?? en

// ---- start page ---------------------------------------------------------------
function landing() {
  document.title = APP
  const app = $('#app')
  app.className = 'landing'
  const nameIn = h('input', { value: prefs.name, placeholder: t('Your name (shown to collaborators)'), oninput: (e: any) => (prefs.name = e.target.value) })
  const linkIn = h('input', { placeholder: t('Paste a project link…'), 'aria-label': t('Project link') })
  const list = h('div', {}, projectList())
  const finder = h('input', { type: 'search', class: 'finder', value: finding.text, placeholder: t('Search by name or tag…'), 'aria-label': t('Search your projects'), hidden: !projects.list().length, oninput: (e: any) => { finding.text = e.target.value; list.replaceChildren(projectList()) } })
  const syncNote = h('div', {})
  const showSync = () => {
    const synced = !!savedSecret()
    syncNote.replaceChildren(
      h('p', { class: 'muted small' }, synced
        ? t('This list is synced between your devices, encrypted. The server can’t read it.')
        : t('This list is kept only in this browser: clearing this site’s data deletes it.{{safari}} A project stored on the server can always be reopened from its link, so keep your links safe, or sync this list to have it on all your devices.', { safari: SAFARI_NOTE })),
      h('p', {}, h('button', { onclick: () => listDialog(() => { showSync(); refreshProjects() }) }, synced ? t('Manage sync…') : t('Sync this list…'))),
    )
  }
  showSync()
  app.append(
    h('h1', {}, brand()),
    h('p', { class: 'sub' }, t('Local-first collaborative LaTeX. Your files live on your devices; the server only relays encrypted changes.')),
    h('label', {}, t('Name'), nameIn),
    languagePicker(),
    h('div', { class: 'row' },
      h('button', { class: 'primary', onclick: async () => {
        const template = await chooseTemplate()
        if (!template) return
        const where = await chooseNewProject()
        if (!where) return
        const k = Keys.generate()
        try {
          if (where === 'relay') localStorage.setItem(`texloom.newRelay.${k.project}`, '1')
          localStorage.setItem(`texloom.newTemplate.${k.project}`, template)
        } catch {}
        location.hash = k.fragment()
      } }, t('New project')),
    ),
    h('div', { class: 'row' },
      linkIn,
      h('button', { onclick: () => { const i = linkIn.value.indexOf('#'); if (i >= 0) location.hash = linkIn.value.slice(i) } }, t('Open')),
    ),
    h('h2', {}, t('Your projects')),
    syncNote,
    finder,
    list,
  )
  refreshProjects = () => {
    finder.hidden = !projects.list().length
    list.replaceChildren(projectList())
  }
}

/** Every word is somewhere in the project's names or tags, and it has every chosen tag. */
function found(r: Recent, label: string, own: Tag[]): boolean {
  if ([...finding.tags].some((id) => !own.some((x) => x.id === id))) return false
  const text = foldSearch([label, r.name, r.nick, r.docTitle, projectCode(r.project), ...own.map((x) => x.name)].filter(Boolean).join(' '))
  return foldSearch(finding.text).split(/\s+/).filter(Boolean).every((word) => text.includes(word))
}

function toggleFilter(id: string) {
  if (!finding.tags.delete(id)) finding.tags.add(id)
  refreshProjects()
  document.querySelector<HTMLElement>(`.tagbar [data-tag="${CSS.escape(id)}"]`)?.focus()
}

function tagBar(): HTMLElement | null {
  const all = tags.all()
  for (const id of finding.tags) if (!all.some((x) => x.id === id)) finding.tags.delete(id)
  const stuck = tagsProblem() === 'needs-code'
  if (!all.length && !stuck) return null
  return h('div', {},
    stuck ? h('p', { class: 'muted small' }, t('Your tags are saved only in this browser until you enter a creation code here.'), ' ',
      h('button', { class: 'small', onclick: async () => { if (await askCode()) syncList().catch(() => {}).finally(() => refreshProjects()) } }, t('Enter code'))) : null,
    all.length ? h('div', { class: 'tagbar', role: 'group', 'aria-label': t('Filter by tag') },
      ...all.map((tag) => h('button', { class: `utag${finding.tags.has(tag.id) ? ' on' : ''}`, 'data-tag': tag.id, 'aria-pressed': String(finding.tags.has(tag.id)), onclick: () => toggleFilter(tag.id) }, tag.name)),
      h('button', { class: 'addtag', onclick: () => manageTags(() => refreshProjects()) }, t('Manage tags…'))) : null)
}

function projectList(): HTMLElement {
  const list = projects.list()
  if (!list.length) return h('p', { class: 'muted' }, t('None yet. Projects you create or open from a link appear here.'))
  const bar = tagBar()
  const shownTags = new Map(list.map((r) => [r.project, tags.of(r.project)]))
  const shown = (r: { nick?: string; name: string }) => r.nick || (r.name && r.name !== UNTITLED ? r.name : t('Untitled project'))
  const dupes = new Set(list.map(shown).filter((n, i, all) => all.indexOf(n) !== i))
  const matching = list.filter((r) => found(r, shown(r), shownTags.get(r.project)!))
  const rows = matching.length ? h('ul', { class: 'recent' }, ...matching.map((r) => {
    const label = shown(r)
    const details: string[] = []
    if (r.nick && r.name) details.push(t('shared name “{{name}}”', { name: r.name }))
    if (r.docTitle && r.docTitle !== label && r.docTitle !== r.name) details.push(t('title “{{title}}”', { title: r.docTitle }))
    if (r.createdBy) details.push(t('started by {{name}}', { name: r.createdBy }))
    if (r.people?.length) details.push(t('with {{names}}', { names: `${r.people.slice(0, 4).join(', ')}${r.people.length > 4 ? ` +${r.people.length - 4}` : ''}` }))
    if (r.files) details.push(tn(r.files, '{{n}} file', '{{n}} files'))
    details.push(t('opened {{when}}', { when: whenOpened(r.at) }))
    return h('li', { class: dupes.has(label) ? 'dupe' : '' },
      h('div', { class: 'top' },
        h('a', { href: `#p=${r.project}`, class: 'pname' }, label),
        h('code', { class: 'pcode', title: t('Project code: derived from this project’s unique ID. Two projects with the same name always have different codes.') }, projectCode(r.project)),
        h('span', { class: `tag ${r.writable ? '' : 'ro'}` }, r.writable ? t('edit') : t('read-only')),
        r.atRisk || r.storage === 'relay' ? h('span', { class: 'tag warn', title: t('The server keeps no copy: this browser (and collaborators’ devices) hold the only copies. Clearing browser data deletes this one.') }, t('not on server')) : null,
        h('span', { class: 'spacer' }),
        h('button', { class: 'to-app small', title: t('Keep this project in a folder on this computer, using the TeXLoom app'), 'aria-label': t('Sync {{name}} to a folder…', { name: label }), onclick: () => openInApp(`${server}/#${r.fragment}`) }, t('Sync to a folder…')),
        h('button', { class: 'icon', title: t('Private label (only you see it)'), 'aria-label': t('Label {{name}}', { name: label }), onclick: () => {
          const v = prompt(t('Private label for “{{name}}” ({{code}}).\nOnly you see it, in this browser. Leave empty to use the shared name.', { name: r.name || t('this project'), code: projectCode(r.project) }), r.nick ?? '')
          if (v === null) return
          projects.update(r.project, { nick: v.trim() })
          refreshProjects()
        } }, '✎'),
        h('button', { class: 'icon', title: t('Make a copy'), 'aria-label': t('Make a copy of {{name}}…', { name: label }), onclick: () => {
          try {
            const keys = Keys.fromFragment('#' + r.fragment)
            if (keys) copyDialog({ name: label, by: prefs.name, server, keys })
          } catch (e) { alert(e) }
        } }, '⧉'),
        h('button', { class: 'icon forget', title: t('Remove…'), 'aria-label': t('Remove {{name}}…', { name: label }), onclick: async () => {
          let keys: Keys | null = null
          try { keys = Keys.fromFragment('#' + r.fragment) } catch {}
          const folder = await savedFolder(r.project).catch(() => undefined)
          removeDialog({ name: label, code: projectCode(r.project), owner: !!keys?.owner, onServer: !(r.atRisk || r.storage === 'relay'), folder: folder?.name ?? null },
            async (fromServer) => {
              if (fromServer && keys) {
                const rep = new Replica(keys, server, 'live')
                await rep.open()
                try { await rep.removeFromServer() } finally { await rep.close() }
              }
              await projects.forget(r.project)
              refreshProjects()
            })
        } }, '✕')),
      h('div', { class: 'details muted' }, details.join(' · ')),
      h('div', { class: 'tags' },
        ...shownTags.get(r.project)!.map((tag) => h('button', { class: `utag${finding.tags.has(tag.id) ? ' on' : ''}`, title: t('Show only projects tagged “{{name}}”', { name: tag.name }), onclick: () => toggleFilter(tag.id) }, tag.name)),
        h('button', { class: 'addtag', 'aria-label': t('Tags for {{name}}…', { name: label }), onclick: () => pickTags(r.project, label, () => refreshProjects()) }, t('+ Tag'))))
  })) : h('p', { class: 'muted' }, t('No projects match your search.'))
  return h('div', {}, bar, rows)
}

// ---- project view -----------------------------------------------------------------
async function openProject(keys: Keys) {
  // a personal link the owner revoked opens read-only
  try {
    if (keys.member && localStorage.getItem(`texloom.withdrawn.${keys.project}`)) keys = new Keys(keys.project, keys.key, null)
  } catch {}
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
  if (writable && !r.meta.get('name')) r.meta.set('name', UNTITLED)
  try {
    if (writable && localStorage.getItem(`texloom.newRelay.${keys.project}`)) {
      localStorage.removeItem(`texloom.newRelay.${keys.project}`)
      r.setStorage('relay') // chosen at creation: no server copy
    }
  } catch {}
  // a brand-new project: fill it from the chosen template (or open Import)
  let template: string | null = null
  try { template = localStorage.getItem(`texloom.newTemplate.${keys.project}`); localStorage.removeItem(`texloom.newTemplate.${keys.project}`) } catch {}
  // Only projects made here with New project: one opened from a link already exists, and
  // starter files added before its own arrive would clash with them (clashes.ts).
  if (template && writable && r.files.size === 0 && r.bin.size === 0 && !r.meta.get('seeded')) {
    const seed = () => {
      if (r.files.size || r.bin.size || r.meta.get('seeded')) return
      if (template === 'import') { importDialog(r, { fresh: true, onDone: () => tree.open(mainFile(r) ?? '') }); return }
      const t = TEMPLATES.find((x) => x.id === template) ?? TEMPLATES[0]
      applyTemplate(r, t, me.name)
      tree.open(t.main)
    }
    setTimeout(seed, 0)
  }

  // ---- components ----
  const index = new ProjectIndex(r)
  const gotoSource = (file: string, line: number, flash = false) => { layout.showEditor(); tree.open(file); setTimeout(() => editor.goto(line, 0, flash), 0) }
  const output = createOutput(r, writable, () => device, () => me.name, gotoSource)
  const editor = createEditorPane(r, writable, index, {
    onCompile: () => output.compile(),
    onSync: () => syncToPdf(),
    problems: (file) => output.problems().filter((p) => p.file === file),
    review: (file) => reviewExtension(r, file, (id) => { openReview(); review()?.focus(id) }),
    menu: {
      writable,
      review: addReview,
      showInPdf: () => syncToPdf(),
      search: (text) => revealSearch(text),
      ask: () => { layout.reveal('right'); showTab('ai') },
      file: () => editor.current(),
      ...menuTargets(r, index, gotoSource, (path) => { layout.showEditor(); tree.open(path) }),
    },
    addReference: (seed, insert) => referenceDialog(r, index, insert, seed),
    spell: () => ({ on: device.spell, lang: String(r.meta.get('lang') ?? '') }),
    mathPreview: () => device.mathPreview,
    checks: () => device.checks,
    onReview: addReview,
  })
  const tree = createFileTree(r, writable, (p) => {
    editor.open(p)
    r.awareness.setLocalStateField('file', p) // so collaborators can jump to where you are
    showFile(p)
  }, {
    preview: (p) => { preview.show(p); showInPanel(true); layout.reveal('right'); showTab('pdf') },
    insert: () => {
      const v = editor.view()
      if (!v || !isTexFile(editor.current())) return null
      return (text) => { v.dispatch(v.state.replaceSelection(text)); v.focus() }
    },
  })

  // "Import" (zip / arXiv) and "+ Reference" (DOI / arXiv): in the file tools
  if (writable) tree.el.querySelector('.tools')?.append(h('button', { title: t('Import files from a .zip (e.g. Overleaf) or an arXiv paper'), onclick: () => importDialog(r) }, t('Import')))
  if (writable) tree.el.querySelector('.tools')?.append(h('button', {
    title: t('Add a bibliography entry from a DOI or arXiv id'),
    onclick: () => referenceDialog(r, index, (key) => {
      const v = editor.view(), f = editor.current()
      if (v && f?.endsWith('.tex')) v.dispatch(v.state.replaceSelection(`\\cite{${key}}`))
    }),
  }, t('+ Reference')))

  /** Jump to a collaborator: open their file at their cursor. */
  function jumpTo(clientId: number) {
    const st = r.awareness.getStates().get(clientId)
    const file = typeof st?.file === 'string' ? st.file : null
    if (!file || !r.files.has(file)) return
    tree.open(file)
    const head = st?.cursor?.head
    if (head) {
      try {
        const abs = Y.createAbsolutePositionFromRelativePosition(Y.createRelativePositionFromJSON(head), r.doc)
        if (abs && abs.type === r.files.get(file)) { setTimeout(() => editor.gotoOffset(abs.index), 0); return }
      } catch {}
    }
  }
  function addReview(kind: 'comment' | 'suggestion') { if (writable) { openReview(); review()?.add(kind) } }
  function revealSearch(text?: string) { layout.reveal('left'); showLeft('search'); search.focus(text) }
  function syncToPdf() {
    const c = editor.context(), v = editor.view()
    if (!c?.file || !v) return
    layout.reveal('right')
    showInPanel(false)
    showTab('pdf')
    if (!output.showInPdf(c.file, v.state.doc.lineAt(v.state.selection.main.head).number)) alert(t('Compile first: the PDF has no SyncTeX data yet.'))
  }
  output.syncRequest = syncToPdf
  function syncToSource() {
    if (!output.showInSource()) alert(t('Compile first: the PDF has no SyncTeX data yet.'))
  }
  output.onProblems.push(() => editor.relint())
  // right-hand panel tabs; panels other than the PDF are created on first use
  type Tab = 'pdf' | 'ai' | 'review' | 'history'
  const preview = new FilePreview(r)
  let currentTab: Tab = 'pdf'
  let showingFile = false // the PDF tab is showing a file from the project, not the compiled PDF
  /** What the PDF tab shows: the compiled PDF, or a file from the project. */
  function showInPanel(file: boolean) {
    showingFile = file
    const seen = currentTab === 'pdf' && file
    preview.el.hidden = !seen
    output.pane.hidden = file
    tabBtns.pdf.textContent = file ? t('Preview') : t('PDF')
    syncArrows.hidden = currentTab !== 'pdf' || file
    if (seen) preview.visible()
  }
  preview.onBack = () => showInPanel(false)
  /** Where a file chosen in the list appears: text in the editor, and anything previewable in the PDF tab. */
  function showFile(p: string | null) {
    layout.closeDrawer()
    const previewable = !!p && isPreviewable(p), inEditor = !!p && r.files.has(p)
    if (previewable) preview.show(p)
    showInPanel(previewable)
    if (layout.narrow) { // one thing on screen at a time
      if (previewable && !inEditor) { layout.reveal('right'); showTab('pdf') }
      else if (p) layout.showEditor()
      return
    }
    if (!previewable || (layout.isOpen('right') && currentTab !== 'pdf' && inEditor)) return // it's in the editor anyway: leave that tab open
    layout.reveal('right')
    showTab('pdf')
  }
  const panes: Record<Tab, HTMLElement> = {
    pdf: h('div', { class: 'pane' }, output.pane, preview.el),
    ai: h('div', { class: 'pane', hidden: true }),
    review: h('div', { class: 'pane', hidden: true }),
    history: h('div', { class: 'pane', hidden: true }),
  }
  let reviewPanel: ReviewPanel | null = null
  const review = () => reviewPanel
  const revealAt = (file: string, from: number, to: number) => { layout.showEditor(); tree.open(file); setTimeout(() => editor.reveal(from, to), 0) }
  const openReview = () => { layout.reveal('right'); showTab('review') }
  const makers: Partial<Record<Tab, () => HTMLElement>> = {
    ai: () => createAssistantPanel({
      replica: r, writable, editorContext: editor.context,
      compile: () => output.compile(false), lastLog: output.lastLog, userName: () => me.name,
    }),
    review: () => (reviewPanel = createReviewPanel({
      replica: r, writable, userName: () => me.name, selection: () => editor.selection(),
      reveal: revealAt,
    })).el,
    history: () => createHistoryPanel({
      replica: r, writable, userName: () => me.name,
      showAuthors: (on) => editor.showAuthors(on), openFile: (p) => tree.open(p),
    }),
  }
  const tabBtns = {
    pdf: h('button', { class: 'tab active', onclick: () => showTab('pdf') }, t('PDF')),
    ai: h('button', { class: 'tab', onclick: () => showTab('ai') }, t('Assistant')),
    review: h('button', { class: 'tab', title: t('Comments and suggested changes'), onclick: () => showTab('review') }, t('Review')),
    history: h('button', { class: 'tab', onclick: () => showTab('history') }, t('History')),
  }
  // the Review tab shows how many comments and suggestions are open
  const countOpen = () => {
    let n = 0
    r.review.forEach((m) => { if (m instanceof Y.Map && !m.get('resolved')) n++ })
    tabBtns.review.textContent = n ? t('Review ({{n}})', { n }) : t('Review')
  }
  r.review.observeDeep(countOpen)
  setTimeout(countOpen, 0)
  function showTab(t: Tab) {
    for (const k of Object.keys(panes) as Tab[]) {
      panes[k].hidden = k !== t
      tabBtns[k].classList.toggle('active', k === t)
    }
    if (!panes[t].firstChild && makers[t]) panes[t].append(makers[t]!())
    currentTab = t
    showInPanel(showingFile)
    try { localStorage.setItem('texloom.tab', t) } catch {}
  }

  // left-hand tabs: files, search across files, outline with word count
  type LeftTab = 'files' | 'search' | 'outline'
  const search = createSearchPanel(r, writable, revealAt)
  const outlinePanel = createOutlinePanel(r, revealAt, () => {
    const s = editor.selection()
    return s && s.to > s.from ? countWords(blankNonProse(s.text)) : null
  })
  const leftPanes: Record<LeftTab, HTMLElement> = {
    files: h('div', { class: 'lpane' }, tree.el),
    search: h('div', { class: 'lpane', hidden: true }, search.el),
    outline: h('div', { class: 'lpane', hidden: true }, outlinePanel.el),
  }
  const leftBtns: Record<LeftTab, HTMLButtonElement> = {
    files: h('button', { class: 'tab active', onclick: () => showLeft('files') }, t('Files')),
    search: h('button', { class: 'tab', title: t('Search all files (⌘⇧F / Ctrl+Shift+F)'), onclick: () => showLeft('search') }, t('Search')),
    outline: h('button', { class: 'tab', title: t('Sections and word count'), onclick: () => showLeft('outline') }, t('Outline')),
  }
  function showLeft(t: LeftTab) {
    for (const k of Object.keys(leftPanes) as LeftTab[]) { leftPanes[k].hidden = k !== t; leftBtns[k].classList.toggle('active', k === t) }
    if (t === 'outline') outlinePanel.refresh()
    if (t === 'search') search.focus()
    try { localStorage.setItem('texloom.ltab', t) } catch {}
  }

  // ---- folder sync from the browser (Chromium) ----
  let folder: FolderSync | null = null
  const folderBtn = h('button', { class: 'folder wide-only', hidden: true, onclick: () => folderMenu() })
  const showFolder = () => {
    folderBtn.hidden = !folder && !pendingFolder
    folderBtn.textContent = folder ? `📁 ${folder.name}` : `📁 ${t('Reconnect folder')}`
    folderBtn.title = folder ? t('Synced with the folder “{{name}}” while this tab is open. {{status}}', { name: folder.name, status: folder.status() }) : t('Click to allow syncing with your folder again.')
  }
  // A collaborator's change wrote lines into the folder that ask to run programs. Harmless
  // for compiles here (TeX never runs programs in the browser), but local LaTeX tools
  // may follow them, so say so once per line.
  const programsNote = h('div', { class: 'risk', role: 'status', hidden: true })
  const toldPrograms = new Set<string>()
  function showPrograms(lines: ProgramLine[]) {
    const fresh = lines.filter((l) => !toldPrograms.has(`${l.path}\0${l.text}`))
    if (!fresh.length) return
    for (const l of fresh) toldPrograms.add(`${l.path}\0${l.text}`)
    programsNote.replaceChildren(
      h('div', {},
        h('b', {}, `⚠ ${t('Lines that run programs were written to your folder.')} `),
        t('If you compile that folder with your own LaTeX tools, check them first:'),
        h('ul', {}, ...fresh.slice(0, 6).map((l) => h('li', {},
          h('a', { href: '#', onclick: (e: Event) => { e.preventDefault(); gotoSource(l.path, l.line, true) } }, `${l.path}:${l.line}`),
          ` ${t('{{what}}:', { what: programWhat(l.what) })} `, h('code', {}, l.text)))),
        fresh.length > 6 ? h('div', { class: 'muted small' }, t('…and {{n}} more.', { n: fresh.length - 6 })) : null),
      h('button', { onclick: () => { programsNote.hidden = true } }, t('Dismiss')))
    programsNote.hidden = false
  }
  let pendingFolder: FileSystemDirectoryHandle | null = null
  async function linkFolder(dir: FileSystemDirectoryHandle, fresh: boolean) {
    if (fresh) {
      const there = await folderFiles(dir)
      const extra = there.filter((p) => !r.files.has(p) && !r.bin.has(p))
      if (extra.length && !confirm(tn(extra.length,
        '“{{name}}” has {{n}} file that isn’t in the project (e.g. {{examples}}). It’ll be added to the project, and the project’s files written into the folder. Continue?',
        '“{{name}}” has {{n}} files that aren’t in the project (e.g. {{examples}}). They’ll be added to the project, and the project’s files written into the folder. Continue?',
        { name: dir.name, examples: extra.slice(0, 3).join(', ') }))) return
    }
    folder?.stop()
    folder = await startFolderSync(r, dir)
    folder.onChange = showFolder
    folder.onPrograms = showPrograms
    pendingFolder = null
    await rememberFolder(keys.project, dir)
    showFolder()
  }
  async function folderMenu() {
    if (!folder && pendingFolder) {
      if ((await folderPermission(pendingFolder, true)) === 'granted') await linkFolder(pendingFolder, false)
      return
    }
    if (folder && confirm(t('Stop syncing with “{{name}}”? The folder keeps its files.', { name: folder.name }))) {
      folder.stop(); folder = null; await rememberFolder(keys.project, null); showFolder()
    }
  }
  const chooseFolder = async () => { const d = await pickFolder(); if (d) await linkFolder(d, true) }
  if (folderSyncSupported() && writable) {
    savedFolder(keys.project).then(async (d) => {
      if (!d) return
      if ((await folderPermission(d, false)) === 'granted') await linkFolder(d, false)
      else { pendingFolder = d; showFolder() }
    }).catch(() => {})
  }

  // ---- header ----
  const status = h('span', { class: 'status', role: 'status' })
  const peers = h('span', { class: 'peers' })
  const title = h('input', { class: 'title', readonly: !writable, 'aria-label': t('Project name'), onchange: (e: any) => r.meta.set('name', e.target.value) })
  const syncNow = async () => { syncBtn.setAttribute('disabled', ''); await r.syncNow(); syncBtn.removeAttribute('disabled') }
  const syncBtn = h('button', { class: 'wide-only', onclick: syncNow }, t('Sync now'))
  const zipBtn = h('button', { class: 'wide-only', title: t('Download all files as a .zip'), onclick: () => downloadZip() }, `⤓ ${t('Source')}`)
  const copyProject = () => copyDialog({ name: title.value, by: prefs.name, server, replica: r })
  const copyBtn = h('button', { class: 'wide-only', title: t('Start a new project from this one’s files'), onclick: copyProject }, `⧉ ${t('Make a copy')}`)
  const shareProject = () => shareDialog(r, server, me.name)
  const shareBtn = h('button', { class: 'wide-only', onclick: shareProject }, t('Share'))
  const settingsBtn = h('button', { class: 'wide-only', title: t('Settings'), onclick: () => openSettings() }, '⚙')
  // Each is a button in wide windows and an item under ⋯ in small ones.
  const headerActions: { btn: HTMLElement; label: () => string; run: () => void }[] = [
    { btn: syncBtn, label: () => t('Sync now'), run: syncNow },
    { btn: zipBtn, label: () => `⤓ ${t('Source')}`, run: () => downloadZip() },
    { btn: copyBtn, label: () => `⧉ ${t('Make a copy')}`, run: copyProject },
    { btn: folderBtn, label: () => folderBtn.textContent ?? '', run: () => folderMenu() },
    { btn: shareBtn, label: () => t('Share'), run: shareProject },
    { btn: settingsBtn, label: () => t('Settings'), run: () => openSettings() },
  ]
  const leftBtn = h('button', { class: 'pane-toggle', onclick: () => layout.toggle('left') }, '☰')
  const allProjects = () => { location.href = '/' }
  const moreBtn = h('button', { class: 'narrow-only', title: t('More'), 'aria-label': t('More'), onclick: (e: any) => {
    const b = e.currentTarget.getBoundingClientRect()
    openContextMenu(b.right, b.bottom + 4, menuItems(...headerActions.map((a) => !a.btn.hidden && ([a.label(), a.run] as MenuItem)), [t('All projects'), allProjects]))
  } }, '⋯')
  const rightBtn = h('button', { class: 'pane-toggle', onclick: () => layout.toggle('right') }, '▥')
  const splitL = h('div', { class: 'split' })
  // Overleaf-style arrows on the editor/PDF divider: jump between the source and the PDF
  const arrow = (label: string, title: string, run: () => void) => h('button', {
    title, 'aria-label': title.split(' (')[0],
    onpointerdown: (e: Event) => e.stopPropagation(), ondblclick: (e: Event) => e.stopPropagation(), onclick: run,
  }, label)
  const syncArrows = h('div', { class: 'syncarrows' },
    arrow('→', t('Show the cursor’s line in the PDF (⌘⌥→ / Ctrl+Alt+→)'), () => syncToPdf()),
    arrow('←', t('Go to the source of the spot you last clicked in the PDF, or the middle of the view (⌘⌥← / Ctrl+Alt+←). Double-click or right-click the PDF also works.'), () => syncToSource()))
  const splitR = h('div', { class: 'split' }, syncArrows)
  const mainEl = h('main', {})
  const risk = riskBanner(r, writable, () => openSettings())

  /** On-screen keyboards hide the characters LaTeX is made of. */
  function latexKeys() {
    return h('div', { class: 'keybar', role: 'toolbar', 'aria-label': t('LaTeX characters') }, ...['\\', '{', '}', '$', '%', '_', '^', '&', '[', ']', '~'].map((key) => h('button', {
      tabindex: -1,
      onmousedown: (e: Event) => e.preventDefault(), // keep the editor's focus and on-screen keyboard
      onclick: () => { const v = editor.view(); if (v) { v.dispatch(v.state.replaceSelection(key)); v.focus() } },
    }, key)))
  }
  // Keep the toolbar and the character bar above the on-screen keyboard.
  const vv = window.visualViewport
  if (vv && matchMedia('(pointer: coarse)').matches) {
    const fit = () => { document.documentElement.style.setProperty('--app-h', `${vv.height}px`); scrollTo(0, 0) }
    vv.addEventListener('resize', fit)
    fit()
  }

  const app = $('#app')
  app.className = 'project'
  app.append(
    h('header', {},
      leftBtn,
      h('a', { href: '/', class: 'brand', title: t('All projects'), onclick: (e: Event) => { e.preventDefault(); location.href = '/' } }, brand()),
      title, writable ? null : h('span', { class: 'badge' }, t('read-only')),
      h('span', { class: 'spacer' }), peers, status,
      ...headerActions.map((a) => a.btn),
      output.compileBtn,
      rightBtn,
      moreBtn,
    ),
    risk.el,
    mainEl,
  )
  mainEl.append(
    h('aside', {}, h('div', { class: 'tabs' }, leftBtns.files, leftBtns.search, leftBtns.outline), leftPanes.files, leftPanes.search, leftPanes.outline),
    splitL,
    h('div', { class: 'editor-area' }, editor.el, writable ? latexKeys() : null),
    splitR,
    h('section', { class: 'right' }, h('div', { class: 'tabs' }, tabBtns.pdf, tabBtns.ai, tabBtns.review, tabBtns.history), panes.pdf, panes.ai, panes.review, panes.history),
  )
  const layout = setupLayout(mainEl, splitL, splitR)
  layout.onChange = () => {
    const left = layout.isOpen('left'), right = layout.isOpen('right')
    leftBtn.title = left ? t('Hide the file list') : t('Show the file list')
    rightBtn.textContent = !layout.narrow ? '▥' : right ? t('Editor') : t('PDF')
    rightBtn.title = right ? t('Hide the PDF and assistant') : t('Show the PDF and assistant')
    leftBtn.classList.toggle('off', !left)
    rightBtn.classList.toggle('off', !right)
    leftBtn.setAttribute('aria-pressed', String(left))
    rightBtn.setAttribute('aria-pressed', String(right))
  }
  const savedLeft = localStorage.getItem('texloom.ltab') as LeftTab | null
  if (savedLeft === 'outline') showLeft('outline')
  const savedTab = localStorage.getItem('texloom.tab') as Tab | null
  if (savedTab && savedTab in panes && savedTab !== 'pdf') showTab(savedTab)
  tree.start()

  // ⌘S / Ctrl+S compiles from anywhere (the editor handles it itself when focused)
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === 'f') {
      e.preventDefault()
      layout.reveal('left')
      showLeft('search')
      const sel = editor.selection()
      if (sel && sel.to > sel.from && !sel.text.includes('\n')) search.focus(sel.text)
      return
    }
    if ((e.metaKey || e.ctrlKey) && e.altKey && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      // ⌘⌥→ in the editor is handled there; from elsewhere (e.g. the PDF) both directions work
      if (e.key === 'ArrowRight' && editor.view()?.hasFocus) return
      e.preventDefault()
      if (e.key === 'ArrowLeft') syncToSource(); else syncToPdf()
      return
    }
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 's') {
      e.preventDefault()
      output.compile()
    }
  })
  // stop the browser from opening files dropped outside the file tree
  app.addEventListener('dragover', (e) => e.preventDefault())
  app.addEventListener('drop', (e) => e.preventDefault())

  function openSettings() {
    settingsDialog(r, keys.link(server, writable), applyDevice, folderSyncSupported() && writable ? chooseFolder : null, async () => {
      removeDialog({ name: projects.list().find((p) => p.project === keys.project)?.nick || r.meta.get('name') || t('Untitled project'), code: projectCode(keys.project),
        owner: keys.owner, onServer: r.storage !== 'relay', pending: r.pending, folder: folder?.name ?? null },
        async (fromServer) => {
          if (fromServer) await r.removeFromServer()
          removing = true
          await r.close()
          await projects.forget(keys.project)
          location.href = '/'
        })
    })
  }
  function applyDevice(d: Device) {
    device = d
    r.setSyncMode(d.sync)
    syncBtn.hidden = d.sync !== 'manual'
    output.compileBtn.hidden = d.compile === 'none'
    editor.refreshSpell()
    editor.relint()
  }
  r.meta.observe((e) => { if (e.keysChanged.has('lang')) editor.refreshSpell() })

  async function downloadZip() {
    zipBtn.setAttribute('disabled', '')
    try {
      const { zip, missing } = await projectZip(r)
      saveFile(`${fileBase(r)}.zip`, zip, 'application/zip')
      if (missing.length) alert(t('These files weren’t included because their content isn’t available yet (offline, and no collaborator online has them):\n\n{{files}}', { files: missing.join('\n') }))
    } finally { zipBtn.removeAttribute('disabled') }
  }

  // ---- status / presence ----
  let lastRisk: boolean | null = null
  const renderStatus = () => {
    status.className = `status ${r.status}`
    const unsent = r.pending ? ` · ${tn(r.pending, '{{n}} unsent', '{{n}} unsent')}` : ''
    const relay = r.storage === 'relay' ? ` · ${tn(r.peers, 'relay-only, {{n}} peer online', 'relay-only, {{n}} peers online')}` : ''
    status.textContent =
      r.status === 'online' ? (r.pending ? t('syncing {{n}}…', { n: r.pending }) : t('saved')) + relay
      : r.status === 'connecting' ? t('connecting…')
      : r.status === 'paused' ? t('manual sync') + unsent
      : t('offline{{unsent}} (changes kept locally)', { unsent })
    status.title = r.storage === 'relay' ? t('The server keeps no copy: changes reach collaborators only while you are both online.') : t('Changes are stored encrypted on the server.')
    if (r.storeProblem && r.storage !== 'relay' && r.status === 'online') {
      status.textContent = t('not saved on server')
      status.className = 'status warn'
      status.title = t('Changes reach collaborators who are online, and stay in this browser, but the server isn’t storing them. See the warning above.')
    }
    risk.render()
    const atRisk = !!r.storeProblem || r.storage === 'relay'
    if (!removing && atRisk !== lastRisk) { lastRisk = atRisk; projects.update(keys.project, { atRisk }) }
  }
  let toldWithdrawn = false
  const movedNote = h('div', { class: 'risk', role: 'status', hidden: true })
  const renderMoved = () => {
    const m = r.meta.get('movedAt') // only the owner can sign it: an editor can't fake the banner
    const at = m && typeof m === 'object' && keys.ownerSigned(`moved:${m.at}`, m.s) ? Number(m.at) : 0
    movedNote.hidden = !at
    if (at) movedNote.replaceChildren(h('span', {}, h('b', {}, '⚠ '),
      t('The owner moved this project to new keys on {{date}}. This copy no longer receives their changes: ask them for the new link.', { date: new Date(at).toLocaleDateString(locale) })))
  }
  risk.el.after(movedNote)
  movedNote.after(programsNote)
  // files created under the same name by two people apart: both kept (clashes.ts)
  const clashNote = h('div', { class: 'risk', role: 'status', hidden: true })
  const clashes: Clash[] = []
  const renderClashes = () => {
    clashNote.hidden = !clashes.length
    const open = (p: string) => h('button', { onclick: () => tree.open(p) }, p)
    clashNote.replaceChildren(
      h('span', {}, h('b', {}, '⚠ '), clashes.length > 1 ? t('Files were created under the same names on two devices while apart. Both versions are kept:')
        : t('A file was created under the same name on two devices while apart. Both versions are kept:'), ' ',
        ...clashes.flatMap((c, i) => [i ? '; ' : '', ...tNodes('{{path}} ({{who}}’s) and {{copy}} (this device’s)', { path: open(c.path), who: c.by ?? t('the other device'), copy: open(c.copy) })]),
        '. ', t('Merge them and delete the copy when you’re done.')),
      h('span', { class: 'actions' },
        h('button', { title: t('Hide'), 'aria-label': t('Hide this warning'), onclick: () => { clashes.length = 0; renderClashes() } }, '✕')))
  }
  programsNote.after(clashNote)
  watchClashes(r, (c) => { clashes.push(c); renderClashes() })
  r.meta.observe(renderMoved); renderMoved()
  r.onStatus = () => {
    renderStatus()
    if (r.withdrawn && !toldWithdrawn) {
      toldWithdrawn = true
      alert(t('The owner has revoked your edit link. The project will reopen read-only.'))
      location.reload()
    }
  }
  renderStatus()
  r.meta.observe(() => renderStatus()) // storage mode can change from Settings or a collaborator
  applyDevice(device)

  r.awareness.on('change', () => {
    const others = [...r.awareness.getStates().entries()].filter(([id, s]) => id !== r.doc.clientID && s.user)
    peers.replaceChildren(...others.map(([id, s]) => {
      const name = String(s.user.name).slice(0, 64)
      const where = typeof s.file === 'string' ? ` · ${String(s.file).slice(0, 100)}` : ''
      return h('button', { class: 'peer', style: `background:${/^#[0-9a-f]{6}$/i.test(s.user.color) ? s.user.color : '#888'}`,
        title: t('{{name}}{{where}} — click to go to where they are', { name, where }), 'aria-label': t('Go to {{name}}', { name }), onclick: () => jumpTo(id) }, name.slice(0, 2))
    }))
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
    const name: string = r.meta.get('name') || ''
    title.value = name === UNTITLED ? t('Untitled project') : name
    document.title = `${title.value} — ${APP}`
    if (!removing) projects.update(keys.project, {
      fragment: keys.fragment(), writable, at: Date.now(), name,
      createdBy: r.meta.get('createdBy'), storage: r.storage,
    })
    title.title = t('Project name (shared with collaborators) · code {{code}}', { code: projectCode(keys.project) })
  }
  r.meta.observe(renderMeta); renderMeta()
  output.compileBtn.addEventListener('click', () => { showInPanel(false); if (layout.narrow) { layout.reveal('right'); showTab('pdf') } }) // the result is what you want to see

  // debugging handle, only when enabled with localStorage['texloom.debug'] = '1'
  try { if (localStorage.getItem('texloom.debug') === '1') (window as any).texloom = { replica: r, Y, output, pdfjs, SyncTex, editor, startFolderSync } } catch {}
}

