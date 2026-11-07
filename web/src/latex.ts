// LaTeX editing support for CodeMirror: completion (commands, environments with
// their bodies, image paths, citation keys, labels, files, packages), automatic
// \end{…}, formatting shortcuts, and warnings for missing images/citations/labels.
import { autocompletion, snippetCompletion, type Completion, type CompletionContext, type CompletionResult } from '@codemirror/autocomplete'
import { linter, type Diagnostic } from '@codemirror/lint'
import { EditorSelection, Prec, type Extension } from '@codemirror/state'
import { EditorView, keymap } from '@codemirror/view'
import { parseId } from './references'
import type { Replica } from './replica'
import type { Problem } from './latexlog'
import { IMAGE_RE } from './projectfiles'

// ---------------------------------------------------------------------------
// Project index: labels, bibliography keys, images, files and macros, rebuilt
// lazily after the project changes.
// ---------------------------------------------------------------------------
export interface BibEntry { key: string; type: string; title?: string; file: string }
export interface Label { key: string; file: string; line: number }

export class ProjectIndex {
  private dirty = true
  labels: Label[] = []
  bib: BibEntry[] = []
  images: string[] = []
  texFiles: string[] = []
  bibFiles: string[] = []
  macros: string[] = []
  constructor(private r: Replica) {
    r.files.observeDeep(() => (this.dirty = true))
    r.bin.observe(() => (this.dirty = true))
  }
  get(): this {
    if (!this.dirty) return this
    this.dirty = false
    const labels: Label[] = [], bib: BibEntry[] = [], macros = new Set<string>()
    const tex: string[] = [], bibs: string[] = []
    this.r.files.forEach((yt, path) => {
      const text = yt.toString()
      if (/\.bib$/i.test(path)) {
        bibs.push(path)
        for (const m of text.matchAll(/@(\w+)\s*[{(]\s*([^,\s{}()"#%'=]+)\s*,/g)) {
          const type = m[1].toLowerCase()
          if (type === 'string' || type === 'comment' || type === 'preamble') continue
          // this entry: up to the next '@' (bounded, so a crafted file can't make this quadratic)
          const from = m.index! + m[0].length, next = text.indexOf('@', from)
          const rest = text.slice(from, Math.min(next < 0 ? text.length : next, from + 20000))
          const t = bibField(rest, 'title')
          bib.push({ key: m[2], type, file: path, title: t?.replace(/[{}]/g, '').replace(/\s+/g, ' ').trim() })
        }
      } else if (/\.(tex|sty|cls|ltx)$/i.test(path)) {
        if (/\.tex$/i.test(path)) tex.push(path)
        const lines = text.split('\n')
        lines.forEach((l, i) => {
          for (const m of l.matchAll(/\\label\{([^}]+)\}/g)) labels.push({ key: m[1], file: path, line: i + 1 })
        })
        for (const m of text.matchAll(/\\(?:re|provide)?newcommand\*?\s*\{?\\([A-Za-z@]+)|\\DeclareMathOperator\*?\s*\{\\([A-Za-z]+)\}|\\def\s*\\([A-Za-z@]+)/g))
          macros.add(m[1] ?? m[2] ?? m[3])
      }
    })
    this.labels = labels
    this.bib = bib
    this.texFiles = tex.sort()
    this.bibFiles = bibs.sort()
    this.images = [...this.r.bin.keys(), ...this.r.files.keys()].filter((p) => IMAGE_RE.test(p)).sort()
    this.macros = [...macros].sort()
    return this
  }
  /** Does an \includegraphics argument resolve to a project file (extension optional)? */
  hasImage(arg: string): boolean {
    const a = arg.replace(/^\.\//, '')
    return this.images.some((p) => p === a || p.replace(IMAGE_RE, '') === a || p.endsWith('/' + a) || p.replace(IMAGE_RE, '').endsWith('/' + a))
  }
}

// ---------------------------------------------------------------------------
// Static data
// ---------------------------------------------------------------------------
type Env = [name: string, body: string, info: string]
// Bodies use CodeMirror snippet syntax: ${} fields, Tab to move between them.
const ENVS: Env[] = [
  ['itemize', '\t\\item ${}', 'bulleted list'],
  ['enumerate', '\t\\item ${}', 'numbered list'],
  ['description', '\t\\item[${term}] ${}', 'description list'],
  ['figure', '\t\\centering\n\t\\includegraphics[width=0.8\\linewidth]{${file}}\n\t\\caption{${caption}}\n\t\\label{fig:${label}}', 'figure with image, caption, label'],
  ['table', '\t\\centering\n\t\\begin{tabular}{${ll}}\n\t\t${} \\\\\n\t\\end{tabular}\n\t\\caption{${caption}}\n\t\\label{tab:${label}}', 'table with tabular, caption, label'],
  ['tabular', '\t${} \\\\', 'columns in {…}'],
  ['equation', '\t${}', 'numbered equation'],
  ['equation*', '\t${}', 'unnumbered equation'],
  ['align', '\t${} &= ${} \\\\', 'aligned equations (amsmath)'],
  ['align*', '\t${} &= ${} \\\\', 'aligned equations, unnumbered'],
  ['gather', '\t${}', 'centred equations (amsmath)'],
  ['multline', '\t${}', 'long equation (amsmath)'],
  ['cases', '\t${} & ${} \\\\', 'piecewise (amsmath)'],
  ['matrix', '\t${} & ${} \\\\', 'matrix (amsmath)'],
  ['pmatrix', '\t${} & ${} \\\\', 'matrix in ( )'],
  ['abstract', '\t${}', ''],
  ['quote', '\t${}', ''],
  ['quotation', '\t${}', ''],
  ['verbatim', '${}', ''],
  ['center', '\t${}', ''],
  ['flushleft', '\t${}', ''],
  ['flushright', '\t${}', ''],
  ['minipage', '\t${}', 'needs {width}'],
  ['theorem', '\t${}', ''],
  ['lemma', '\t${}', ''],
  ['proof', '\t${}', ''],
  ['definition', '\t${}', ''],
  ['example', '\t${}', ''],
  ['frame', '\t\\frametitle{${title}}\n\t${}', 'beamer slide'],
  ['columns', '\t\\column{0.5\\textwidth}\n\t${}\n\t\\column{0.5\\textwidth}\n\t${}', 'beamer columns'],
  ['tikzpicture', '\t${}', 'TikZ drawing'],
  ['lstlisting', '${}', 'code listing (listings)'],
  ['minted', '${}', 'code listing (minted); needs {language}'],
  ['thebibliography', '\t\\bibitem{${key}} ${}', 'manual bibliography; needs {99}'],
  ['document', '${}', ''],
]
const ENV_ARGS: Record<string, string> = { minipage: '{${0.45\\textwidth}}', minted: '{${python}}', thebibliography: '{${99}}', tabular: '{${ll}}' }
const ENV_OPTS: Record<string, string> = { figure: '[htbp]', table: '[htbp]' }

const envSnippet = ([name, body]: Env) => `\\begin{${name}}${ENV_OPTS[name] ?? ''}${ENV_ARGS[name] ?? ''}\n${body}\n\\end{${name}}`

type Cmd = [label: string, template: string, info?: string]
const COMMANDS: Cmd[] = [
  ['\\section', '\\section{${}}'], ['\\section*', '\\section*{${}}'], ['\\subsection', '\\subsection{${}}'],
  ['\\subsubsection', '\\subsubsection{${}}'], ['\\paragraph', '\\paragraph{${}}'], ['\\chapter', '\\chapter{${}}'],
  ['\\textbf', '\\textbf{${}}', 'bold'], ['\\textit', '\\textit{${}}', 'italic'], ['\\emph', '\\emph{${}}', 'emphasis'],
  ['\\texttt', '\\texttt{${}}', 'monospace'], ['\\textsc', '\\textsc{${}}', 'small caps'], ['\\underline', '\\underline{${}}'],
  ['\\label', '\\label{${}}'], ['\\ref', '\\ref{${}}'], ['\\eqref', '\\eqref{${}}'], ['\\autoref', '\\autoref{${}}'], ['\\cref', '\\cref{${}}'],
  ['\\cite', '\\cite{${}}'], ['\\citep', '\\citep{${}}'], ['\\citet', '\\citet{${}}'], ['\\parencite', '\\parencite{${}}'], ['\\textcite', '\\textcite{${}}'],
  ['\\includegraphics', '\\includegraphics[width=${0.8}\\linewidth]{${}}', 'image'],
  ['\\input', '\\input{${}}'], ['\\include', '\\include{${}}'], ['\\usepackage', '\\usepackage{${}}'],
  ['\\item', '\\item ${}'], ['\\caption', '\\caption{${}}'], ['\\centering', '\\centering'],
  ['\\footnote', '\\footnote{${}}'], ['\\url', '\\url{${}}'], ['\\href', '\\href{${url}}{${text}}'],
  ['\\frac', '\\frac{${}}{${}}'], ['\\sqrt', '\\sqrt{${}}'], ['\\sum', '\\sum_{${}}^{${}}'], ['\\int', '\\int_{${}}^{${}}'],
  ['\\left(', '\\left( ${} \\right)'], ['\\mathbb', '\\mathbb{${}}'], ['\\mathcal', '\\mathcal{${}}'], ['\\mathrm', '\\mathrm{${}}'],
  ['\\newcommand', '\\newcommand{\\${name}}[${1}]{${}}'], ['\\renewcommand', '\\renewcommand{\\${name}}{${}}'],
  ['\\maketitle', '\\maketitle'], ['\\tableofcontents', '\\tableofcontents'], ['\\title', '\\title{${}}'], ['\\author', '\\author{${}}'], ['\\date', '\\date{${}}'],
  ['\\bibliography', '\\bibliography{${}}'], ['\\bibliographystyle', '\\bibliographystyle{${plain}}'],
  ['\\addbibresource', '\\addbibresource{${}}'], ['\\printbibliography', '\\printbibliography'],
  ['\\hline', '\\hline'], ['\\toprule', '\\toprule'], ['\\midrule', '\\midrule'], ['\\bottomrule', '\\bottomrule'],
  ['\\vspace', '\\vspace{${}}'], ['\\hspace', '\\hspace{${}}'], ['\\newpage', '\\newpage'], ['\\clearpage', '\\clearpage'],
  ['\\ldots', '\\ldots'], ['\\quad', '\\quad'], ['\\qquad', '\\qquad'], ['\\noindent', '\\noindent'],
  ['\\documentclass', '\\documentclass{${article}}'], ['\\begin', '\\begin{${}}'], ['\\end', '\\end{${}}'],
  ...['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'lambda', 'mu', 'pi', 'sigma', 'theta', 'phi', 'omega', 'infty', 'leq', 'geq', 'neq', 'approx', 'cdot', 'times', 'in', 'subseteq', 'forall', 'exists', 'rightarrow', 'Rightarrow', 'mapsto']
    .map((s): Cmd => [`\\${s}`, `\\${s}`]),
]
const PACKAGES = ['amsmath', 'amssymb', 'amsthm', 'graphicx', 'hyperref', 'cleveref', 'booktabs', 'tabularx', 'longtable', 'multirow', 'xcolor',
  'tikz', 'pgfplots', 'geometry', 'fontenc', 'inputenc', 'babel', 'csquotes', 'biblatex', 'natbib', 'listings', 'minted', 'algorithm2e',
  'algorithmic', 'algorithm', 'subcaption', 'caption', 'float', 'enumitem', 'siunitx', 'microtype', 'lmodern', 'mathtools', 'url', 'todonotes', 'xspace', 'lipsum']

const CITE_CMDS = /^(no|paren|text|auto|foot|full|super|smart)?cite[pt]?\*?$|^cite(author|year|title|num)\*?$|^[Cc]ite[pt]?$|^[Aa]utocite$/
const REF_CMDS = /^(ref|eqref|autoref|[cC]ref|pageref|nameref|vref|Vref|cpageref|labelcref)\*?$/

// ---------------------------------------------------------------------------
// Completion
// ---------------------------------------------------------------------------
export type AddReference = (seed: string, insert: (key: string) => void) => void

function completionSource(index: ProjectIndex, addReference?: AddReference) {
  const envOptions: Completion[] = ENVS.map((e) => snippetCompletion(envSnippet(e), { label: `\\begin{${e[0]}}`, detail: e[2] || 'environment', type: 'keyword', boost: 2 }))
  const cmdOptions: Completion[] = COMMANDS.map(([label, tpl, info]) => snippetCompletion(tpl, { label, detail: info, type: 'function' }))

  return (ctx: CompletionContext): CompletionResult | null => {
    const line = ctx.state.doc.lineAt(ctx.pos)
    const before = line.text.slice(0, ctx.pos - line.from)
    const after = line.text.slice(ctx.pos - line.from)
    const closeTo = after.startsWith('}') ? ctx.pos + 1 : ctx.pos // replace an auto-inserted }

    // inside an argument: \cmd[opt]{partial  (comma-separated for \cite)
    const arg = /\\([A-Za-z]+\*?)(?:\[[^\]]*\])*\{([^{}]*)$/.exec(before)
    if (arg) {
      const [, cmd, partial] = arg
      const word = partial.slice(partial.lastIndexOf(',') + 1).trimStart()
      const from = ctx.pos - word.length
      const idx = index.get()
      // Accepting adds the closing brace if it isn't there yet (and steps past it).
      const close = after.startsWith('}') ? '' : '}'
      const multi = CITE_CMDS.test(cmd) || REF_CMDS.test(cmd) // room for ", more" inside the braces
      const withClose = (o: Completion): Completion => ({ ...o, apply: (view, c, f, t) => {
        const insert = c.label + close
        const anchor = multi ? f + c.label.length : f + c.label.length + 1
        view.dispatch({ changes: { from: f, to: t, insert }, selection: { anchor }, userEvent: 'input.complete' })
      } })
      const list = (opts: Completion[]): CompletionResult => ({ from, to: ctx.pos, options: opts.map(withClose), validFor: /^[^,{}]*$/ })
      if (cmd === 'begin' || cmd === 'end') {
        const names = cmd === 'end' ? openEnvs(ctx.state.doc.sliceString(0, line.from)).reverse() : ENVS.map((e) => e[0])
        if (cmd === 'end') return { from, to: closeTo, options: names.map((n) => ({ label: n, apply: `${n}}`, type: 'keyword' })) }
        return {
          from, to: closeTo, validFor: /^[\w*]*$/,
          options: ENVS.map((e) => snippetCompletion(`${e[0]}}${ENV_OPTS[e[0]] ?? ''}${ENV_ARGS[e[0]] ?? ''}\n${e[1]}\n\\end{${e[0]}}`, { label: e[0], detail: e[2], type: 'keyword' })),
        }
      }
      if (cmd === 'includegraphics')
        return list(idx.images.map((p) => ({ label: p, type: 'variable', detail: 'image' })))
      if (CITE_CMDS.test(cmd)) {
        const fetchRef = (seed: string, label: string): Completion => ({ label, type: 'text', boost: -50, apply: (view, _c, f, t) => {
          addReference!(seed, (key) => view.dispatch({ changes: { from: f, to: t, insert: key + close }, selection: { anchor: f + key.length }, userEvent: 'input.complete' }))
        } })
        // typing a DOI or arXiv id offers to fetch it
        if (addReference && parseId(word)) return { from, to: ctx.pos, filter: false, options: [fetchRef(word, `Fetch reference for ${word}`)] }
        const res = list(idx.bib.map((b) => ({ label: b.key, type: 'constant', detail: b.title ? truncate(b.title, 60) : b.type, info: b.title ? `${b.title} (${b.file})` : b.file })))
        return addReference && !word ? { ...res, options: [...res.options, fetchRef('', '+ Add from DOI / arXiv…')] } : res
      }
      if (REF_CMDS.test(cmd))
        return list(idx.labels.map((l) => ({ label: l.key, type: 'property', detail: `${l.file}:${l.line}` })))
      if (cmd === 'input' || cmd === 'include' || cmd === 'subfile' || cmd === 'subfileinclude')
        return list(idx.texFiles.map((p) => ({ label: cmd === 'include' ? p.replace(/\.tex$/i, '') : p, type: 'variable' })))
      if (cmd === 'bibliography' || cmd === 'addbibresource')
        return list(idx.bibFiles.map((p) => ({ label: cmd === 'bibliography' ? p.replace(/\.bib$/i, '') : p, type: 'variable' })))
      if (cmd === 'usepackage' || cmd === 'RequirePackage')
        return list(PACKAGES.map((p) => ({ label: p, type: 'namespace' })))
      return null
    }

    // a command being typed: \partial
    const m = /\\[A-Za-z]*\*?$/.exec(before)
    if (!m || (m[0].length < 2 && !ctx.explicit)) return null
    const project = index.get().macros.map((name): Completion => ({ label: `\\${name}`, type: 'function', detail: 'defined in project' }))
    return { from: ctx.pos - m[0].length, options: [...envOptions, ...cmdOptions, ...project], validFor: /^\\[A-Za-z]*\*?$/ }
  }
}

/** Value of a BibTeX field, honouring nested braces: title = {A {B} C} or "…". */
function bibField(entry: string, name: string): string | undefined {
  const m = new RegExp(`\\b${name}\\s*=\\s*([{"])`, 'i').exec(entry)
  if (!m) return undefined
  let i = m.index + m[0].length
  if (m[1] === '"') { const end = entry.indexOf('"', i); return end < 0 ? undefined : entry.slice(i, end) }
  let depth = 1
  for (let j = i; j < entry.length; j++) {
    if (entry[j] === '{') depth++
    else if (entry[j] === '}' && --depth === 0) return entry.slice(i, j)
  }
  return undefined
}

const truncate = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

/** Environments opened (and not closed) in `text`, innermost last. */
function openEnvs(text: string): string[] {
  const stack: string[] = []
  for (const m of text.matchAll(/\\(begin|end)\{([^}]+)\}/g)) {
    if (m[1] === 'begin') stack.push(m[2])
    else { const i = stack.lastIndexOf(m[2]); if (i >= 0) stack.splice(i, 1) }
  }
  return stack
}

// ---------------------------------------------------------------------------
// Typing \begin{env} and pressing Enter adds the matching \end{env}.
// ---------------------------------------------------------------------------
const autoEnd = keymap.of([{
  key: 'Enter',
  run(view) {
    const { state } = view
    const sel = state.selection.main
    if (!sel.empty || state.readOnly) return false
    const line = state.doc.lineAt(sel.head)
    const before = line.text.slice(0, sel.head - line.from)
    const m = /\\begin\{([^}]+)\}(?:\[[^\]]*\])?(?:\{[^}]*\})*\s*$/.exec(before)
    if (!m || line.text.slice(sel.head - line.from).trim()) return false
    const env = m[1]
    // only if this \begin isn't already closed further down
    const opened = openEnvs(state.doc.toString())
    if (!opened.includes(env)) return false
    const indent = /^\s*/.exec(line.text)![0]
    const inner = indent + '\t'
    const body = env === 'itemize' || env === 'enumerate' ? '\\item ' : ''
    const insert = `\n${inner}${body}\n${indent}\\end{${env}}`
    view.dispatch({
      changes: { from: sel.head, insert },
      selection: { anchor: sel.head + 1 + inner.length + body.length },
      scrollIntoView: true, userEvent: 'input',
    })
    return true
  },
}])

// ---------------------------------------------------------------------------
// Formatting shortcuts: Mod-B / Mod-I wrap the selection (or insert an empty
// command), and unwrap if it's already wrapped.
// ---------------------------------------------------------------------------
function wrapWith(cmd: string) {
  return (view: EditorView) => {
    if (view.state.readOnly) return false
    const open = `\\${cmd}{`
    const tr = view.state.changeByRange((range) => {
      const doc = view.state.doc
      const pre = doc.sliceString(Math.max(0, range.from - open.length), range.from)
      const post = doc.sliceString(range.to, range.to + 1)
      if (pre === open && post === '}') { // unwrap
        return {
          changes: [{ from: range.from - open.length, to: range.from }, { from: range.to, to: range.to + 1 }],
          range: EditorSelection.range(range.from - open.length, range.to - open.length),
        }
      }
      return {
        changes: [{ from: range.from, insert: open }, { from: range.to, insert: '}' }],
        range: range.empty ? EditorSelection.cursor(range.from + open.length) : EditorSelection.range(range.from + open.length, range.to + open.length),
      }
    })
    view.dispatch(view.state.update(tr, { scrollIntoView: true, userEvent: 'input' }))
    return true
  }
}

// ---------------------------------------------------------------------------
// Warnings for references that don't resolve.
// ---------------------------------------------------------------------------
function referenceLinter(index: ProjectIndex, compileProblems: () => Problem[]) {
  return linter((view) => {
    const idx = index.get()
    const text = view.state.doc.toString()
    const out: Diagnostic[] = []
    // problems from the last compile, for this file
    const doc = view.state.doc
    for (const p of compileProblems()) {
      if (!p.line || p.line > doc.lines) continue
      const l = doc.line(p.line)
      out.push({ from: l.from, to: Math.max(l.from, l.to), severity: p.severity === 'error' ? 'error' : p.severity === 'warning' ? 'warning' : 'info',
        message: `${p.severity === 'box' ? 'Layout' : 'Compile'}: ${p.message}`, source: 'LaTeX' })
    }
    const keys = new Set(idx.bib.map((b) => b.key))
    const labels = new Set(idx.labels.map((l) => l.key))
    const commented = commentedAt(text)
    // bounded repeats: unbounded ones make crafted input (e.g. many unclosed "\a[") quadratic
    for (const m of text.matchAll(/\\([A-Za-z]+\*?)(?:\[[^\]\n]{0,300}\]){0,4}\{([^{}]{0,2000})\}/g)) {
      const [, cmd, argRaw] = m
      const argFrom = m.index! + m[0].length - 1 - argRaw.length
      if (commented(m.index!)) continue
      if (cmd === 'includegraphics') {
        const a = argRaw.trim()
        if (a && !a.includes('#') && !a.includes('\\') && !idx.hasImage(a))
          out.push({ from: argFrom, to: argFrom + argRaw.length, severity: 'warning', message: `No image “${a}” in this project.` })
      } else if (CITE_CMDS.test(cmd) && idx.bibFiles.length) {
        let off = 0
        for (const part of argRaw.split(',')) {
          const k = part.trim()
          const start = argFrom + off + part.indexOf(k)
          if (k && k !== '*' && !keys.has(k) && !k.includes('\\') && !k.includes('#'))
            out.push({ from: start, to: start + k.length, severity: 'warning', message: `No bibliography entry “${k}” in ${idx.bibFiles.join(', ')}.` })
          off += part.length + 1
        }
      } else if (REF_CMDS.test(cmd)) {
        for (const part of argRaw.split(',')) {
          const k = part.trim()
          if (k && !labels.has(k) && !k.includes('#') && !k.includes('\\')) {
            const start = argFrom + argRaw.indexOf(k)
            out.push({ from: start, to: start + k.length, severity: 'warning', message: `No \\label{${k}} in this project.` })
          }
        }
      }
    }
    return out
  }, { delay: 600 })
}

/** Is a position inside a % comment? Line starts are found once, so this stays linear
 *  even for a huge single-line file. */
function commentedAt(text: string): (pos: number) => boolean {
  const starts = [0]
  for (let i = text.indexOf('\n'); i >= 0; i = text.indexOf('\n', i + 1)) starts.push(i + 1)
  const pct = new Map<number, number>() // line start -> first comment character (or Infinity)
  return (pos) => {
    let lo = 0, hi = starts.length - 1
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (starts[mid] <= pos) lo = mid; else hi = mid - 1 }
    const s = starts[lo]
    let c = pct.get(s)
    if (c === undefined) {
      const m = /(^|[^\\])%/.exec(text.slice(s, starts[lo + 1] ?? text.length))
      c = m ? s + m.index + m[1].length : Infinity
      pct.set(s, c)
    }
    return c < pos
  }
}

// ---------------------------------------------------------------------------
/** All LaTeX editing support. `onCompile` runs on Mod-S. */
export function latexSupport(index: ProjectIndex, opts: { onCompile: () => void; onSync: () => void; problems: () => Problem[]; addReference?: AddReference }): Extension {
  return [
    autocompletion({ override: [completionSource(index, opts.addReference)], activateOnTyping: true, icons: false }),
    Prec.highest(keymap.of([
      { key: 'Mod-s', run: () => { opts.onCompile(); return true }, preventDefault: true },
      { key: 'Mod-Alt-ArrowRight', run: () => { opts.onSync(); return true }, preventDefault: true },
      { key: 'Mod-b', run: wrapWith('textbf'), preventDefault: true },
      { key: 'Mod-i', run: wrapWith('textit'), preventDefault: true },
    ])),
    Prec.high(autoEnd),
    referenceLinter(index, opts.problems),
  ]
}
