// The project file tree: folders (collapsible), files, and file/folder operations.
import { menuItems, openContextMenu, type MenuItem } from './contextmenu'
import { isPreviewable } from './preview'
import * as Y from 'yjs'
import type { Replica } from './replica'
import { h, saveFile, askText } from './dom'
import { icon } from './icons'
import { t, tn } from './i18n'
import { allFiles, allFolders, cleanPath, confirmAdding, deletePath, exists, IMAGE_RE, isDocument, mainFile, movePath, newFolder, putFile } from './projectfiles'
import { isJunk } from './templates'

export interface FileTree {
  el: HTMLElement
  open(path: string): void
  current(): string | null
  /** Open the main file, and keep a file open from now on. */
  start(): void
  /** Add an item to the New menu (after New file and New folder). */
  addNew(label: string, run: () => void): void
}

const base = (p: string) => p.slice(p.lastIndexOf('/') + 1)
const parent = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)

export interface FileTreeMenu {
  preview(path: string): void
  compile(path: string): void
  /** Put LaTeX at the editor's cursor; absent when no .tex file is open. */
  insert(): ((text: string) => void) | null
  /** Fetch a file linked to a Zotero collection again; null when it isn't (or can't be here). */
  zotero?(path: string): (() => void) | null
}

export function createFileTree(r: Replica, writable: boolean, onOpen: (path: string | null) => void, menu: FileTreeMenu): FileTree {
  let current: string | null = null // open file
  let folder = '' // folder new things go into (selected folder, or the open file's folder)
  const KEY = `texloom.tree.${r.keys.project}`
  const collapsed = new Set<string>((() => { try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] } })())
  const saveCollapsed = () => { try { localStorage.setItem(KEY, JSON.stringify([...collapsed])) } catch {} }

  const list = h('ul', { class: 'files', role: 'tree' })
  const uploadIn = h('input', { type: 'file', multiple: true, hidden: true, onchange: (e: any) => { upload([...e.target.files].map((file) => ({ file, rel: file.name })), [], folder); e.target.value = '' } })
  const where = h('div', { class: 'where muted small' })
  // New file, New folder and the items the editor adds (a reference, an import)
  const newItems: MenuItem[] = [
    [t('New file…'), () => newFile(folder)],
    [t('New folder…'), () => makeFolder(folder)],
  ]
  const newBtn = h('button', { class: 'withicon', 'aria-haspopup': 'menu', title: t('New file or folder in the selected folder, and more'), onclick: () => {
    const box = newBtn.getBoundingClientRect()
    newBtn.setAttribute('aria-expanded', 'true')
    openContextMenu(box.left, box.bottom + 2, newItems, () => newBtn.setAttribute('aria-expanded', 'false'))
  } }, icon('plus'), t('New'), icon('chevron'))
  const el = h('div', { class: 'filetree' },
    writable ? h('div', { class: 'tools' }, newBtn,
      h('button', { class: 'withicon', title: t('Upload files into the selected folder'), onclick: () => uploadIn.click() }, icon('upload'), t('Upload')),
      uploadIn) : null,
    where,
    list,
    h('div', { class: 'hint muted' }, writable ? t('Drag to move · drop files or folders from your computer onto a folder to upload · ★ = main file') : ''),
  )

  // ---- rendering ------------------------------------------------------------
  let started = false // the first file opens from start(), once the page around it exists
  function render() {
    const files = allFiles(r)
    const folders = allFolders(r)
    const main = mainFile(r)
    const rows: HTMLElement[] = []
    const children = (dir: string) => ({
      dirs: folders.filter((f) => parent(f) === dir),
      files: files.filter((f) => parent(f) === dir),
    })
    const walk = (dir: string, depth: number) => {
      const c = children(dir)
      for (const d of c.dirs) {
        const open = !collapsed.has(d)
        rows.push(row(d, depth, true, open, false))
        if (open) walk(d, depth + 1)
      }
      for (const f of c.files) rows.push(row(f, depth, false, false, f === main))
    }
    walk('', 0)
    list.replaceChildren(...rows)
    where.textContent = writable && folder ? t('Adding to: {{folder}}/', { folder }) : ''
    if (started && (!current || !files.includes(current))) {
      const pick = main ?? files[0] ?? null
      if (pick !== current) openFile(pick)
    }
  }

  async function download(path: string) {
    const info = r.bin.get(path)
    const data = info ? await r.getBlob(info.h) : r.files.get(path)?.toString() ?? ''
    saveFile(base(path), data)
  }

  function rowMenu(path: string, isDir: boolean, isBin: boolean): MenuItem[] {
    if (isDir) return menuItems(writable && [t('New file here'), () => newFile(path)], writable && [t('Rename or move'), () => rename(path, true)], writable && [t('Delete'), () => remove(path, true)])
    const insert = writable ? menu.insert() : null
    const latex = /\.tex$/i.test(path) ? `\\input{${path.replace(/\.tex$/i, '')}}` : IMAGE_RE.test(path) ? `\\includegraphics{${path}}` : null
    return menuItems(
      [t('Open'), () => openFile(path)],
      isPreviewable(path) && [t('Preview'), () => menu.preview(path)],
      isDocument(r, path) && [t('Compile'), () => menu.compile(path)],
      insert && latex !== null && !/[{}\\%#]/.test(path) && [t('Insert {{latex}}', { latex }), () => insert(latex)],
      writable && !isBin && path.endsWith('.tex') && [t('Set as main file'), () => r.meta.set('main', path)],
      !!menu.zotero?.(path) && [t('Refresh from Zotero'), () => menu.zotero?.(path)?.()],
      [t('Download'), () => { download(path).catch((e) => alert(String(e))) }],
      writable && [t('Rename or move'), () => rename(path, false)],
      writable && [t('Delete'), () => remove(path, false)],
    )
  }

  function row(path: string, depth: number, isDir: boolean, open: boolean, isMain: boolean): HTMLElement {
    const isBin = r.bin.has(path)
    const icon = isDir ? '' : isMain ? '★' : isBin ? '▣' : '¶' // a folder's triangle is drawn in style.css
    const li = h('li', {
      class: `${!isDir && path === current ? 'active' : ''} ${isDir && path === folder ? 'folder-sel' : ''} ${isDir ? 'dir' : ''}`,
      role: 'treeitem', 'aria-expanded': isDir ? String(open) : null, title: path,
      style: `padding-left:${10 + depth * 14}px`,
      draggable: writable ? 'true' : null,
      onclick: () => {
        if (isDir) {
          folder = path
          if (open) collapsed.add(path); else collapsed.delete(path)
          saveCollapsed(); render()
        } else openFile(path)
      },
    },
      h('span', { class: isDir ? 'icon tri' : 'icon' }, icon),
      h('span', { class: 'path' }, base(path)))
    li.addEventListener('contextmenu', (e) => {
      e.preventDefault()
      e.stopPropagation()
      openContextMenu(e.clientX, e.clientY, rowMenu(path, isDir, isBin))
    })
    if (writable) {
      const act = (title: string, label: string, fn: () => void) =>
        h('button', { title, 'aria-label': t('{{action}}: {{path}}', { action: title, path }), onclick: (e: Event) => { e.stopPropagation(); fn() } }, label)
      li.append(h('span', { class: 'acts' },
        isDir ? act(t('New file here'), '+', () => newFile(path)) : null,
        !isDir && !isBin && path.endsWith('.tex') ? act(t('Set as main file'), '★', () => r.meta.set('main', path)) : null,
        act(t('Rename or move'), '✎', () => rename(path, isDir)),
        act(t('Delete'), '✕', () => remove(path, isDir))))
      // drag to move; drop files from the computer onto folders
      li.addEventListener('dragstart', (e) => { e.dataTransfer?.setData('application/x-texloom-path', path); e.stopPropagation() })
      if (isDir) {
        li.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); li.classList.add('drop') })
        li.addEventListener('dragleave', () => li.classList.remove('drop'))
        li.addEventListener('drop', (e) => { e.preventDefault(); e.stopPropagation(); li.classList.remove('drop'); dropOn(e, path) })
      }
    }
    return li
  }

  // dropping on empty space in the list = project root
  if (writable) {
    list.addEventListener('dragover', (e) => e.preventDefault())
    list.addEventListener('drop', (e) => { e.preventDefault(); e.stopPropagation(); dropOn(e, '') })
  }

  function dropOn(e: DragEvent, dir: string) {
    const moved = e.dataTransfer?.getData('application/x-texloom-path')
    if (moved) {
      if (moved === dir || dir.startsWith(moved + '/') || parent(moved) === dir) return
      const to = join(dir, base(moved))
      if (exists(r, to)) return alert(t('{{path}} already exists.', { path: to }))
      movePath(r, moved, to)
      if (current === moved) current = to
      return
    }
    // directories can only be read from these, and only during the drop event itself
    const entries = [...(e.dataTransfer?.items ?? [])].filter((i) => i.kind === 'file').map((i) => i.webkitGetAsEntry?.()).filter(Boolean) as FileSystemEntry[]
    if (entries.some((x) => x.isDirectory)) { addEntries(entries, dir).catch((err) => alert(String(err))); return }
    const files = [...(e.dataTransfer?.files ?? [])]
    if (files.length) upload(files.map((file) => ({ file, rel: file.name })), [], dir)
  }

  /** Everything under dropped files and folders, with paths starting at what was dropped. */
  async function addEntries(entries: FileSystemEntry[], dir: string) {
    const files: { file: File; rel: string }[] = [], dirs: string[] = []
    let skipped = 0
    const walk = async (entry: FileSystemEntry, rel: string): Promise<void> => {
      if (isJunk(rel)) { skipped++; return }
      if (entry.isFile) {
        files.push({ file: await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej)), rel })
        return
      }
      dirs.push(rel)
      const reader = (entry as FileSystemDirectoryEntry).createReader()
      for (;;) { // each call gives only some of a folder's entries (100 in Chrome), then none
        const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej))
        if (!batch.length) break
        for (const child of batch) await walk(child, `${rel}/${child.name}`)
      }
    }
    where.textContent = t('Reading the folder…')
    for (const entry of entries) await walk(entry, entry.name)
    await upload(files, dirs, dir, skipped)
  }

  function openFile(path: string | null) {
    current = path
    if (path) folder = parent(path)
    onOpen(path)
    render()
  }

  // ---- operations -----------------------------------------------------------
  async function ask(label: string, def: string): Promise<string | null> {
    const v = await askText(label, def)
    if (v === null) return null
    const p = cleanPath(v)
    if (!p) { alert(t('That isn’t a valid path. Use letters, numbers, - _ . and / between folders.')); return null }
    return p
  }

  async function newFile(dir: string) {
    const p = await ask(t('New file name (you can include folders, e.g. sections/intro.tex)'), join(dir, ''))
    if (!p) return
    if (exists(r, p)) return alert(t('{{path}} already exists.', { path: p }))
    r.files.set(p, new Y.Text(''))
    collapsed.delete(parent(p)); saveCollapsed()
    openFile(p)
  }

  async function makeFolder(dir: string) {
    const p = await ask(t('New folder name'), join(dir, ''))
    if (!p) return
    if (exists(r, p)) return alert(t('{{path}} already exists.', { path: p }))
    newFolder(r, p)
    folder = p
    render()
  }

  async function rename(path: string, isDir: boolean) {
    const q = await ask(isDir ? t('Rename or move folder to') : t('Rename or move file to'), path)
    if (!q || q === path) return
    if (isDir && q.startsWith(path + '/')) return alert(t('A folder can’t be moved into itself.'))
    if (exists(r, q)) return alert(t('{{path}} already exists.', { path: q }))
    movePath(r, path, q)
    if (current === path) current = q
    else if (current?.startsWith(path + '/')) current = q + current.slice(path.length)
    if (folder === path) folder = q
  }

  function remove(path: string, isDir: boolean) {
    const count = isDir ? allFiles(r).filter((f) => f.startsWith(path + '/')).length : 1
    const question = !isDir ? t('Delete {{path}}? (It stays in history.)', { path })
      : count ? tn(count, 'Delete the folder {{path}} and the {{n}} file in it? (It stays in history.)', 'Delete the folder {{path}} and the {{n}} files in it? (It stays in history.)', { path })
      : t('Delete the folder {{path}}? (It stays in history.)', { path })
    if (!confirm(question)) return
    deletePath(r, path)
    if (folder === path || folder.startsWith(path + '/')) folder = parent(path)
  }

  /** Add files, and any folders (so that empty ones arrive too). `skipped`: already left out. */
  async function upload(files: { file: File; rel: string }[], dirs: string[], dir: string, skipped = 0) {
    const refused: string[] = []
    if (!confirmAdding(r, files.map((f) => cleanPath(join(dir, f.rel))).filter((p): p is string => !!p), files.reduce((sum, f) => sum + f.file.size, 0))) { render(); return }
    for (const d of dirs) { const path = cleanPath(join(dir, d)); if (path && !exists(r, path)) newFolder(r, path) }
    for (const [i, { file, rel }] of files.entries()) {
      if (files.length > 1) where.textContent = t('Adding files… ({{done}} of {{total}})', { done: i + 1, total: files.length })
      const path = cleanPath(join(dir, rel))
      if (!path) { refused.push(rel); continue }
      await putFile(r, path, new Uint8Array(await file.arrayBuffer()))
    }
    render()
    if (refused.length === 1 && !skipped) alert(t('Skipped {{name}}: unsupported name.', { name: refused[0] }))
    else if (refused.length || skipped) alert(tn(refused.length + skipped, 'Skipped {{n}} (build files or unsupported names).', 'Skipped {{n}} (build files or unsupported names).'))
  }

  r.files.observe(render); r.bin.observe(render); r.dirs.observe(render); r.meta.observe(render)
  render()
  return { el, open: openFile, current: () => current, start: () => { started = true; render() }, addNew: (label, run) => { newItems.push([label, run]) } }
}
