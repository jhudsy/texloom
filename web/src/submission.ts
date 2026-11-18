// What goes in a .zip for arXiv or a journal: the files the document uses (found by following
// its own commands), the compiled bibliography (.bbl) instead of the .bib, optionally without
// comments and optionally merged into one .tex. Pure functions, checked by
// scripts/check-submission.mjs; the dialog is in submitdialog.ts.
import { latexOutput, stripComments } from './texscan'

export type Files = Map<string, string | Uint8Array>
export interface Options { stripComments: boolean; oneFile: boolean }
export interface Submission {
  /** Paths in the .zip, relative to the main file's folder when everything used is inside it. */
  files: Map<string, string | Uint8Array>
  /** The main file's path in the .zip. */
  main: string
  /** Project files that don't go in (unused, build output, the .bib when there's a .bbl). */
  leftOut: string[]
  /** .tex files merged into the main file (one-file mode). */
  merged: string[]
}

/** A project path joined and tidied ('a/../b' is 'b'); null if it leaves the project. */
function resolve(dir: string, name: string): string | null {
  if (!name || name.startsWith('/')) return null
  const out: string[] = []
  for (const seg of `${dir}/${name}`.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') { if (!out.length) return null; out.pop() } else out.push(seg)
  }
  return out.length ? out.join('/') : null
}
const dirOf = (p: string) => p.slice(0, Math.max(0, p.lastIndexOf('/')))
const stem = (p: string) => { const i = p.lastIndexOf('.'); return i > p.lastIndexOf('/') ? p.slice(0, i) : p }

/** Files TeX reads as code, so the files they name are followed too. */
const SCANNED = /\.(tex|ltx|sty|cls|def|cfg|clo|fd|bbx|cbx|lbx|tikz|pgf)$/i
/** What LaTeX writes while compiling: never part of a submission. */
const BUILD = latexOutput(String.raw`|synctex\(busy\)|ilg|brf|bbl`)
/** arXiv's instructions to its own build (which file is the main one, and so on). */
const ARXIV = /^00README(\.XXX|\.json|\.yaml)?$/i
/** Commands that name a bibliography: with one, the submission needs a .bbl. */
const BIB = /\\(?:bibliography|addbibresource|addglobalbib|addsectionbib)\s*(?:\[[^\]]{0,300}\]\s*)?\{/

/** The files `main` uses: itself, what its code names in a command's argument (with or without
 *  an extension, from the main file's folder or a \graphicspath folder), and so on through the
 *  files those name. Liberal on purpose: a file named but unused costs a few bytes; a file
 *  missing fails the submission's build. */
export function usedFiles(files: Files, main: string): string[] {
  const byStem = new Map<string, string[]>()
  for (const p of files.keys()) {
    if (BUILD.test(p)) continue
    const s = stem(p)
    byStem.set(s, [...(byStem.get(s) ?? []), p])
  }
  const mainDir = dirOf(main)
  const used = new Set<string>([main])
  const todo = [main]
  const add = (p: string) => { if (!used.has(p)) { used.add(p); if (SCANNED.test(p)) todo.push(p) } }
  for (const p of files.keys()) if (dirOf(p) === mainDir && ARXIV.test(p.slice(p.lastIndexOf('/') + 1))) add(p)
  while (todo.length) {
    const path = todo.pop()!
    const v = files.get(path)
    if (typeof v !== 'string') continue
    const text = stripComments(v)
    const bases = [mainDir, dirOf(path)]
    for (const m of text.matchAll(/\\graphicspath\s*\{((?:\s*\{[^{}]{0,300}\})+)\s*\}/g))
      for (const d of m[1].matchAll(/\{([^{}]*)\}/g)) { const p = resolve(mainDir, d[1].trim()); if (p) bases.push(p) }
    const names: string[] = []
    for (const m of text.matchAll(/\\[A-Za-z@]+\*?\s*(?:\[[^\]]{0,300}\]\s*)*\{([^{}]{1,300})\}(?:\s*\{([^{}]{1,300})\})?/g)) {
      names.push(...m[1].split(','))
      if (m[2]) names.push(...m[2].split(','), `${m[1].trim()}/${m[2].trim()}`) // \import{dir}{file}
    }
    for (const m of text.matchAll(/\\input\s+([^\s{}\\%]+)/g)) names.push(m[1]) // plain TeX's \input file
    for (const raw of names) {
      const name = raw.trim().replace(/^"(.*)"$/, '$1')
      if (!name || name.length > 300) continue
      for (const base of bases) {
        const p = resolve(base, name)
        if (!p) continue
        if (files.has(p) && !BUILD.test(p)) add(p)
        for (const q of byStem.get(p) ?? []) add(q)
      }
    }
  }
  return [...used]
}

/** Whether the document's bibliography comes from a .bib, so the submission needs the .bbl. */
export function needsBbl(files: Files, main: string): boolean {
  return usedFiles(files, main).some((p) => { const v = files.get(p); return typeof v === 'string' && /\.tex$/i.test(p) && BIB.test(stripComments(v)) })
}

/** Where a line's comment starts (its length if none): a '%' that isn't escaped, nor inside
 *  \verb|…|, \lstinline|…|, or a URL (\url{…}, \href{…}, \path{…}), where '%' is a character. */
export function commentStart(line: string): number {
  const SPECIAL = /\\(verb\*?|lstinline|url|path|href|nolinkurl)(?![A-Za-z@])/y
  for (let i = 0; i < line.length; i++) {
    const c = line[i]
    if (c === '%') return i
    if (c !== '\\') continue
    SPECIAL.lastIndex = i
    const m = SPECIAL.exec(line)
    if (!m) { i++; continue } // "\%", "\\" and any other control sequence's first character
    let j = i + m[0].length
    if (m[1] === 'lstinline') { while (line[j] === ' ') j++; if (line[j] === '[') { const k = line.indexOf(']', j); if (k < 0) return line.length; j = k + 1 } }
    else if (m[1] !== 'verb' && m[1] !== 'verb*') while (line[j] === ' ') j++
    const open = line[j]
    if (open === undefined) return line.length
    const close = open === '{' ? '}' : open
    const k = line.indexOf(close, j + 1)
    if (k < 0) return line.length // a verbatim argument that goes on: TeX would complain; leave the line alone
    i = k
  }
  return line.length
}

const VERBATIM = /\\begin\{(verbatim\*?|Verbatim\*?|BVerbatim|LVerbatim|lstlisting|minted|alltt|comment|filecontents\*?|tcblisting|codeblock)\}/

/** The text without comments, as arXiv publishes the source. A line that is only a comment goes;
 *  a comment after code becomes a bare '%', which keeps its meaning (no space at the line end).
 *  Verbatim environments are left as they are; comment environments go. */
export function stripTexComments(text: string): string {
  const out: string[] = []
  let inside: { end: string; drop: boolean } | null = null
  for (const line of text.split('\n')) {
    if (inside) {
      const at = line.indexOf(inside.end)
      if (at < 0) { if (!inside.drop) out.push(line); continue }
      const drop = inside.drop
      inside = null
      if (drop) { const after = line.slice(at + '\\end{comment}'.length); if (after.trim()) out.push(stripLine(after)); continue }
      out.push(line)
      continue
    }
    const cut = commentStart(line)
    const code = line.slice(0, cut)
    const v = VERBATIM.exec(code)
    if (v && !code.includes(`\\end{${v[1]}}`, v.index)) {
      inside = { end: `\\end{${v[1]}}`, drop: v[1] === 'comment' && !code.slice(0, v.index).trim() }
      if (!inside.drop) out.push(line) // the line opening it is kept whole: what follows may be code
      continue
    }
    if (cut === line.length) out.push(line)
    else if (code.trim()) out.push(code + '%')
    // else: the line was only a comment
  }
  return out.join('\n')
}
const stripLine = (line: string) => { const cut = commentStart(line); return cut === line.length ? line : line.slice(0, cut) + '%' }

/** The body of a \subfile's document (between \begin{document} and \end{document}). */
function documentBody(text: string): string {
  const a = text.search(/\\begin\{document\}/), b = text.search(/\\end\{document\}/)
  return a >= 0 && b > a ? text.slice(a + '\\begin{document}'.length, b) : text
}

/** `path` with each \input, \include and \subfile of a .tex file in `used` replaced by that
 *  file's text, recursively; commented-out ones stay as they are. With `bbl` (BibTeX), the
 *  \bibliography command is replaced by it, so the one file has the references too. */
function merged(files: Files, used: Set<string>, mainDir: string, path: string, into: Set<string>, stack: string[], bbl: string | null): string {
  const text = files.get(path)
  if (typeof text !== 'string') return ''
  const lines = text.split('\n')
  return lines.map((line) => {
    const cut = commentStart(line)
    const code = line.slice(0, cut).replace(/\\(input|include|subfile)\s*\{([^{}]{1,300})\}/g, (m, cmd: string, name: string) => {
      const p = resolve(mainDir, name.trim())
      const hit = p && [p, p + '.tex'].find((q) => /\.tex$/i.test(q) && used.has(q))
      if (!hit || stack.includes(hit)) return m
      into.add(hit)
      let body = merged(files, used, mainDir, hit, into, [...stack, hit], null)
      if (cmd === 'subfile') body = documentBody(body)
      if (!body.endsWith('\n')) body += '\n'
      return cmd === 'include' ? `\\clearpage\n${body}\\clearpage\n` : body
    }).replace(/\\bibliography\s*\{[^{}]{0,2000}\}/g, (m) => (bbl === null ? m : bbl.endsWith('\n') ? bbl : bbl + '\n'))
    return code + line.slice(cut)
  }).join('\n')
}

/** The submission for `main`. `bbl` is the compiled bibliography (null: none, or it couldn't be
 *  made; then the .bib goes in instead). */
export function submission(files: Files, main: string, bbl: string | null, opts: Options): Submission {
  const usedList = usedFiles(files, main)
  const used = new Set(usedList)
  const mainDir = dirOf(main)
  const bblPath = stem(main) + '.bbl'
  const biblatex = usedList.some((p) => { const v = files.get(p); return typeof v === 'string' && /\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]{0,300}\]\s*)?\{[^}]*\bbiblatex\b/.test(stripComments(v)) })
  const into = new Set<string>()
  const out = new Map<string, string | Uint8Array>()
  for (const p of usedList) {
    if (bbl !== null && /\.bib$/i.test(p)) continue
    out.set(p, files.get(p)!)
  }
  let bblInMain = false
  if (opts.oneFile) {
    const inlineBbl = bbl !== null && !biblatex ? bbl : null
    const text = merged(files, used, mainDir, main, into, [main], inlineBbl)
    bblInMain = inlineBbl !== null && text !== merged(files, used, mainDir, main, new Set(), [main], null)
    out.set(main, text)
    for (const p of into) out.delete(p)
  }
  if (bbl !== null && !bblInMain) out.set(bblPath, bbl)
  if (opts.stripComments) for (const [p, v] of out) if (typeof v === 'string' && /\.tex$/i.test(p)) out.set(p, stripTexComments(v))
  // Everything inside the main file's folder: that folder is the .zip's top level, as arXiv wants.
  const prefix = mainDir ? mainDir + '/' : ''
  const rebase = prefix && [...out.keys()].every((p) => p.startsWith(prefix))
  const files2 = new Map([...out].map(([p, v]) => [rebase ? p.slice(prefix.length) : p, v]))
  const leftOut = [...files.keys()].filter((p) => !out.has(p) && p !== bblPath).sort()
  return { files: files2, main: rebase ? main.slice(prefix.length) : main, leftOut, merged: [...into].sort() }
}
