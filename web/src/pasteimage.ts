// Paste or drop an image into the editor: it's added to the project (figs/ or an
// existing figures folder) and \includegraphics is inserted where it landed.
import { EditorView } from '@codemirror/view'
import type { Extension } from '@codemirror/state'
import type { Replica } from './replica'
import { allFolders, cleanPath } from './projectfiles'
import { t } from './i18n'

const GRAPHIC = /\.(png|jpe?g|pdf|eps|svg|gif|webp)$/i
const MAX = 25 * 1024 * 1024

function folderFor(r: Replica): string {
  const folders = allFolders(r)
  return ['figs', 'figures', 'images', 'img', 'graphics'].find((f) => folders.includes(f)) ?? 'figs'
}

function nameFor(r: Replica, dir: string, f: File): string {
  const ext = (/\.([A-Za-z0-9]+)$/.exec(f.name)?.[1] ?? f.type.split('/')[1] ?? 'png').toLowerCase().replace('jpeg', 'jpg').replace('svg+xml', 'svg')
  const generic = !f.name || /^(image|unknown|clipboard|screenshot)?[-_ ]?\d*\.\w+$/i.test(f.name)
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-')
  // LaTeX dislikes spaces and extra dots in graphics names
  const stem = generic ? `pasted-${stamp}` : f.name.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || `image-${stamp}`
  let path = `${dir}/${stem}.${ext}`
  for (let i = 2; r.files.has(path) || r.bin.has(path); i++) path = `${dir}/${stem}-${i}.${ext}`
  return path
}

async function addImages(r: Replica, view: EditorView, files: File[], pos: number) {
  const dir = folderFor(r)
  const paths: string[] = []
  for (const f of files) {
    if (f.size > MAX) { alert(t('{{name}} is larger than 25 MB; skipped.', { name: f.name })); continue }
    const path = cleanPath(nameFor(r, dir, f))
    if (!path) continue
    const data = new Uint8Array(await f.arrayBuffer())
    const id = await r.putBlob(data)
    r.doc.transact(() => r.bin.set(path, { h: id, size: data.length }))
    paths.push(path)
  }
  if (!paths.length) return
  const line = view.state.doc.lineAt(Math.min(pos, view.state.doc.length))
  const blankLine = !line.text.trim()
  const graphics = paths.map((p) => `\\includegraphics[width=\\linewidth]{${p}}`)
  const label = (p: string) => p.slice(p.lastIndexOf('/') + 1).replace(/\.[^.]+$/, '')
  // on an empty line outside a figure, make a whole figure; otherwise just the graphic
  const inFigure = /\\begin\{figure\*?\}(?![\s\S]*\\end\{figure\*?\})/.test(view.state.doc.sliceString(Math.max(0, pos - 3000), pos))
  const insert = blankLine && !inFigure
    ? `\\begin{figure}[htbp]\n  \\centering\n  ${graphics.join('\n  ')}\n  \\caption{}\n  \\label{fig:${label(paths[0])}}\n\\end{figure}`
    : graphics.join('\n')
  const at = blankLine ? line.from : pos
  const to = blankLine ? line.to : pos
  const caret = insert.indexOf('\\caption{}')
  view.dispatch({
    changes: { from: at, to, insert },
    selection: { anchor: caret >= 0 ? at + caret + '\\caption{'.length : at + insert.length },
    userEvent: 'input.paste',
  })
  view.focus()
}

const images = (list: FileList | null | undefined) => [...(list ?? [])].filter((f) => f.type.startsWith('image/') || f.type === 'application/pdf' || GRAPHIC.test(f.name))

export function imageDropExtension(r: Replica, writable: boolean): Extension {
  if (!writable) return []
  return EditorView.domEventHandlers({
    paste(e, view) {
      const files = images(e.clipboardData?.files)
      if (!files.length) return false
      e.preventDefault()
      addImages(r, view, files, view.state.selection.main.head).catch((err) => alert(t('Couldn’t add the image: {{error}}', { error: err })))
      return true
    },
    drop(e, view) {
      const files = images(e.dataTransfer?.files)
      if (!files.length) return false
      e.preventDefault()
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY }) ?? view.state.selection.main.head
      addImages(r, view, files, pos).catch((err) => alert(t('Couldn’t add the image: {{error}}', { error: err })))
      return true
    },
  })
}
