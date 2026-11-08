// The project file tree: folders (collapsible), files, and file/folder operations.
import { menuItems, openContextMenu, type MenuItem } from './contextmenu'
import { isPreviewable } from './preview'
import * as Y from 'yjs'
import type { Replica } from './replica'
import { h, saveFile } from './dom'
import { t, tn } from './i18n'
import { allFiles, allFolders, cleanPath, deletePath, exists, IMAGE_RE, mainFile, movePath, newFolder, putFile } from './projectfiles'

export interface FileTree {
  el: HTMLElement
  open(path: string): void
  current(): string | null
  /** Open the main file, and keep a file open from now on. */
  start(): void
}

const base = (p: string) => p.slice(p.lastIndexOf('/') + 1)
const parent = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)

export interface FileTreeMenu {
  preview(path: string): void
  /** Put LaTeX at the editor's cursor; absent when no .tex file is open. */
  insert(): ((text: string) => void) | null
}

export function createFileTree(r: Replica, writable: boolean, onOpen: (path: string | null) => void, menu: FileTreeMenu): FileTree {
  let current: string | null = null // open file
  let folder = '' // folder new things go into (selected folder, or the open file's folder)
  const KEY = `texloom.tree.${r.keys.project}`
  const collapsed = new Set<string>((() => { try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] } })())
  const saveCollapsed = () => { try { localStorage.setItem(KEY, JSON.stringify([...collapsed])) } catch {} }

  const list = h('ul', { class: 'files', role: 'tree' })
  const uploadIn = h('input', { type: 'file', multiple: true, hidden: true, onchange: (e: any) => { upload([...e.target.files], folder); e.target.value = '' } })
  const where = h('div', { class: 'where muted small' })
  const el = h('div', { class: 'filetree' },
    writable ? h('div', { class: 'tools' },
      h('button', { title: t('New file in the selected folder'), onclick: () => newFile(folder) }, t('+ File')),
      h('button', { title: t('New folder in the selected folder'), onclick: () => makeFolder(folder) }, t('+ Folder')),
      h('button', { title: t('Upload files into the selected folder'), onclick: () => uploadIn.click() }, t('Upload')),
      uploadIn) : null,
    where,
    list,
    h('div', { class: 'hint muted' }, writable ? t('Drag to move · drop files from your computer onto a folder to upload · ★ = main file') : ''),
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
      isPreviewable(path) && [t('Show in Preview'), () => menu.preview(path)],
      insert && latex !== null && !/[{}\\%#]/.test(path) && [t('Insert {{latex}}', { latex }), () => insert(latex)],
      writable && !isBin && path.endsWith('.tex') && [t('Set as main file'), () => r.meta.set('main', path)],
      [t('Download'), () => { download(path).catch((e) => alert(String(e))) }],
      writable && [t('Rename or move'), () => rename(path, false)],
      writable && [t('Delete'), () => remove(path, false)],
    )
  }

  function row(path: string, depth: number, isDir: boolean, open: boolean, isMain: boolean): HTMLElement {
    const isBin = r.bin.has(path)
    const icon = isDir ? (open ? '▾' : '▸') : isMain ? '★' : isBin ? '▣' : '¶'
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
      h('span', { class: 'icon' }, icon),
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
    const files = [...(e.dataTransfer?.files ?? [])]
    if (files.length) upload(files, dir)
  }

  function openFile(path: string | null) {
    current = path
    if (path) folder = parent(path)
    onOpen(path)
    render()
  }

  // ---- operations -----------------------------------------------------------
  function ask(label: string, def: string): string | null {
    const v = prompt(label, def)
    if (v === null) return null
    const p = cleanPath(v)
    if (!p) { alert(t('That isn’t a valid path. Use letters, numbers, - _ . and / between folders.')); return null }
    return p
  }

  function newFile(dir: string) {
    const p = ask(t('New file name (you can include folders, e.g. sections/intro.tex)'), join(dir, ''))
    if (!p) return
    if (exists(r, p)) return alert(t('{{path}} already exists.', { path: p }))
    r.files.set(p, new Y.Text(''))
    collapsed.delete(parent(p)); saveCollapsed()
    openFile(p)
  }

  function makeFolder(dir: string) {
    const p = ask(t('New folder name'), join(dir, ''))
    if (!p) return
    if (exists(r, p)) return alert(t('{{path}} already exists.', { path: p }))
    newFolder(r, p)
    folder = p
    render()
  }

  function rename(path: string, isDir: boolean) {
    const q = ask(isDir ? t('Rename or move folder to') : t('Rename or move file to'), path)
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

  async function upload(files: File[], dir: string) {
    for (const f of files) {
      const rel = (f as any).webkitRelativePath || f.name
      const path = cleanPath(join(dir, rel))
      if (!path) { alert(t('Skipped {{name}}: unsupported name.', { name: f.name })); continue }
      await putFile(r, path, new Uint8Array(await f.arrayBuffer()))
    }
  }

  r.files.observe(render); r.bin.observe(render); r.dirs.observe(render); r.meta.observe(render)
  render()
  return { el, open: openFile, current: () => current, start: () => { started = true; render() } }
}
