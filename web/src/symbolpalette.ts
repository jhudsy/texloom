// The symbols tab of the left-hand pane. It stays open while you work, and clicking a symbol types
// it at the editor's cursor; one that needs a package gets that package added to the preamble.
import type { EditorView } from '@codemirror/view'
import { h } from './dom'
import { t } from './i18n'
import { mathAt } from './mathpreview'
import { isDocument, isTexFile, mainFile } from './projectfiles'
import type { Replica } from './replica'
import { SYMBOLS, type Symbol } from './symbols'
import { stripComments } from './texscan'
import { changeOrSuggest, isSuggesting, suggested } from './suggestmode'

const LABELS: Record<string, () => string> = {
  greek: () => t('Greek'), relations: () => t('Relations'), operators: () => t('Operators'), large: () => t('Large operators'), arrows: () => t('Arrows'),
  delimiters: () => t('Delimiters'), misc: () => t('Miscellaneous'), letters: () => t('Letters'), structures: () => t('Accents and structures'), text: () => t('Text'),
}
const RECENT_KEY = 'texloom.symbolsRecent', MAX_RECENT = 16
const readRecent = (): string[] => { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]') } catch { return [] } }

/** Packages that a class or another package already provides. */
const PROVIDED_BY: Record<string, RegExp> = {
  amsmath: /^(?:amsmath|mathtools|beamer|amsart|amsbook|amsproc)$/,
  amssymb: /^(?:amssymb|beamer)$/,
}
const declared = (r: Replica): Set<string> => {
  const out = new Set<string>()
  r.files.forEach((text, p) => {
    if (!/\.(?:tex|ltx|sty|cls)$/i.test(p)) return
    for (const m of stripComments(text.toString()).matchAll(/\\(?:usepackage|RequirePackage|documentclass|LoadClass)\s*(?:\[[^\]]{0,300}\]\s*)?\{([^}]{0,2000})\}/g)) for (const n of m[1].split(',')) out.add(n.trim())
  })
  return out
}
const isDeclared = (have: Set<string>, pkg: string) => have.has(pkg) || [...have].some((n) => PROVIDED_BY[pkg]?.test(n))

/** Add \usepackage to a document's preamble: after the last one there, else after \documentclass.
 *  While suggesting, it's a suggestion. */
function addPackage(r: Replica, file: string, pkg: string): boolean {
  const yt = r.files.get(file)
  if (!yt) return false
  const clean = stripComments(yt.toString())
  const stop = clean.search(/\\begin\s*\{document\}/)
  const preamble = stop < 0 ? '' : clean.slice(0, stop)
  const lines = [...preamble.matchAll(/\\(?:usepackage|documentclass)\s*(?:\[[^\]]{0,300}\]\s*)?\{[^}]{0,2000}\}[^\n]*\n?/g)]
  const lastPackage = lines.at(-1)
  if (!lastPackage) return false
  const at = lastPackage.index + lastPackage[0].length
  if (suggested(r, file, `\\usepackage{${pkg}}`)) return true // already suggested
  changeOrSuggest(r, file, at, at, `${at > 0 && !lastPackage[0].endsWith('\n') ? '\n' : ''}\\usepackage{${pkg}}\n`)
  return true
}

export interface PaletteHost { r: Replica; view(): EditorView | null; file(): string | null; inserted(): void }

export function createSymbolPalette({ r, view, file, inserted }: PaletteHost): { el: HTMLElement; refresh(): void } {
  let group = SYMBOLS[0].id, query = ''
  const status = h('p', { class: 'muted small', role: 'status' })
  const search = h('input', { type: 'search', placeholder: t('Search symbols…'), 'aria-label': t('Search symbols…'), oninput: (e: any) => { query = e.target.value.trim().toLowerCase(); render() } })
  const tabs = h('div', { class: 'symtabs', role: 'group' })
  const recent = h('div', { class: 'symgrid' })
  const recentRow = h('div', { class: 'symrecent' }, h('div', { class: 'muted small' }, t('Recent')), recent)
  const grid = h('div', { class: 'symgrid symall' })
  const el = h('section', { class: 'sympalette', 'aria-label': t('Symbols'), onkeydown: (e: KeyboardEvent) => { if (e.key === 'Escape') view()?.focus() } },
    h('div', { class: 'symhead' }, search),
    tabs, recentRow, grid, status,
    h('p', { class: 'muted small' }, t('A dot means the symbol needs a package, which is added to your document when you use it.')))

  /** The document a package belongs in: the open one, else the main file. */
  const target = () => (isDocument(r, file()) ? file() : mainFile(r))

  /** Is the cursor inside maths ($…$, \\[…\\], an equation environment), not on its delimiters? */
  function inMath(v: EditorView, pos: number): boolean {
    const span = mathAt(v.state.doc.toString(), pos)
    return !!span && pos > span.from && pos < span.to
  }

  function insert(sym: Symbol, isText: boolean) {
    const v = view()
    if (!v || !isTexFile(file())) { status.textContent = t('Open a .tex file first.'); return }
    const { from, to } = v.state.selection.main
    const selected = v.state.sliceDoc(from, to)
    let text = sym.latex
    const argument = text.indexOf('{}')
    if (selected && argument >= 0) text = `${text.slice(0, argument + 1)}${selected}${text.slice(argument + 1)}`
    const wrap = !isText && !inMath(v, from)
    const next = v.state.sliceDoc(to, to + 1)
    if (!wrap && /[A-Za-z]$/.test(text) && /[A-Za-z]/.test(next)) text += ' ' // \alpha then x must not become \alphax
    const offset = wrap ? 1 : 0
    const cursor = argument >= 0 && !selected ? argument + 1 : text.length
    v.dispatch({ changes: { from, to, insert: wrap ? `$${text}$` : text }, selection: { anchor: from + offset + cursor } })
    v.focus()
    status.textContent = ''
    if (sym.pkg && !isDeclared(declared(r), sym.pkg)) {
      const line = `\\usepackage{${sym.pkg}}`, main = target()
      status.textContent = main && addPackage(r, main, sym.pkg)
        ? (isSuggesting() ? t('Suggested adding {{line}} to {{main}}.', { line, main }) : t('Added {{line}} to {{main}}.', { line, main }))
        : t('This symbol needs {{line}} in your preamble.', { line })
    }
    const list = [sym.latex, ...readRecent().filter((x) => x !== sym.latex)].slice(0, MAX_RECENT)
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)) } catch {}
    showRecent()
    if (!status.textContent) inserted() // on a phone, get out of the way unless there's something to read
  }

  const isTextSymbol = (sym: Symbol) => !!SYMBOLS.find((g) => g.symbols.includes(sym))?.text
  function button(sym: Symbol): HTMLElement {
    const title = sym.pkg ? t('{{cmd}} (needs {{line}})', { cmd: sym.latex, line: `\\usepackage{${sym.pkg}}` }) : sym.latex
    return h('button', {
      class: `sym${sym.pkg ? ' needs' : ''}`, title, 'aria-label': title,
      onmousedown: (e: Event) => e.preventDefault(), // keep the editor's selection
      onclick: () => insert(sym, isTextSymbol(sym)),
    }, sym.glyph)
  }
  const every = () => SYMBOLS.flatMap((g) => g.symbols)
  function showRecent() {
    const syms = readRecent().map((latex) => every().find((s) => s.latex === latex)).filter((s): s is Symbol => !!s)
    recent.replaceChildren(...syms.map(button))
    recentRow.hidden = !syms.length || !!query
  }
  function render() {
    tabs.hidden = !!query
    tabs.replaceChildren(...SYMBOLS.map((g) => h('button', { class: `small${g.id === group ? ' on' : ''}`, 'aria-pressed': String(g.id === group), onclick: () => { group = g.id; render() } }, LABELS[g.id]())))
    const shown = query ? every().filter((s) => s.latex.toLowerCase().includes(query) || s.glyph === query) : SYMBOLS.find((g) => g.id === group)!.symbols
    grid.replaceChildren(...(shown.length ? shown.map(button) : [h('p', { class: 'muted small' }, t('No symbols found.'))]))
    showRecent()
  }

  return { el, refresh() { status.textContent = ''; render() } }
}
