// A project, open: the editor, the PDF, the panels beside them, and keeping it in step.
import { warmUp } from './compile'
import { listenForShortcutsKey } from './shortcuts'
import * as Y from 'yjs'
import { Keys } from './crypto'
import { Replica } from './replica'
import { projects, projectCode, latexTitle } from './projects'
import { removeDialog } from './remove'
import { prefs, setupDialog, settingsDialog, type Device, type SettingsTab } from './settings'
import { createAssistantPanel, type AssistantStatus } from './assistant/panel'
import { setupLayout } from './layout'
import { riskBanner } from './storagenotice'
import { $, debounce, errorText, h, saveFile, sleep } from './dom'
import { icon } from './icons'
import { t, tn, tNodes, locale } from './i18n'
import { fileBase, mainFile, projectZip } from './projectfiles'
import { submissionDialog } from './submitdialog'
import { keepLF } from './lineends'
import { createFileTree } from './filetree'
import { menuItems, openContextMenu, type MenuItem } from './contextmenu'
import { menuTargets } from './editormenu'
import { FilePreview, isPreviewable } from './preview'
import { isTexFile } from './projectfiles'
import { copyDialog } from './copyproject'
import { createSymbolPalette } from './symbolpalette'
import { tags } from './tags'
import { pickTags } from './tagdialogs'
import { links as zoteroLinks, refresh as zoteroRefresh, zoteroAccount } from './zotero'
import { COLORS, createEditorPane } from './editor'
import { createOutput } from './output'
import { pdfjs } from './pdfview'
import { SyncTex } from './synctex'
import type { ProgramLine } from './texscan'
import { ProjectIndex } from './latex'
import { autoSave, createHistoryPanel } from './history'
import { referenceDialog } from './references'
import { watchClashes, type Clash } from './clashes'
import { watchMerges } from './merges'
import { checking, wellFormed } from './invariants'
import { sharedPos, type SharedPos } from './positions'
import { runGitHub } from './githubui'
import { runDropbox } from './dropboxui'
import { shareDialog } from './sharing'
import { awayServer } from './servers'
import { folderFiles, folderPermission, folderSyncSupported, pickFolder, rememberFolder, savedFolder, startFolderSync, type FolderSync } from './foldersync'
import { applyTemplate, importDialog, TEMPLATES } from './templates'
import { createOutlinePanel, createSearchPanel, blankNonProse, countWords } from './navigate'
import { allItems, createReviewPanel, reviewExtension, type ReviewPanel } from './review'
import { mentionsMe } from './mention'
import { suggestMode } from './suggestmode'
import { quiet, quieted } from './quiet'
import { openedByApp, appCompiles, inAppKey, APP, UNTITLED, brand, programWhat, onRefresh } from './appinfo'
import { getString, setString, takeString } from './store'
import { confirmFetch } from './zoteroui'

// ---- project view -----------------------------------------------------------------
/** Open a project, syncing with `server`: this app's own, or the project's if it lives elsewhere. */
export async function openProject(keys: Keys, server: string) {
  // a personal link the owner revoked opens read-only
  if (keys.member && getString(`texloom.withdrawn.${keys.project}`)) keys = new Keys(keys.project, keys.key, null)
  if (openedByApp) setString(inAppKey(keys.project), '1')
  let device: Device = prefs.device(keys.project)
  const r = new Replica(keys, server, device.sync)
  await r.open()
  const writable = keys.writable && !r.deletedAt // a project its owner deleted for everyone stays as it was
  let removing = false // set while deleting, so the page doesn't re-save it to "recent"
  if (!device.setup) {
    if (r.syncMode === 'live') await sleep(800) // fetch the name first
    device = await setupDialog(r, keys.link(server, writable), openedByApp, appCompiles)
    r.setSyncMode(device.sync)
  }
  const me = { name: prefs.name || `anon-${r.doc.clientID % 1000}`, color: COLORS[r.doc.clientID % COLORS.length] }
  r.awareness.setLocalStateField('user', { ...me, colorLight: me.color + '33' })
  if (writable && r.clients.get(String(r.doc.clientID)) !== me.name) r.clients.set(String(r.doc.clientID), me.name) // for "who wrote this"
  autoSave(r, () => me.name)
  const named = takeString(`texloom.newName.${keys.project}`) // the name given in Create project
  if (named && writable && !r.meta.get('name')) r.meta.set('name', named)
  if (writable && !r.meta.get('name')) r.meta.set('name', UNTITLED)
  if (writable && takeString(`texloom.newRelay.${keys.project}`)) r.setStorage('relay') // chosen at creation: no server copy
  // a brand-new project: fill it from the chosen template (or open Import)
  const template = takeString(`texloom.newTemplate.${keys.project}`)
  // Only projects made here with Create project: one opened from a link already exists, and
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
  const output = createOutput(r, writable, () => device, () => me.name, gotoSource, () => tree.current())
  suggestMode.start(keys.project, writable, () => me.name) // before the editor, which reads it
  const editor = createEditorPane(r, writable, index, {
    onCompile: () => output.compile(),
    onSync: () => syncToPdf(),
    problems: (file) => output.problems().filter((p) => p.file === file),
    review: (file, map) => reviewExtension(r, file, (id) => { openReview(); review()?.focus(id) }, map),
    menu: {
      writable,
      review: addReview,
      showInPdf: () => syncToPdf(),
      search: (text) => revealSearch(text),
      symbols: () => showSymbols(),
      ask: () => { layout.reveal('right'); showTab('ai') },
      file: () => editor.current(),
      ...menuTargets(r, index, gotoSource, (path) => { layout.showEditor(); tree.open(path) }),
    },
    addReference: (seed, insert) => referenceDialog(r, index, insert, seed),
    spell: () => ({ on: device.spell, lang: String(r.meta.get('lang') ?? '') }),
    mathPreview: () => device.mathPreview,
    checks: () => device.checks,
    userName: () => me.name,
  })
  const tree = createFileTree(r, writable, (p) => {
    editor.open(p)
    r.awareness.setLocalStateField('file', p) // so collaborators can jump to where you are
    showFile(p)
  }, {
    preview: (p) => { preview.show(p); showInPanel(true); layout.reveal('right'); showTab('pdf') },
    compile: (p) => { showInPanel(false); layout.reveal('right'); showTab('pdf'); output.compile(true, false, p) },
    zotero: (p) => (writable && zoteroLinks(r)[p] && zoteroAccount.get()
      ? () => { zoteroRefresh(r, p, me.name, confirmFetch).catch((e) => alert(errorText(e))) } : null),
    insert: () => {
      const v = editor.view()
      if (!v || !isTexFile(editor.current())) return null
      return (text) => { v.dispatch(v.state.replaceSelection(text)); v.focus() }
    },
  })

  // a reference (DOI / arXiv) and an import (zip / arXiv): in the file list's New menu
  tree.addNew(t('Reference…'), () => referenceDialog(r, index, (key) => {
    const v = editor.view(), f = editor.current()
    if (v && f?.endsWith('.tex')) v.dispatch(v.state.replaceSelection(`\\cite{${key}}`))
  }))
  tree.addNew(t('Import a .zip (e.g. Overleaf) or arXiv paper…'), () => importDialog(r))

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
        if (abs && abs.type === r.files.get(file)) { setTimeout(() => editor.gotoOffset(sharedPos(abs.index)), 0); return } // a Yjs position
      } catch (e) { quiet('a saved cursor that no longer fits the file: falls back below', e) }
    }
  }
  /** Comment on the selection, or suggest changes to it: suggest mode, keeping the selection to type over. */
  function addReview(kind: 'comment' | 'suggestion') {
    if (!writable) return
    if (kind === 'comment') { openReview(); review()?.addComment(); return }
    if (!suggestMode.on()) suggestMode.set(true)
    editor.view()?.focus()
  }
  function revealSearch(text?: string) { layout.reveal('left'); showLeft('search'); search.focus(text) }
  function showSymbols() { layout.reveal('left'); showLeft('symbols') }
  function syncToPdf() {
    const c = editor.context(), v = editor.view()
    if (!c?.file || !v) return
    layout.reveal('right')
    showInPanel(false)
    showTab('pdf')
    if (!output.showInPdf(c.file, c.line)) alert(t('Compile first: the PDF has no SyncTeX data yet.'))
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
  let layoutReady = false // the layout below is set up (what the PDF being on screen depends on)
  /** The output downloads new PDFs only while the compiled PDF is on screen: the PDF tab (not a file preview) in an open
   *  panel, in a window that someone can see. */
  function updateLooking() {
    if (layoutReady) output.looking(layout.isOpen('right') && currentTab === 'pdf' && !showingFile && !document.hidden)
  }
  document.addEventListener('visibilitychange', updateLooking)
  let pageTitle = APP, titleDot = false
  /** The browser tab's title; with a dot when the assistant has news (see markAssistant). */
  const setTitle = (dot = titleDot) => { titleDot = dot; document.title = (dot ? '● ' : '') + pageTitle }
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
    updateLooking()
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
  const revealAt = (file: string, from: SharedPos, to: SharedPos) => { layout.showEditor(); tree.open(file); setTimeout(() => editor.reveal(from, to), 0) }
  const openReview = () => { layout.reveal('right'); showTab('review') }
  const makers: Partial<Record<Tab, () => HTMLElement>> = {
    ai: () => createAssistantPanel({
      replica: r, writable, editorContext: editor.context,
      compile: () => output.compile(false), lastLog: output.lastLog, userName: () => me.name,
      link: (write) => keys.link(server, write && writable),
      onContextChange: editor.onContextChange,
      status: (s) => { aiStatus = s; markAssistant() },
    }),
    review: () => (reviewPanel = createReviewPanel({
      replica: r, writable, userName: () => me.name, selection: () => editor.selection(), suggesting: suggestMode,
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
  // and, with an @, how many of those mention you (issue #3): click it to see just those
  const countOpen = () => {
    const open = allItems(r).filter((i) => !i.resolved)
    const n = open.length, mine = open.filter((i) => mentionsMe(i, me.name)).length
    tabBtns.review.textContent = n ? t('Review ({{n}})', { n }) : t('Review')
    tabBtns.review.title = mine ? tn(mine, '{{n}} open comment mentions you', '{{n}} open comments mention you') : t('Comments and suggested changes')
    if (mine) tabBtns.review.append(h('span', { class: 'mention-badge', onclick: (e: Event) => { e.stopPropagation(); showTab('review'); reviewPanel?.showMentions() } }, `@${mine}`))
  }
  r.review.observeDeep(countOpen)
  setTimeout(countOpen, 0)
  // The Assistant tab says what the assistant is up to while another tab is open: a pulsing dot while it
  // works, and a slow glow once it has answered or waits for an edit to be applied or rejected. The
  // browser tab's title gets a dot too, for someone who went to another window meanwhile.
  let aiStatus: AssistantStatus = null
  function markAssistant() {
    const seen = currentTab === 'ai' && !document.hidden
    if (aiStatus === 'done' && seen) aiStatus = null // answered, and looked at
    const news = (aiStatus === 'done' || aiStatus === 'ask') && !seen
    tabBtns.ai.classList.toggle('ai-busy', aiStatus === 'busy' && currentTab !== 'ai')
    tabBtns.ai.classList.toggle('ai-news', news)
    tabBtns.ai.classList.toggle('ai-ask', news && aiStatus === 'ask')
    tabBtns.ai.title = aiStatus === 'busy' ? t('The assistant is working…') : !news ? '' :
      aiStatus === 'ask' ? t('The assistant is waiting for you to apply or reject a change') : t('The assistant has answered')
    setTitle(news)
  }
  document.addEventListener('visibilitychange', markAssistant)
  function showTab(t: Tab) {
    for (const k of Object.keys(panes) as Tab[]) {
      panes[k].hidden = k !== t
      tabBtns[k].classList.toggle('active', k === t)
    }
    if (!panes[t].firstChild && makers[t]) panes[t].append(makers[t]!())
    currentTab = t
    markAssistant()
    showInPanel(showingFile)
    setString('texloom.tab', t)
  }

  // left-hand tabs: files, search across files, outline with word count, symbols
  type LeftTab = 'files' | 'search' | 'outline' | 'symbols'
  const search = createSearchPanel(r, writable, revealAt)
  const outlinePanel = createOutlinePanel(r, revealAt, () => {
    const s = editor.selection()
    return s && s.to > s.from ? countWords(blankNonProse(s.text)) : null
  })
  const palette = createSymbolPalette({ r, view: () => editor.view(), file: () => editor.current(), inserted: () => layout.closeDrawer() })
  const leftPanes: Record<LeftTab, HTMLElement> = {
    files: h('div', { class: 'lpane' }, tree.el),
    search: h('div', { class: 'lpane', hidden: true }, search.el),
    outline: h('div', { class: 'lpane', hidden: true }, outlinePanel.el),
    symbols: h('div', { class: 'lpane', hidden: true }, palette.el),
  }
  const leftBtns: Record<LeftTab, HTMLButtonElement> = {
    files: h('button', { class: 'tab active', onclick: () => showLeft('files') }, t('Files')),
    search: h('button', { class: 'tab', title: t('Search all files (⌘⇧F / Ctrl+Shift+F)'), onclick: () => showLeft('search') }, t('Search')),
    outline: h('button', { class: 'tab', title: t('Sections and word count'), onclick: () => showLeft('outline') }, t('Outline')),
    symbols: h('button', { class: 'tab symtab', hidden: !writable, title: t('Insert a symbol at the cursor (⌘⌥S / Ctrl+Alt+S)'), 'aria-label': t('Symbols'), onclick: () => showLeft('symbols') }, 'Ω'),
  }
  function showLeft(t: LeftTab) {
    for (const k of Object.keys(leftPanes) as LeftTab[]) { leftPanes[k].hidden = k !== t; leftBtns[k].classList.toggle('active', k === t) }
    if (t === 'outline') outlinePanel.refresh()
    if (t === 'search') search.focus()
    if (t === 'symbols') palette.refresh()
    setString('texloom.ltab', t)
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
    }).catch((e) => quiet('the saved folder couldn’t be found again: the Folder button links one', e))
  }

  // ---- header ----
  const status = h('span', { class: 'status', role: 'status' })
  const peers = h('span', { class: 'peers' })
  const title = h('input', { class: 'title', readonly: !writable, 'aria-label': t('Project name'), onchange: (e: any) => r.meta.set('name', e.target.value) })
  const syncNow = async () => { syncBtn.setAttribute('disabled', ''); await r.syncNow(); syncBtn.removeAttribute('disabled') }
  const syncBtn = h('button', { class: 'wide-only', onclick: syncNow }, t('Sync now'))
  const zipBtn = h('button', { class: 'wide-only', title: t('Download all files as a .zip'), onclick: () => downloadZip() }, icon('download'), t('Source'))
  const submitBtn = h('button', { class: 'wide-only', title: t('Download a .zip for arXiv or a journal: only the files the document uses, with the compiled bibliography'), onclick: () => submissionDialog(r) }, icon('download'), t('arXiv / journal')) // (was “Submission”, which didn't say what for)
  const copyProject = () => copyDialog({ name: title.value, by: prefs.name, server, replica: r })
  const copyBtn = h('button', { class: 'wide-only', title: t('Start a new project from this one’s files'), 'aria-label': t('Make a copy'), onclick: copyProject }, icon('copy'), h('span', { class: 'lbl' }, t('Make a copy')))
  const tagsLabel = () => { const n = tags.of(keys.project).length; return n ? t('Tags ({{n}})', { n }) : t('Tags') }
  const showTags = () => {
    tagsBtn.textContent = tagsLabel()
    tagsBtn.title = tags.of(keys.project).map((x) => x.name).join(', ') || t('Your own tags for this project. Collaborators don’t see them.')
  }
  const editTags = () => pickTags([keys.project], t('Tags for “{{name}}”', { name: title.value }), showTags)
  const tagsBtn = h('button', { class: 'wide-only', onclick: editTags }, tagsLabel())
  onRefresh(showTags) // tags changed on another device
  showTags()
  const shareProject = () => shareDialog(r, server, me.name)
  const shareBtn = h('button', { class: 'wide-only', hidden: !!r.deletedAt, onclick: shareProject }, t('Share')) // a deleted project's links open nothing
  const settingsBtn = h('button', { class: 'wide-only gearbtn', title: t('Settings'), 'aria-label': t('Settings'), onclick: () => openSettings() }, icon('gear'))
  // Each is a button in wide windows and an item under ⋯ in small ones.
  const headerActions: { btn: HTMLElement; label: () => string; run: () => void }[] = [
    { btn: syncBtn, label: () => t('Sync now'), run: syncNow },
    { btn: zipBtn, label: () => t('Download the source (.zip)'), run: () => downloadZip() },
    { btn: submitBtn, label: () => t('Download for arXiv or a journal (.zip)'), run: () => submissionDialog(r) },
    { btn: copyBtn, label: () => t('Make a copy'), run: copyProject },
    { btn: tagsBtn, label: tagsLabel, run: editTags },
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
  const risk = riskBanner(r, writable, () => openSettings('storage'))

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
    h('aside', {}, h('div', { class: 'tabs' }, leftBtns.files, leftBtns.search, leftBtns.outline, leftBtns.symbols), leftPanes.files, leftPanes.search, leftPanes.outline, leftPanes.symbols),
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
    updateLooking()
  }
  layoutReady = true
  updateLooking()
  const savedLeft = getString('texloom.ltab') as LeftTab | null
  if (savedLeft === 'outline' || (savedLeft === 'symbols' && writable)) showLeft(savedLeft)
  const savedTab = getString('texloom.tab') as Tab | null
  if (savedTab && savedTab in panes && savedTab !== 'pdf') showTab(savedTab)
  // the desktop app's "Review in browser" and mention notices (?tab=review, &mentions=1 for just yours)
  const asked = new URLSearchParams(location.search)
  if (asked.get('tab') === 'review') {
    layout.reveal('right')
    showTab('review')
    if (asked.get('mentions') === '1') review()?.showMentions()
  }
  tree.start()

  listenForShortcutsKey() // ? lists them all
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
      if (e.defaultPrevented) return // the editor's own ⌘S already compiled: a second compile at once used to hang the first
      e.preventDefault()
      output.compile()
    }
  })
  // stop the browser from opening files dropped outside the file tree
  app.addEventListener('dragover', (e) => e.preventDefault())
  app.addEventListener('drop', (e) => e.preventDefault())

  function openSettings(tab?: SettingsTab) {
    settingsDialog(r, keys.link(server, writable), applyDevice, folderSyncSupported() && writable ? chooseFolder : null, async () => {
      removeDialog({ name: projects.list().find((p) => p.project === keys.project)?.nick || r.meta.get('name') || t('Untitled project'), code: projectCode(keys.project),
        owner: keys.owner && !r.deletedAt, onServer: r.storage !== 'relay' && !r.deletedAt, pending: r.pending, folder: folder?.name ?? null },
        async (scope) => {
          removing = true
          try {
            if (scope === 'everyone') await r.deleteForEveryone()
            else if (scope === 'server') await r.removeFromServer()
          } catch (e) { removing = false; throw e }
          await r.close()
          await projects.forget(keys.project)
          location.href = '/'
        })
    }, tab)
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
  // "syncing" only when changes are held up: some are waiting and none has been confirmed for a
  // while. Typing sends a change a keystroke, each confirmed in a moment: showing every one made the
  // status flicker between "saved" and "syncing" all the time.
  const SLOW = 2000 // ms without a confirmation before waiting changes are shown
  let lastPending = 0, progressAt = Date.now(), slowTimer: ReturnType<typeof setTimeout> | undefined
  const heldUp = () => {
    if (!r.pending || r.pending < lastPending || (lastPending === 0 && r.pending > 0)) progressAt = Date.now() // confirmed, or newly waiting
    lastPending = r.pending
    clearTimeout(slowTimer)
    if (!r.pending) return false
    const wait = SLOW - (Date.now() - progressAt)
    if (wait > 0) { slowTimer = setTimeout(() => renderStatus(), wait + 20); return false }
    return true
  }
  const renderStatus = () => {
    status.className = `status ${r.status}`
    const unsent = r.pending ? ` · ${tn(r.pending, '{{n}} unsent', '{{n}} unsent')}` : ''
    const relay = r.storage === 'relay' ? ` · ${tn(r.peers, 'relay-only, {{n}} peer online', 'relay-only, {{n}} peers online')}` : ''
    const slow = heldUp()
    status.textContent =
      r.status === 'online' ? (slow ? t('syncing {{n}}…', { n: r.pending }) : t('saved')) + relay
      : r.status === 'connecting' ? t('connecting…')
      : r.status === 'paused' ? t('manual sync') + unsent
      : t('offline{{unsent}} (changes kept locally)', { unsent })
    status.title = r.storage === 'relay' ? t('The server keeps no copy: changes reach collaborators only while you are both online.') : t('Changes are stored encrypted on the server.')
    if (r.storeProblem && r.storage !== 'relay' && r.status === 'online') {
      status.textContent = t('not saved on server')
      status.className = 'status warn'
      status.title = t('Changes reach collaborators who are online, and stay in this browser, but the server isn’t storing them. See the warning above.')
    }
    if (r.deletedAt) { // nothing syncs any more: not "offline"
      status.textContent = t('deleted by the owner')
      status.className = 'status offline'
      status.title = t('The owner deleted this project for everyone. This browser keeps a read-only copy.')
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
  // the owner deleted the project for everyone: this copy is all there is, and read-only
  if (r.deletedAt) {
    projects.update(keys.project, { deleted: r.deletedAt })
    const when = new Date(r.deletedAt).toLocaleDateString(locale, { dateStyle: 'medium' })
    clashNote.after(h('div', { class: 'risk deleted', role: 'status' },
      h('span', {}, h('b', {}, '⚠ '), t('The owner deleted this project for everyone on {{date}}. This copy in your browser is read-only and is the only one left here: download it to keep it.', { date: when })),
      h('span', { class: 'actions' },
        h('button', { onclick: () => downloadZip() }, t('Download the source (.zip)')),
        h('button', { onclick: async () => { removing = true; await r.close(); await projects.forget(keys.project); location.href = '/' } }, t('Remove from this browser')))))
  }
  r.onDeleted = () => { if (!removing) location.reload() } // reopen read-only, with the notice
  watchClashes(r, (c) => { clashes.push(c); renderClashes() })
  watchMerges(r, () => me.name) // paragraphs changed by two people apart: a note in Review
  keepLF(r) // line breaks are \n only (files saved on Windows have \r\n): see lineends.ts
  // End-to-end checks (scripts/check_synconly.py) type into the editor for real and read the shared
  // text back: only on this computer's own addresses, and only when a check turns it on.
  if (checking()) {
    (window as any).__texloom = { r, view: () => editor.view(), open: (p: string) => { layout.showEditor(); tree.open(p) } }
    // every shared text stays well formed once changes settle (invariants.ts)
    r.files.observeDeep(debounce(() => r.files.forEach((yt, p) => wellFormed(`the shared ${p}`, yt.toString())), 200))
  }
  runGitHub(r, () => me.name) // keep it in step with a GitHub repository, if it's linked to one
  runDropbox(r, () => me.name) // and with a Dropbox folder
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
  // Ready the in-browser engine while the project is open, so the first Compile starts sooner.
  setTimeout(() => { if (device.compile === 'browser') r.snapshotFiles().then(warmUp).catch((e) => quiet('the engine is readied at the first Compile instead', e)) }, 4000)

  // One button per collaborator, kept while they're here: awareness changes with every move of
  // their cursor, and a button rebuilt each time would lose the hover before its tooltip shows.
  const peerButtons = new Map<number, HTMLButtonElement>()
  r.awareness.on('change', () => {
    const others = [...r.awareness.getStates().entries()].filter(([id, s]) => id !== r.doc.clientID && s.user)
    const buttons = others.map(([id, s]) => {
      const name = String(s.user.name).slice(0, 64)
      const where = typeof s.file === 'string' ? ` · ${String(s.file).slice(0, 100)}` : ''
      let b = peerButtons.get(id)
      if (!b) peerButtons.set(id, (b = h('button', { class: 'peer', onclick: () => jumpTo(id) })))
      b.style.background = s.user.color || '#888' // a colour: replica.ts cleaned it
      b.dataset.tip = t('{{name}}{{where}} — click to go to where they are', { name, where }) // shown at once (style.css)
      b.setAttribute('aria-label', t('Go to {{name}}', { name }))
      b.textContent = name.slice(0, 2)
      return b
    })
    for (const id of peerButtons.keys()) if (!others.some(([o]) => o === id)) peerButtons.delete(id)
    if (buttons.length !== peers.children.length || buttons.some((b, i) => peers.children[i] !== b)) peers.replaceChildren(...buttons)
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
  // for sorting the start page; only now and then, since each one syncs the list of projects
  let lastTouched = 0
  const touched = () => {
    if (removing || Date.now() - lastTouched < 5 * 60_000) return
    lastTouched = Date.now()
    projects.update(keys.project, { modified: lastTouched })
  }
  r.files.observeDeep(touched); r.bin.observe(touched); r.dirs.observe(touched)

  const renderMeta = () => {
    const name: string = r.meta.get('name') || ''
    title.value = name === UNTITLED ? t('Untitled project') : name
    pageTitle = `${title.value} — ${APP}`
    setTitle()
    if (!removing) projects.update(keys.project, {
      fragment: keys.fragment(), server: awayServer(server), writable, at: Date.now(), name,
      createdBy: r.meta.get('createdBy'), storage: r.storage,
    })
    title.title = t('Project name (shared with collaborators) · code {{code}}', { code: projectCode(keys.project) })
  }
  r.meta.observe(renderMeta); renderMeta()
  output.compileBtn.addEventListener('click', () => { showInPanel(false); if (layout.narrow) { layout.reveal('right'); showTab('pdf') } }) // the result is what you want to see

  // debugging handle, only when enabled with localStorage['texloom.debug'] = '1'
  if (getString('texloom.debug') === '1') (window as any).texloom = { replica: r, Y, output, pdfjs, SyncTex, editor, startFolderSync, quiet: quieted }
}
