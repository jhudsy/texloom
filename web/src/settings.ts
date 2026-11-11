// Per-device settings, the first-open setup dialog and the settings dialog.
import { DOWNLOAD_APP, openInApp } from './openinapp'
import type { Replica, StorageMode, SyncMode } from './replica'
import { engineCached, clearEngineCache } from './compile'
import { h, dialog } from './dom'
import { detectMain } from './projectfiles'
import { t, LANGUAGES, langPref, setLangPref, type Lang } from './i18n'
import { editorMarkup, type Markup } from './review'

export type CompileWhere = 'browser' | 'local' | 'none'
export type Device = { sync: SyncMode; compile: CompileWhere; autoCompile: boolean; setup: boolean; spell: boolean; mathPreview: boolean; checks: boolean }

const DEFAULTS: Device = { sync: 'live', compile: 'browser', autoCompile: false, setup: false, spell: true, mathPreview: true, checks: true }

export const prefs = {
  get name(): string { return localStorage.getItem('texloom.name') || '' },
  set name(v: string) { localStorage.setItem('texloom.name', v) },
  device(project: string): Device {
    try {
      const global = JSON.parse(localStorage.getItem('texloom.device') || '{}')
      const local = JSON.parse(localStorage.getItem(`texloom.device.${project}`) || '{}')
      return { ...DEFAULTS, ...global, ...local }
    } catch { return { ...DEFAULTS } }
  },
  saveDevice(project: string, d: Device) {
    localStorage.setItem(`texloom.device.${project}`, JSON.stringify(d))
    // New projects default to the choices made most recently.
    const { sync, compile, autoCompile, spell, mathPreview, checks } = d
    localStorage.setItem('texloom.device', JSON.stringify({ sync, compile, autoCompile, spell, mathPreview, checks }))
  },
}

/** The interface language for this browser (all projects). Changing it reloads the page. */
export function languagePicker(): HTMLElement {
  return h('label', { class: 'lang' }, t('Interface language'),
    h('select', { 'aria-label': t('Interface language'), onchange: (e: any) => { setLangPref(e.target.value as Lang | 'auto'); location.reload() } },
      h('option', { value: 'auto', selected: langPref() === 'auto' }, t('Automatic (the browser’s language)')),
      ...LANGUAGES.map((l) => h('option', { value: l.id, selected: langPref() === l.id }, l.name))))
}

function radios<T extends string>(name: string, value: T, options: [T, string, string?, boolean?][], onChange: (v: T) => void) {
  return h('div', { class: 'radios' }, ...options.map(([v, label, hint, disabled]) =>
    h('label', { class: disabled ? 'radio disabled' : 'radio' },
      h('input', { type: 'radio', name, value: v, 'aria-label': label, checked: v === value, disabled: !!disabled, onchange: () => onChange(v) }),
      h('span', {}, h('b', {}, label), hint ? h('small', { class: 'muted' }, hint) : ''))))
}

function folderHelp(r: Replica, link: string): HTMLElement {
  const name = (r.meta.get('name') || 'project').toLowerCase().replace(/[^a-z0-9]+/g, '-')
  return h('div', { class: 'cmds' },
    h('p', { class: 'muted' }, t('A web page can’t keep a folder in sync while it’s closed. The TeXLoom app can, for all your projects, from the menu bar or tray, and starting at login if you like:')),
    h('div', { class: 'row' },
      h('button', { class: 'primary', onclick: () => openInApp(link) }, t('Open in the TeXLoom app')),
      h('a', { class: 'small', href: DOWNLOAD_APP, target: '_blank', rel: 'noopener' }, t('Download the app'))),
    h('p', { class: 'muted' }, t('Or with the texloom command-line helper:')),
    h('pre', {}, `texloom clone '${link}' ~/papers/${name}\ntexloom start      # ${t('syncs all your folders; `texloom autostart on` runs it at login')}`),
    h('p', { class: 'muted' }, t('Edit the files with anything (Claude Code, vim, VS Code); changes flow both ways. Install: see the README.')))
}

/** Why the server isn't storing more right now, in words. */
const problemText = (p: string) => ({ 'needs-code': t('needs code'), 'quota-full': t('quota full'), 'code-expired': t('code expired'), 'project-full': t('project full'), 'server-full': t('server full') } as Record<string, string>)[p] ?? p.replace('-', ' ')

const engineNote = async () => (await engineCached()) ? t('already downloaded on this browser') : t('one-time download of ≈125 MB, shared by all projects')

/** Shown the first time a link is opened in this browser. `byApp`: the desktop app opened it,
 *  and already keeps the project in a folder, so where the files live isn't a question. */
export async function setupDialog(r: Replica, link: string, byApp = false): Promise<Device> {
  const d = prefs.device(r.keys.project)
  const note = await engineNote()
  const nameIn = h('input', { value: prefs.name, placeholder: t('Shown to collaborators') }) as HTMLInputElement
  const folderBox = h('div', { hidden: true }, folderHelp(r, link))
  return new Promise((resolve) => {
    const dlg = dialog('setup',
      h('h3', {}, t('Open “{{name}}”', { name: r.meta.get('name') || t('project') })),
      h('p', { class: 'muted' }, r.keys.writable ? t('You can edit this project.') : t('This is a read-only link.')),
      h('label', { class: 'field' }, t('Your name'), nameIn),
      byApp ? h('p', { class: 'muted' }, t('The TeXLoom app on this computer keeps this project in sync with a folder. This browser keeps a copy of its own as well.')) : '',
      byApp ? '' : h('h4', {}, t('Where do the files live?')),
      byApp ? '' : radios('where', 'browser', [
        ['browser', t('Only in this browser'), t('Works offline. Nothing to install. Note: clearing this browser’s data deletes this copy; keep the link so it can be reopened from the server’s copy.')],
        ['folder', t('In this browser and in a folder on this computer'), t('For editing with other tools or agents, and compiling with your own TeX. The folder is a separate copy: delete it like any folder. Choose this only for projects whose editors you trust: your own LaTeX tools may run programs the files ask for.')],
      ], (v) => (folderBox.hidden = v !== 'folder')),
      folderBox,
      h('h4', {}, t('Compiling')),
      radios('compile', d.compile, [
        ['browser', t('In this browser'), t('TeX Live 2026 in WebAssembly — {{note}}.', { note })],
        ['local', t('With my own TeX installation'), t('Sends compile requests to a texloom helper running with --compile.')],
        ['none', t('Don’t compile here'), t('Just view the PDFs collaborators publish.')],
      ], (v) => (d.compile = v)),
      h('h4', {}, t('Syncing')),
      radios('sync', d.sync, [
        ['live', t('Live'), t('See collaborators’ changes as they type.')],
        ['manual', t('Manual'), t('Keep changes on this device until you press Sync.')],
      ], (v) => (d.sync = v)),
      h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Start'))),
    )
    dlg.addEventListener('close', () => {
      prefs.name = nameIn.value.trim()
      d.setup = true
      prefs.saveDevice(r.keys.project, d)
      resolve(d)
    })
  })
}

/** Project (shared) + device (local) settings. */
export async function settingsDialog(r: Replica, link: string, onDevice: (d: Device) => void, chooseFolder: (() => Promise<void>) | null, onRemove: () => Promise<void>) {
  const d = prefs.device(r.keys.project)
  const w = r.keys.writable
  const texFiles = [...r.files.keys()].filter((p) => p.endsWith('.tex')).sort()
  // the compiler's rule when no main file is chosen
  const detectedMain = detectMain(texFiles, (p) => r.files.get(p)?.toString() ?? '') ?? texFiles[0] ?? ''
  const sel = (key: string, options: [string, string][], def: string) =>
    h('select', { disabled: !w, onchange: (e: any) => r.meta.set(key, e.target.value) },
      ...options.map(([v, l]) => h('option', { value: v, ...(String(r.meta.get(key) ?? def) === v ? { selected: true } : {}) }, l)))
  const note = await engineNote()
  // A server that needs a creation code won't store a project it doesn't have yet: then
  // "keep a copy on the server" isn't something you can choose until a code is entered.
  // (A full allowance or disk is different: storing is allowed, just not right now.)
  const info = r.storeInfo
  let noStorage = r.storeProblem === 'needs-code' || (!!info && !info.registered && !info.canCreate)
  const storageBox = h('div', {})
  let codeRow: HTMLElement | null = null
  const storageRadios = () => radios('storage', r.storage, [
    ['server', t('Keep an encrypted copy on the server'),
      noStorage ? t('Not available yet: this server stores new projects only with a creation code. Until one is entered, your changes are kept here and reach collaborators who are online.')
        : r.storeProblem ? t('Collaborators get your changes even if you’re never online at the same time. Right now the server isn’t taking more ({{reason}}): your changes are kept here and sent later.', { reason: problemText(r.storeProblem) })
          : t('Collaborators get your changes even if you’re never online at the same time. The server can’t read it.'),
      !w || noStorage],
    ['relay', t('Don’t store anything on the server'), t('The server only passes changes between people who are online at the same time. Choosing this deletes the server’s copy.'), !w],
  ], async (v: StorageMode) => {
    if (!w) return
    if (v === 'relay' && !r.keys.owner) {
      alert(t('Only the project’s owner can delete the server’s copy.'))
      const keep = document.querySelector<HTMLInputElement>('input[name="storage"][value="server"]')
      if (keep) keep.checked = true
      return
    }
    if (v === 'relay' && !confirm(t('Delete the server’s encrypted copy? Changes will only reach collaborators while you are both online.'))) return
    await r.setStorage(v)
  })
  storageBox.append(storageRadios())
  const cacheBtn = h('button', { onclick: async () => { await clearEngineCache(); cacheBtn.textContent = t('Cleared') } }, t('Remove downloaded TeX engine'))
  const dlg = dialog('settings',
    h('h3', {}, t('Settings')),
    h('h4', {}, t('Project'), h('small', { class: 'muted' }, ' — ' + (w ? t('shared with all collaborators') : t('read-only')))),
    h('div', { class: 'grid' },
      h('span', {}, t('Main file')), sel('main', texFiles.map((p) => [p, p]), detectedMain),
      h('span', {}, t('Engine')), sel('engine', [['pdflatex', 'pdfLaTeX'], ['xelatex', 'XeLaTeX'], ['lualatex', 'LuaLaTeX']], 'pdflatex'),
      h('span', {}, t('Bibliography')), sel('bib', [['auto', t('Detect automatically')], ['bibtex', 'BibTeX'], ['biber', 'Biber'], ['none', t('None')]], 'auto'),
      h('span', {}, t('Spelling')), sel('lang', [['', t('Browser’s language')], ['en-GB', t('English (UK)')], ['en-US', t('English (US)')], ['de', t('German')], ['fr', t('French')],
        ['es', t('Spanish')], ['it', t('Italian')], ['nl', t('Dutch')], ['pt', t('Portuguese')], ['sv', t('Swedish')], ['pl', t('Polish')]], ''),
    ),
    h('h4', {}, t('Server storage')),
    storageBox,
    codeRow = noStorage ? h('div', { class: 'row' },
      h('button', { onclick: async () => {
        const { askCode } = await import('./storagenotice')
        if (!(await askCode())) return
        r.retryStore() // reconnects with the code; the server then stores this project
        noStorage = false
        storageBox.replaceChildren(storageRadios())
        codeRow?.remove()
      } }, t('Enter creation code…')),
      h('span', { class: 'muted small' }, t('Codes come from whoever runs this server.'))) : null,
    h('h4', {}, t('This device')),
    h('label', { class: 'field' }, t('Your name'), h('input', { value: prefs.name, onchange: (e: any) => (prefs.name = e.target.value) })),
    h('h5', {}, t('Syncing')),
    radios('sync', d.sync, [
      ['live', t('Live sync')],
      ['manual', t('Manual sync'), t('Changes stay here until you press Sync (e.g. to finish a thought, or save bandwidth).')],
    ], (v) => { d.sync = v; prefs.saveDevice(r.keys.project, d); onDevice(d) }),
    h('h5', {}, t('Compiling')),
    radios('compile', d.compile, [
      ['browser', t('Compile in this browser'), `${note[0].toUpperCase()}${note.slice(1)}.`],
      ['local', t('Compile with my own TeX'), t('Needs `texloom run <folder> --compile` running.')],
      ['none', t('Don’t compile here')],
    ], (v) => { d.compile = v; prefs.saveDevice(r.keys.project, d); onDevice(d) }),
    h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'aria-label': t('Recompile automatically'), checked: d.autoCompile, onchange: (e: any) => { d.autoCompile = e.target.checked; prefs.saveDevice(r.keys.project, d); onDevice(d) } }),
      t('Recompile automatically after I stop typing')),
    h('h5', {}, t('Language')),
    languagePicker(),
    h('h5', {}, t('Editing')),
    h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'aria-label': t('Check spelling'), checked: d.spell, onchange: (e: any) => { d.spell = e.target.checked; prefs.saveDevice(r.keys.project, d); onDevice(d) } }),
      t('Check spelling (uses the browser’s dictionary; LaTeX commands, maths and labels are skipped)')),
    h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'aria-label': t('Preview equations'), checked: d.mathPreview, onchange: (e: any) => { d.mathPreview = e.target.checked; prefs.saveDevice(r.keys.project, d); onDevice(d) } }),
      t('Preview the equation under the cursor')),
    h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'aria-label': t('Point out common mistakes'), checked: d.checks, onchange: (e: any) => { d.checks = e.target.checked; prefs.saveDevice(r.keys.project, d); onDevice(d) } }),
      t('Point out common LaTeX mistakes (unclosed environments, stray braces, \\label before \\caption, quotes, ties, …)')),
    h('label', { class: 'row' }, t('Suggested changes in the editor:'),
      h('select', { 'aria-label': t('Suggested changes in the editor:'), onchange: (e: any) => editorMarkup.set(e.target.value as Markup) },
        ...([['none', t('Not marked (see the Review tab)')], ['simple', t('Underlined')], ['all', t('In full (removed text struck through)')]] as [Markup, string][])
          .map(([v, label]) => h('option', { value: v, selected: editorMarkup.mode() === v }, label)))),
    h('div', { class: 'row' },
      h('button', { onclick: async () => { const { askCode } = await import('./storagenotice'); if (await askCode()) r.retryStore() } }, t('Creation code…')),
      h('span', { class: 'muted small' }, t('Needed only to store new projects on servers that ask for one.'))),
    h('details', {}, h('summary', {}, t('Keep a copy in a folder on this computer')),
      chooseFolder ? h('div', {},
        h('p', { class: 'muted small' }, t('This browser can sync a folder itself while this tab is open: nothing to install. Changes go both ways; files changed on both sides take the folder’s version (it stays in History).')),
        h('p', { class: 'muted small' }, t('Collaborators’ files are written there. TeXLoom won’t write files that LaTeX tools run as programs, and points out lines that ask to run one, but compile the folder with your own tools only if you trust everyone who can edit this project.')),
        h('div', { class: 'row' }, h('button', { class: 'primary', onclick: async () => { dlg.close(); await chooseFolder() } }, t('Choose a folder…')))) : null,
      chooseFolder ? h('p', { class: 'muted small' }, t('Or, to keep it in sync without a browser (and compile with your own TeX):')) : null,
      folderHelp(r, link)),
    h('h4', {}, t('Remove')),
    h('p', { class: 'muted small' },
      t('Remove this project from this browser; or, as the owner, from the server too, or delete it for everyone. Folders on your computer keep their files.')),
    h('div', { class: 'row' }, h('button', { class: 'danger', onclick: async () => {
      dlg.close()
      await onRemove()
    } }, t('Remove…'))),
    h('div', { class: 'row end' }, cacheBtn, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Done'))),
  )
}
