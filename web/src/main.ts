import * as Y from 'yjs'
import { EditorView, basicSetup } from 'codemirror'
import { EditorState, Compartment } from '@codemirror/state'
import { StreamLanguage } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { yCollab } from 'y-codemirror.next'
import { oneDark } from '@codemirror/theme-one-dark'
import { Keys } from './crypto'
import { Replica } from './replica'
import { projects, projectCode, latexTitle, whenOpened } from './projects'
import { compileInBrowser, browserCompileAvailable, type Progress } from './compile'
import { prefs, setupDialog, settingsDialog, type Device } from './settings'
import { PdfView } from './pdfview'
import { createAssistantPanel } from './assistant/panel'
import { setupLayout } from './layout'
import { chooseNewProject, riskBanner, SAFARI_NOTE } from './storagenotice'
import './style.css'

const $ = <T extends HTMLElement = HTMLElement>(sel: string, root: ParentNode = document) => root.querySelector(sel) as T
const h = (tag: string, attrs: Record<string, any> = {}, ...kids: (Node | string)[]) => {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v)
    else if (v !== false && v != null) el.setAttribute(k, v === true ? '' : v)
  }
  el.append(...kids)
  return el
}
const server = location.origin
const COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#469990', '#9a6324']

// ---- per-browser settings & recent projects ---------------------------------
const store = {
  get name(): string { return prefs.name },
  set name(v: string) { prefs.name = v },
}

window.addEventListener('hashchange', () => location.reload())
const keys = (() => { try { return Keys.fromFragment(location.hash) } catch (e) { alert(e); return null } })()
if (keys) openProject(keys)
else landing()

// ---- landing page -----------------------------------------------------------
function landing() {
  const app = $('#app')
  app.className = 'landing'
  const nameIn = h('input', { value: store.name, placeholder: 'Your name (shown to collaborators)', oninput: (e: any) => (store.name = e.target.value) })
  const linkIn = h('input', { placeholder: 'Paste a project link…' }) as HTMLInputElement
  app.append(
    h('h1', {}, 'texloom'),
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
        h('code', { class: 'pcode', title: 'Project code: the start of this project’s unique ID. Two projects with the same name always have different codes.' }, projectCode(r.project)),
        h('span', { class: `tag ${r.writable ? '' : 'ro'}` }, r.writable ? 'edit' : 'read-only'),
        r.atRisk || r.storage === 'relay' ? h('span', { class: 'tag warn', title: 'The server keeps no copy: this browser (and collaborators’ devices) hold the only copies. Clearing browser data deletes this one.' }, 'not on server') : '',
        h('span', { class: 'spacer' }),
        h('button', { class: 'icon', title: 'Private label (only you see it)', 'aria-label': `Label ${label}`, onclick: () => {
          const v = prompt(`Private label for “${r.name || 'this project'}” (${projectCode(r.project)}).\nOnly you see it, in this browser. Leave empty to use the shared name.`, r.nick ?? '')
          if (v === null) return
          projects.update(r.project, { nick: v.trim() || '' })
          location.reload()
        } }, '✎'),
        h('button', { class: 'icon forget', title: 'Remove from this browser', 'aria-label': `Remove ${label} from this browser`, onclick: async () => {
          if (!confirm(`Remove “${label}” (${projectCode(r.project)}) from this browser?\n\nOnly this browser’s copy is deleted. Unsynced changes made here would be lost. Collaborators, folder copies and the server’s copy are not affected.`)) return
          await projects.forget(r.project); location.reload()
        } }, '✕')),
      h('div', { class: 'details muted' }, details.join(' · ')))
  }))
}

// ---- project view -----------------------------------------------------------
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
  const me = { name: store.name || `anon-${r.doc.clientID % 1000}`, color: COLORS[r.doc.clientID % COLORS.length] }
  r.awareness.setLocalStateField('user', { ...me, colorLight: me.color + '33' })
  if (writable && !r.meta.get('name')) r.meta.set('name', 'Untitled project')
  try {
    if (writable && localStorage.getItem(`texloom.newRelay.${keys.project}`)) {
      localStorage.removeItem(`texloom.newRelay.${keys.project}`)
      r.setStorage('relay') // chosen at creation: no server copy
    }
  } catch {}
  if (writable && r.files.size === 0 && r.bin.size === 0 && !r.meta.get('seeded')) {
    // brand-new project: give it a starter file
    setTimeout(() => {
      if (r.files.size || r.bin.size) return
      r.doc.transact(() => {
        r.files.set('main.tex', new Y.Text('\\documentclass{article}\n\\title{Untitled}\n\\begin{document}\n\\maketitle\n\nHello, world.\n\n\\end{document}\n'))
        r.meta.set('main', 'main.tex'); r.meta.set('seeded', true)
        if (!r.meta.get('createdBy')) r.meta.set('createdBy', me.name)
      })
    }, 1500)
  }

  const app = $('#app')
  app.className = 'project'
  const status = h('span', { class: 'status' })
  const peers = h('span', { class: 'peers' })
  const title = h('input', { class: 'title', readonly: !writable, onchange: (e: any) => r.meta.set('name', e.target.value) }) as HTMLInputElement
  const fileList = h('ul', { class: 'files' })
  const editorHost = h('div', { class: 'editor' })
  const pdfView = new PdfView()
  const pdfInfo = h('div', { class: 'pdfinfo muted' }, 'No PDF yet.')
  const logPre = h('pre', { class: 'log', hidden: true })
  const compileBtn = h('button', { class: 'primary', onclick: () => doCompile() }, 'Compile')
  const pdfPane = h('div', { class: 'pane' })
  const aiPane = h('div', { class: 'pane', hidden: true })
  const showTab = (ai: boolean) => {
    pdfPane.hidden = ai; aiPane.hidden = !ai
    pdfTab.classList.toggle('active', !ai); aiTab.classList.toggle('active', ai)
    try { localStorage.setItem('texloom.tab', ai ? 'ai' : 'pdf') } catch {}
    if (ai && !aiPane.firstChild) aiPane.append(createAssistantPanel({
      replica: r, writable,
      editorContext: () => {
        if (!current || !view) return current ? { file: current, selection: '', fromLine: 0, toLine: 0 } : null
        const sel = view.state.selection.main
        const doc = view.state.doc
        return { file: current, selection: doc.sliceString(sel.from, sel.to), fromLine: doc.lineAt(sel.from).number, toLine: doc.lineAt(sel.to).number }
      },
      compile: () => doCompile(false),
      lastLog: () => lastLog,
      userName: () => me.name,
    }))
  }
  const pdfTab = h('button', { class: 'tab active', onclick: () => showTab(false) }, 'PDF')
  const aiTab = h('button', { class: 'tab', onclick: () => showTab(true) }, 'Assistant')
  const syncBtn = h('button', { onclick: async () => { syncBtn.setAttribute('disabled', ''); await r.syncNow(); syncBtn.removeAttribute('disabled') } }, 'Sync now')
  const progress = h('div', { class: 'progress', hidden: true }, h('div', { class: 'bar' }), h('span', {}))
  const applyDevice = (d: Device) => {
    device = d
    r.setSyncMode(d.sync)
    syncBtn.hidden = d.sync !== 'manual'
    compileBtn.hidden = d.compile === 'none'
  }
  const uploadIn = h('input', { type: 'file', multiple: true, hidden: true, onchange: (e: any) => upload([...e.target.files]) }) as HTMLInputElement

  const leftBtn = h('button', { class: 'pane-toggle', onclick: () => layout.toggleLeft() }, '☰')
  const rightBtn = h('button', { class: 'pane-toggle', onclick: () => layout.toggleRight() }, '▥')
  const splitL = h('div', { class: 'split' })
  const splitR = h('div', { class: 'split' })
  const mainEl = h('main', {})
  const risk = riskBanner(r, writable, () => openSettings())
  app.append(
    h('header', {},
      leftBtn,
      h('a', { href: '/', class: 'brand', onclick: (e: Event) => { e.preventDefault(); location.href = '/' } }, 'texloom'),
      title, writable ? '' : h('span', { class: 'badge' }, 'read-only'),
      h('span', { class: 'spacer' }), peers, status,
      syncBtn,
      h('button', { onclick: () => share(keys) }, 'Share'),
      h('button', { title: 'Settings', onclick: () => openSettings() }, '⚙'),
      compileBtn,
      rightBtn,
    ),
    risk.el,
    mainEl,
  )
  function openSettings() {
    settingsDialog(r, keys.link(server, writable), applyDevice, async () => {
        removing = true
        await r.close()
        await projects.forget(keys.project)
        location.href = '/'
    })
  }
  mainEl.append(
      h('aside', {},
        h('div', { class: 'tools' },
          writable ? h('button', { title: 'New file', onclick: () => newFile() }, '+ File') : '',
          writable ? h('button', { title: 'Upload files', onclick: () => uploadIn.click() }, 'Upload') : '',
          uploadIn,
        ),
        fileList,
        h('div', { class: 'hint muted' }, writable ? 'Drop files here to upload. ★ = main file.' : ''),
      ),
      splitL,
      editorHost,
      splitR,
      h('section', { class: 'right' },
        h('div', { class: 'tabs' }, pdfTab, aiTab),
        pdfPane, aiPane),
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

  // drag & drop upload
  app.addEventListener('dragover', (e) => e.preventDefault())
  app.addEventListener('drop', (e) => { e.preventDefault(); if (writable) upload([...(e.dataTransfer?.files ?? [])]) })

  // ---- status / presence ----
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
  let lastRisk: boolean | null = null
  r.onStatus = renderStatus; renderStatus()
  r.meta.observe(() => renderStatus()) // storage mode can change from Settings or a collaborator
  applyDevice(device)
  const renderPeers = () => {
    peers.replaceChildren(...[...r.awareness.getStates().entries()].filter(([id, s]) => id !== r.doc.clientID && s.user)
      .map(([, s]) => h('span', { class: 'peer', style: `background:${s.user.color}`, title: s.user.name }, s.user.name.slice(0, 2))))
  }
  r.awareness.on('change', renderPeers)
  r.awareness.on('change', () => {
    if (removing) return
    const names = [...r.awareness.getStates().entries()].filter(([id]) => id !== r.doc.clientID).map(([, st]) => st.user?.name as string)
    projects.sawPeople(keys.project, names, me.name)
  })
  // keep the start page's summary (document title, file count) current
  let summaryTimer: any
  const summarize = () => {
    clearTimeout(summaryTimer)
    summaryTimer = setTimeout(() => {
      if (removing) return
      const main = mainFile()
      projects.update(keys.project, {
        docTitle: main ? latexTitle(r.files.get(main)!.toString()) : undefined,
        files: r.files.size + r.bin.size,
      })
    }, 1500)
  }
  r.files.observeDeep(summarize); r.bin.observe(summarize); summarize()

  // ---- meta ----
  const renderMeta = () => {
    title.value = r.meta.get('name') || ''
    document.title = `${title.value} — texloom`
    if (!removing) projects.update(keys.project, {
      fragment: keys.fragment(), writable, at: Date.now(), name: title.value,
      createdBy: r.meta.get('createdBy'), storage: r.storage,
    })
    title.title = `Project name (shared with collaborators) · code ${projectCode(keys.project)}`
  }
  r.meta.observe(renderMeta); renderMeta()

  // ---- file tree ----
  let current: string | null = null
  const mainFile = () => {
    const m = r.meta.get('main')
    if (m && r.files.has(m)) return m as string
    const tex = [...r.files.keys()].filter((p) => p.endsWith('.tex')).sort((a, b) => Number(b === 'main.tex') - Number(a === 'main.tex'))
    return tex.find((p) => r.files.get(p)!.toString().includes('\\documentclass')) ?? tex[0] ?? null
  }
  const renderFiles = () => {
    const paths = [...new Set([...r.files.keys(), ...r.bin.keys()])].sort()
    const main = mainFile()
    fileList.replaceChildren(...paths.map((p) => {
      const isBin = r.bin.has(p)
      const li = h('li', { class: p === current ? 'active' : '', onclick: () => select(p) },
        h('span', { class: 'icon' }, p === main ? '★' : isBin ? '▣' : '¶'),
        h('span', { class: 'path' }, p))
      if (writable) {
        li.append(h('span', { class: 'acts' },
          !isBin && p.endsWith('.tex') ? h('button', { title: 'Set as main file', onclick: (e: Event) => { e.stopPropagation(); r.meta.set('main', p) } }, '★') : '',
          h('button', { title: 'Rename', onclick: (e: Event) => { e.stopPropagation(); rename(p) } }, '✎'),
          h('button', { title: 'Delete', onclick: (e: Event) => { e.stopPropagation(); remove(p) } }, '✕')))
      }
      return li
    }))
    if (!current || !paths.includes(current)) {
      const m = mainFile() ?? paths[0]
      if (m) select(m)
    }
  }
  r.files.observe(renderFiles); r.bin.observe(renderFiles); r.meta.observe(renderFiles)

  // ---- editor ----
  let view: EditorView | null = null
  const ro = new Compartment()
  function select(p: string) {
    current = p
    view?.destroy(); view = null
    editorHost.replaceChildren()
    const yt = r.files.get(p)
    if (yt) {
      const undoManager = new Y.UndoManager(yt)
      view = new EditorView({
        parent: editorHost,
        state: EditorState.create({
          doc: yt.toString(),
          extensions: [basicSetup, StreamLanguage.define(stex), EditorView.lineWrapping,
            matchMedia('(prefers-color-scheme: dark)').matches ? oneDark : [],
            yCollab(yt, r.awareness, { undoManager }),
            ro.of([EditorState.readOnly.of(!writable), EditorView.editable.of(writable)])],
        }),
      })
    } else if (r.bin.has(p)) {
      const info = r.bin.get(p)!
      const box = h('div', { class: 'binary' }, h('p', {}, `${p} — ${(info.size / 1024).toFixed(1)} KB`))
      editorHost.append(box)
      r.getBlob(info.h).then((data) => {
        const type = p.endsWith('.pdf') ? 'application/pdf' : p.endsWith('.svg') ? 'image/svg+xml' : ''
        const url = URL.createObjectURL(new Blob([data as BlobPart], { type }))
        box.append(/\.(png|jpe?g|gif|svg|webp)$/i.test(p) ? h('img', { src: url }) : type ? h('iframe', { src: url }) : h('a', { href: url, download: p.split('/').pop() }, 'Download'))
      }).catch((e) => box.append(h('p', { class: 'err' }, String(e))))
    }
    renderFiles()
  }

  // ---- file operations ----
  function newFile() {
    const p = prompt('New file path (e.g. sections/intro.tex)')?.trim()
    if (!p || r.files.has(p) || r.bin.has(p)) return
    r.files.set(p, new Y.Text(''))
    select(p)
  }
  function remove(p: string) {
    if (!confirm(`Delete ${p}? (It stays in history.)`)) return
    r.doc.transact(() => { r.files.delete(p); r.bin.delete(p) })
  }
  function rename(p: string) {
    const q = prompt('Rename to', p)?.trim()
    if (!q || q === p || r.files.has(q) || r.bin.has(q)) return
    r.doc.transact(() => {
      const t = r.files.get(p)
      if (t) { r.files.set(q, new Y.Text(t.toString())); r.files.delete(p) }
      const b = r.bin.get(p)
      if (b) { r.bin.set(q, b); r.bin.delete(p) }
      if (r.meta.get('main') === p) r.meta.set('main', q)
    })
    current = q
  }
  async function upload(files: File[]) {
    for (const f of files) {
      const path = (f as any).webkitRelativePath || f.name
      const data = new Uint8Array(await f.arrayBuffer())
      const text = isText(path, data)
      if (text !== null) {
        r.doc.transact(() => {
          r.bin.delete(path)
          const yt = r.files.get(path)
          if (yt) { yt.delete(0, yt.length); yt.insert(0, text) } else r.files.set(path, new Y.Text(text))
        })
      } else {
        const id = await r.putBlob(data)
        r.doc.transact(() => { r.files.delete(path); r.bin.set(path, { h: id, size: data.length }) })
      }
    }
  }

  // ---- PDF output ----
  let lastLog = ''
  const showPdf = (pdf: Uint8Array | null, log: string, info: string) => {
    if (pdf) pdfView.show(pdf).catch((e) => (pdfInfo.textContent = `PDF error: ${e}`))
    logPre.textContent = log
    lastLog = log
    pdfInfo.textContent = info
  }
  const renderOut = async () => {
    const o = r.out.get('latest')
    if (!o) return
    const dec = new TextDecoder()
    const [pdf, log] = await Promise.all([o.pdf ? r.getBlob(o.pdf) : null, o.log ? r.getBlob(o.log).then((b) => dec.decode(b)) : ''])
    showPdf(pdf, log, `${o.ok ? '✓' : '✗ errors'} · ${o.by} · ${new Date(o.at * 1000).toLocaleTimeString()}`)
  }
  r.out.observe(() => renderOut().catch(console.error)); renderOut().catch(console.error)

  const MB = (b?: number) => ((b ?? 0) / 1048576).toFixed(0)
  const showProgress = (p: Progress | null) => {
    progress.hidden = !p
    if (!p) return
    const bar = progress.firstChild as HTMLElement, label = progress.lastChild as HTMLElement
    if (p.phase === 'download') {
      bar.style.width = `${p.percent ?? 0}%`
      label.textContent = `Downloading TeX (once per browser) — ${Math.round(p.percent ?? 0)}%` + (p.total ? ` · ${MB(p.loaded)} / ${MB(p.total)} MB` : '')
    } else {
      bar.style.width = '100%'
      label.textContent = 'Compiling…'
    }
    progress.classList.toggle('busy', p.phase === 'compile')
  }

  type CompileOut = { ok: boolean; log: string } | null
  /** Compile per this device's setting. `interactive` = show alerts (button) vs return them (assistant). */
  async function doCompile(interactive = true): Promise<CompileOut> {
    const fail = (msg: string): CompileOut => { if (interactive) alert(msg); return { ok: false, log: msg } }
    if (device.compile === 'none') return null
    const main = mainFile()
    if (!main) return fail('No .tex file with \\documentclass found.')
    if (device.compile === 'local') return compileLocally(fail)
    if (!(await browserCompileAvailable())) {
      return fail('In-browser compilation is not installed on this server.\n\nChoose “Compile with my own TeX” in ⚙ Settings and run:\n  texloom run <folder> --compile')
    }
    if (compileBtn.hasAttribute('disabled')) return fail('A compile is already running; try again in a moment.')
    compileBtn.textContent = 'Compiling…'; compileBtn.setAttribute('disabled', '')
    showProgress({ phase: 'download', percent: 0 })
    try {
      const t0 = performance.now()
      const res = await compileInBrowser(await r.snapshotFiles(), main,
        { engine: r.meta.get('engine') || 'pdflatex', bib: r.meta.get('bib') || 'auto' }, showProgress)
      const secs = ((performance.now() - t0) / 1000).toFixed(1)
      showPdf(res.pdf, res.log, `${res.pdf ? '✓' : '✗ errors'} · in browser · ${secs}s`)
      if (!res.pdf) logPre.hidden = false
      if (writable && res.pdf) { // publish so collaborators see it without compiling
        const [pdfId, logId] = await Promise.all([r.putBlob(res.pdf), r.putBlob(new TextEncoder().encode(res.log))])
        r.out.set('latest', { pdf: pdfId, log: logId, ok: true, by: `${me.name} (browser)`, at: Date.now() / 1000, main })
      }
      return { ok: !!res.pdf, log: res.log }
    } catch (e) {
      return fail(`Compile failed: ${e}`)
    } finally {
      showProgress(null)
      compileBtn.textContent = 'Compile'; compileBtn.removeAttribute('disabled')
    }
  }

  async function compileLocally(fail: (m: string) => CompileOut): Promise<CompileOut> {
    if (!(await r.requestCompile())) return fail('Not connected, so no local helper can be reached.')
    showProgress({ phase: 'compile' })
    const label = progress.lastChild as HTMLElement
    label.textContent = 'Asking your texloom helper to compile…'
    let acked = false
    r.onCompileAck = (p) => { acked = true; label.textContent = `Compiling with ${p.by}…` }
    const before = r.out.get('latest')?.at
    for (let i = 0; i < 240; i++) {
      await new Promise((res) => setTimeout(res, 250))
      if (r.out.get('latest')?.at !== before) break
      if (i === 20 && !acked) {
        showProgress(null)
        return fail('No texloom helper with --compile answered.\n\nOn a computer with TeX, run:\n  texloom run <folder> --compile')
      }
    }
    showProgress(null)
    const o = r.out.get('latest')
    if (!o || o.at === before) return fail('The local helper did not finish compiling within 60 s.')
    const log = o.log ? new TextDecoder().decode(await r.getBlob(o.log)) : ''
    return { ok: !!o.ok, log }
  }

  // optional auto-compile after a pause in typing
  let autoTimer: any
  pdfPane.append(h('div', { class: 'pdfbar' }, pdfInfo, h('button', { onclick: () => (logPre.hidden = !logPre.hidden) }, 'Log')), progress, logPre, pdfView.el)
  if (localStorage.getItem('texloom.tab') === 'ai') showTab(true)

  r.files.observeDeep((_e, tr) => {
    if (!device.autoCompile || device.compile === 'none' || tr.origin !== null && typeof tr.origin === 'symbol') return
    clearTimeout(autoTimer)
    autoTimer = setTimeout(() => { if (!compileBtn.hasAttribute('disabled')) doCompile() }, 2500)
  })

  renderFiles()
  ;(window as any).texloom = { replica: r, Y } // handy for debugging & browser-driven agents
}

function isText(path: string, data: Uint8Array): string | null {
  if (data.length > 4 * 1024 * 1024 || data.subarray(0, 8192).includes(0)) return null
  if (/\.(png|jpe?g|gif|pdf|eps|webp|zip|gz|ttf|otf|woff2?)$/i.test(path)) return null
  try { return new TextDecoder('utf-8', { fatal: true }).decode(data) } catch { return null }
}

function share(keys: Keys) {
  const dlg = h('dialog', { class: 'share' },
    h('h3', {}, 'Share this project'),
    ...(keys.writable ? [h('label', {}, 'Edit link — anyone with it can edit'), copyRow(keys.link(server, true))] : []),
    h('label', {}, 'Read-only link'), copyRow(keys.link(server, false)),
    h('p', { class: 'muted' }, 'Keys stay in the part after “#”, which is never sent to the server. To revoke access, create a new project.'),
    h('button', { onclick: () => dlg.close() }, 'Close'),
  ) as HTMLDialogElement
  document.body.append(dlg)
  dlg.addEventListener('close', () => dlg.remove())
  dlg.showModal()
}
function copyRow(text: string) {
  const inp = h('input', { value: text, readonly: true, onfocus: (e: any) => e.target.select() })
  return h('div', { class: 'row' }, inp, h('button', { onclick: (e: any) => { navigator.clipboard.writeText(text); e.target.textContent = 'Copied' } }, 'Copy'))
}
