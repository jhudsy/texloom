// Per-device settings, the first-open setup dialog and the settings dialog.
import type { Replica, StorageMode, SyncMode } from './replica'
import { engineCached, clearEngineCache } from './compile'
import { h, dialog } from './dom'
import { detectMain } from './projectfiles'

export type CompileWhere = 'browser' | 'local' | 'none'
export type Device = { sync: SyncMode; compile: CompileWhere; autoCompile: boolean; setup: boolean; spell: boolean }

const DEFAULTS: Device = { sync: 'live', compile: 'browser', autoCompile: false, setup: false, spell: true }

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
    const { sync, compile, autoCompile, spell } = d
    localStorage.setItem('texloom.device', JSON.stringify({ sync, compile, autoCompile, spell }))
  },
}

function radios<T extends string>(name: string, value: T, options: [T, string, string?][], onChange: (v: T) => void) {
  return h('div', { class: 'radios' }, ...options.map(([v, label, hint]) =>
    h('label', { class: 'radio' },
      h('input', { type: 'radio', name, value: v, 'aria-label': label, checked: v === value, onchange: () => onChange(v) }),
      h('span', {}, h('b', {}, label), hint ? h('small', { class: 'muted' }, hint) : ''))))
}

function folderHelp(r: Replica, link: string): HTMLElement {
  const name = (r.meta.get('name') || 'project').toLowerCase().replace(/[^a-z0-9]+/g, '-')
  return h('div', { class: 'cmds' },
    h('p', { class: 'muted' }, 'A web page can’t keep a folder in sync while it’s closed, so this uses the small texloom helper (Python). Run once in a terminal:'),
    h('pre', {}, `texloom clone '${link}' ~/papers/${name}\ntexloom run ~/papers/${name} --compile`),
    h('p', { class: 'muted' }, 'Edit the files with anything (Claude Code, vim, VS Code); changes flow both ways while it runs. Leave out --compile if you don’t want your TeX installation used. Install: see the README.'))
}

const engineNote = async () => (await engineCached()) ? 'already downloaded on this browser' : 'one-time download of ≈125 MB, shared by all projects'

/** Shown the first time a link is opened in this browser. */
export async function setupDialog(r: Replica, link: string): Promise<Device> {
  const d = prefs.device(r.keys.project)
  const note = await engineNote()
  const nameIn = h('input', { value: prefs.name, placeholder: 'Shown to collaborators' }) as HTMLInputElement
  const folderBox = h('div', { hidden: true }, folderHelp(r, link))
  return new Promise((resolve) => {
    const dlg = dialog('setup',
      h('h3', {}, `Open “${r.meta.get('name') || 'project'}”`),
      h('p', { class: 'muted' }, r.keys.writable ? 'You can edit this project.' : 'This is a read-only link.'),
      h('label', { class: 'field' }, 'Your name', nameIn),
      h('h4', {}, 'Where do the files live?'),
      radios('where', 'browser', [
        ['browser', 'Only in this browser', 'Works offline. Nothing to install. Note: clearing this browser’s data deletes this copy; keep the link so it can be reopened from the server’s copy.'],
        ['folder', 'In this browser and in a folder on this computer', 'For editing with other tools or agents, and compiling with your own TeX. The folder is a separate copy: delete it like any folder. Choose this only for projects whose editors you trust: your own LaTeX tools may run programs the files ask for.'],
      ], (v) => (folderBox.hidden = v !== 'folder')),
      folderBox,
      h('h4', {}, 'Compiling'),
      radios('compile', d.compile, [
        ['browser', 'In this browser', `TeX Live 2026 in WebAssembly — ${note}.`],
        ['local', 'With my own TeX installation', 'Sends compile requests to a texloom helper running with --compile.'],
        ['none', 'Don’t compile here', 'Just view the PDFs collaborators publish.'],
      ], (v) => (d.compile = v)),
      h('h4', {}, 'Syncing'),
      radios('sync', d.sync, [
        ['live', 'Live', 'See collaborators’ changes as they type.'],
        ['manual', 'Manual', 'Keep changes on this device until you press Sync.'],
      ], (v) => (d.sync = v)),
      h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, 'Start')),
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
  const cacheBtn = h('button', { onclick: async () => { await clearEngineCache(); cacheBtn.textContent = 'Cleared' } }, 'Remove downloaded TeX engine')
  const dlg = dialog('settings',
    h('h3', {}, 'Settings'),
    h('h4', {}, 'Project', h('small', { class: 'muted' }, w ? ' — shared with all collaborators' : ' — read-only')),
    h('div', { class: 'grid' },
      h('span', {}, 'Main file'), sel('main', texFiles.map((p) => [p, p]), detectedMain),
      h('span', {}, 'Engine'), sel('engine', [['pdflatex', 'pdfLaTeX'], ['xelatex', 'XeLaTeX'], ['lualatex', 'LuaLaTeX']], 'pdflatex'),
      h('span', {}, 'Bibliography'), sel('bib', [['auto', 'Detect automatically'], ['bibtex', 'BibTeX'], ['biber', 'Biber'], ['none', 'None']], 'auto'),
      h('span', {}, 'Spelling'), sel('lang', [['', 'Browser’s language'], ['en-GB', 'English (UK)'], ['en-US', 'English (US)'], ['de', 'German'], ['fr', 'French'],
        ['es', 'Spanish'], ['it', 'Italian'], ['nl', 'Dutch'], ['pt', 'Portuguese'], ['sv', 'Swedish'], ['pl', 'Polish']], ''),
    ),
    h('h4', {}, 'Server storage'),
    radios('storage', r.storage, [
      ['server', 'Keep an encrypted copy on the server', 'Collaborators get your changes even if you’re never online at the same time. The server can’t read it.'],
      ['relay', 'Don’t store anything on the server', 'The server only passes changes between people who are online at the same time. Choosing this deletes the server’s copy.'],
    ], async (v: StorageMode) => {
      if (!w) return
      if (v === 'relay' && !r.keys.owner) {
        alert('Only the project’s owner can delete the server’s copy.')
        const keep = document.querySelector<HTMLInputElement>('input[name="storage"][value="server"]')
        if (keep) keep.checked = true
        return
      }
      if (v === 'relay' && !confirm('Delete the server’s encrypted copy? Changes will only reach collaborators while you are both online.')) return
      await r.setStorage(v)
    }),
    h('h4', {}, 'This device'),
    h('label', { class: 'field' }, 'Your name', h('input', { value: prefs.name, onchange: (e: any) => (prefs.name = e.target.value) })),
    h('h5', {}, 'Syncing'),
    radios('sync', d.sync, [
      ['live', 'Live sync'],
      ['manual', 'Manual sync', 'Changes stay here until you press Sync (e.g. to finish a thought, or save bandwidth).'],
    ], (v) => { d.sync = v; prefs.saveDevice(r.keys.project, d); onDevice(d) }),
    h('h5', {}, 'Compiling'),
    radios('compile', d.compile, [
      ['browser', 'Compile in this browser', `${note[0].toUpperCase()}${note.slice(1)}.`],
      ['local', 'Compile with my own TeX', 'Needs `texloom run <folder> --compile` running.'],
      ['none', 'Don’t compile here'],
    ], (v) => { d.compile = v; prefs.saveDevice(r.keys.project, d); onDevice(d) }),
    h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'aria-label': 'Recompile automatically', checked: d.autoCompile, onchange: (e: any) => { d.autoCompile = e.target.checked; prefs.saveDevice(r.keys.project, d); onDevice(d) } }),
      'Recompile automatically after I stop typing'),
    h('h5', {}, 'Editing'),
    h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'aria-label': 'Check spelling', checked: d.spell, onchange: (e: any) => { d.spell = e.target.checked; prefs.saveDevice(r.keys.project, d); onDevice(d) } }),
      'Check spelling (uses the browser’s dictionary; LaTeX commands, maths and labels are skipped)'),
    h('div', { class: 'row' },
      h('button', { onclick: async () => { const { askCode } = await import('./storagenotice'); if (await askCode()) r.retryStore() } }, 'Creation code…'),
      h('span', { class: 'muted small' }, 'Needed only to store new projects on servers that ask for one.')),
    h('details', {}, h('summary', {}, 'Keep a copy in a folder on this computer'),
      chooseFolder ? h('div', {},
        h('p', { class: 'muted small' }, 'This browser can sync a folder itself while this tab is open: nothing to install. Changes go both ways; files changed on both sides take the folder’s version (it stays in History).'),
        h('p', { class: 'muted small' }, 'Collaborators’ files are written there. TeXLoom won’t write files that LaTeX tools run as programs, and points out lines that ask to run one, but compile the folder with your own tools only if you trust everyone who can edit this project.'),
        h('div', { class: 'row' }, h('button', { class: 'primary', onclick: async () => { dlg.close(); await chooseFolder() } }, 'Choose a folder…'))) : null,
      chooseFolder ? h('p', { class: 'muted small' }, 'Or, to keep it in sync without a browser (and compile with your own TeX):') : null,
      folderHelp(r, link)),
    h('h4', {}, 'Remove'),
    h('p', { class: 'muted small' },
      'Removing deletes this browser’s copy only. Collaborators keep theirs, folder copies stay until you delete the folder, and the server’s encrypted copy stays unless you choose “Don’t store anything on the server” above.'),
    h('div', { class: 'row' }, h('button', { class: 'danger', onclick: async () => {
      const warn: string[] = []
      if (r.pending) warn.push(`${r.pending} change(s) from this browser haven’t reached the server or anyone else yet and will be lost.`)
      if (r.storage === 'relay') warn.push('The server keeps no copy of this project: if nobody else has one, it will be gone for good.')
      if (!confirm(['Remove this project from this browser?', ...warn].join('\n\n'))) return
      dlg.close()
      await onRemove()
    } }, 'Remove from this browser')),
    h('div', { class: 'row end' }, cacheBtn, h('button', { class: 'primary', onclick: () => dlg.close() }, 'Done')),
  )
}
