// The editor's right-click menu, for selected text and LaTeX commands. Everywhere else the
// browser's own menu appears, because that's where its spelling suggestions are: a page can't
// read them, or add to that menu. Shift + right-click always gives the browser's menu.
import { EditorSelection, type SelectionRange, type Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { menuItems, openContextMenu, type MenuItem } from './contextmenu'
import { t } from './i18n'
import { CITE_CMDS, REF_CMDS, type ProjectIndex } from './latex'
import { allFiles, isTexFile } from './projectfiles'
import type { Replica } from './replica'
import { COMMENT } from './texscan'

type Hit =
  | { kind: 'ref' | 'label' | 'cite'; key: string }
  | { kind: 'file'; name: string; command: string }
  | { kind: 'command' }

const FILES = /^(?:input|include|subfile|includegraphics|includepdf|bibliography|addbibresource|lstinputlisting|verbatiminput)$/
const TOKEN = /\\\\|\\([A-Za-z@]+)\*?\s*((?:\[[^\]\n]*\]\s*)*)(\{([^{}\n]*)\})?/g

const isComment = (line: string, offset: number) => {
  const at = line.search(COMMENT)
  return at >= 0 && offset > at
}

/** The piece of a comma-separated argument at `at`, or else its first piece. */
function pieceAt(arg: string, at: number): string {
  let from = 0
  const pieces = arg.split(',')
  for (const piece of pieces) {
    if (at >= from && at <= from + piece.length && piece.trim()) return piece.trim()
    from += piece.length + 1
  }
  return pieces.map((p) => p.trim()).find(Boolean) ?? ''
}

/** The LaTeX command at `offset` in `line`. Only command names and arguments that aren't prose
 *  count: the text in \emph{…} or \section{…} keeps the browser's spelling menu. */
function commandAt(line: string, offset: number): Hit | null {
  if (isComment(line, offset)) return null
  for (const m of line.matchAll(TOKEN)) {
    const name = m[1]
    if (!name || offset < m.index || offset > m.index + m[0].length) continue
    const arg = m[4]
    const at = arg === undefined ? 0 : offset - (m.index + m[0].length - 1 - arg.length)
    const key = arg === undefined ? '' : pieceAt(arg, at)
    if (key && name === 'label') return { kind: 'label', key }
    if (key && REF_CMDS.test(name)) return { kind: 'ref', key }
    if (key && CITE_CMDS.test(name)) return { kind: 'cite', key }
    if (key && FILES.test(name)) return { kind: 'file', name: key, command: name }
    return offset <= m.index + 1 + name.length ? { kind: 'command' } : null
  }
  return null
}

export interface EditorMenuHost {
  writable: boolean
  review(kind: 'comment' | 'suggestion'): void
  showInPdf(): void
  search(text: string): void
  symbols(): void
  ask(): void
  /** What to do for a target, or null when the project doesn't have it. */
  gotoLabel(key: string): (() => void) | null
  gotoCitation(key: string): (() => void) | null
  openFile(name: string, command: string, from: string): { path: string; open: () => void } | null
  file(): string | null
}

function selectionItems(host: EditorMenuHost, text: string, canShow: boolean): MenuItem[] {
  return menuItems(
    canShow && [t('Show in PDF'), host.showInPdf],
    host.writable && [t('Add a comment…'), () => host.review('comment')],
    host.writable && [t('Suggest changes (suggest mode)'), () => host.review('suggestion')],
    !text.includes('\n') && [t('Search the project'), () => host.search(text)],
    host.writable && isTexFile(host.file()) && [t('Insert a symbol…'), host.symbols],
    [t('Ask the assistant'), host.ask],
  )
}

function commandItems(host: EditorMenuHost, hit: Hit, canShow: boolean): MenuItem[] {
  const out: MenuItem[] = []
  if (hit.kind === 'ref') {
    const go = host.gotoLabel(hit.key)
    if (go) out.push([t('Go to label {{key}}', { key: hit.key }), go])
  }
  else if (hit.kind === 'label') out.push([t('Find uses of {{key}}', { key: hit.key }), () => host.search(hit.key)])
  else if (hit.kind === 'cite') {
    const go = host.gotoCitation(hit.key)
    if (go) out.push([t('Go to bibliography entry {{key}}', { key: hit.key }), go])
  }
  else if (hit.kind === 'file') {
    const target = host.openFile(hit.name, hit.command, host.file() ?? '')
    if (target) out.push([t('Open {{file}}', { file: target.path }), target.open])
  }
  if (canShow) out.push([t('Show in PDF'), host.showInPdf])
  if (host.writable) out.push([t('Insert a symbol…'), host.symbols]) // a command is only found in a .tex file
  return out
}

export function editorMenu(host: EditorMenuHost): Extension {
  let before: SelectionRange | null = null // the selection when the button went down: some systems select the word under a right-click
  return EditorView.domEventHandlers({
    mousedown(e, view) {
      if (e.button === 2 || (e.button === 0 && e.ctrlKey)) before = view.state.selection.main
      return false
    },
    contextmenu(e, view) {
      const prior = before
      before = null
      if (!prior || e.shiftKey) return false
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY })
      if (pos === null) return false
      const doc = view.state.doc
      const start = doc.toString().indexOf('\\begin{document}')
      const inBody = start < 0 || pos > start
      const canShow = isTexFile(host.file()) && inBody
      const inside = !prior.empty && pos >= prior.from && pos <= prior.to
      let items: MenuItem[]
      if (inside) items = selectionItems(host, doc.sliceString(prior.from, prior.to), canShow)
      else {
        const line = doc.lineAt(pos)
        const hit = isTexFile(host.file()) ? commandAt(line.text, pos - line.from) : null
        items = hit ? commandItems(host, hit, canShow) : []
      }
      if (!items.length) return false
      e.preventDefault()
      view.dispatch({ selection: inside ? EditorSelection.single(prior.anchor, prior.head) : EditorSelection.cursor(pos) })
      openContextMenu(e.clientX, e.clientY, items)
      return true
    },
  })
}

function findFile(all: Set<string>, name: string, command: string, from: string): string | null {
  const exts = command === 'includegraphics' ? ['', '.pdf', '.png', '.jpg', '.jpeg', '.eps', '.svg'] : /bib/.test(command) ? ['', '.bib'] : ['', '.tex']
  const clean = name.replace(/^\.\//, '')
  const dir = from.includes('/') ? from.slice(0, from.lastIndexOf('/') + 1) : ''
  for (const base of ['', dir]) for (const ext of exts) if (all.has(base + clean + ext)) return base + clean + ext
  // e.g. a folder named by \graphicspath
  for (const ext of exts) {
    const found = [...all].filter((p) => p.endsWith(`/${clean}${ext}`))
    if (found.length === 1) return found[0]
  }
  return null
}

/** A project's labels, citations and files, as targets for the menu. */
export function menuTargets(r: Replica, index: ProjectIndex, goto: (file: string, line: number) => void, open: (path: string) => void) {
  return {
    gotoLabel(key: string) {
      const found = index.get().labels.find((l) => l.key === key)
      return found ? () => goto(found.file, found.line) : null
    },
    gotoCitation(key: string) {
      const found = index.get().bib.find((b) => b.key === key)
      return found ? () => goto(found.file, found.line) : null
    },
    openFile(name: string, command: string, from: string) {
      const path = findFile(new Set(allFiles(r)), name, command, from)
      return path ? { path, open: () => open(path) } : null
    },
  }
}
