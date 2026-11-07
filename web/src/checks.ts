// Common LaTeX mistakes, found while typing (shown by the editor's linter in latex.ts).
// Each check aims to fire only when something is actually wrong or will typeset badly, and
// most offer a one-click fix: the text in [from, to) is replaced by `fix`.
//
// Kinds: structure (environments, braces, \label before \caption: errors and warnings),
// and typesetting hints (quotes, dots, ties, spacing after abbreviations: info).

import { COMMENT, envClose, envOpen, spans } from './texscan'
import { mathSpans } from './mathpreview'

export interface Check { from: number; to: number; severity: 'error' | 'warning' | 'info'; message: string; fix?: string; fixLabel?: string }

const MAX = 2 * 1024 * 1024
const VERBATIM = 'verbatim\\*?|Verbatim\\*?|BVerbatim|lstlisting|minted|comment|filecontents\\*?|alltt'

const blank = (t: string, rs: Iterable<[number, number]>) => {
  const a = t.split('')
  for (const [x, y] of rs) for (let i = x; i < y; i++) if (a[i] !== '\n') a[i] = ' '
  return a.join('')
}

/** The text with comments, verbatim environments and \verb|…| blanked (offsets kept). */
function code(text: string): string {
  let t = text.replace(COMMENT, (c) => ' '.repeat(c.length))
  t = blank(t, spans(t, envOpen(VERBATIM), envClose).map(([a, b]) => {
    // keep \begin{…} and \end{…} so environments still balance
    const open = /^\\begin\{[^}]*\}/.exec(t.slice(a, b))![0].length, close = t.slice(a, b).lastIndexOf('\\end{')
    return [a + open, a + close] as [number, number]
  }))
  return t.replace(/\\verb\*?([^A-Za-z\s*])[^\n]{0,500}?\1/g, (m) => ' '.repeat(m.length))
}

// ---- structure -------------------------------------------------------------------------

function environments(t: string, out: Check[]) {
  const stack: { name: string; from: number; to: number }[] = []
  // the preamble's \begin/\end sit in definitions (\newcommand{\beq}{\begin{equation}})
  const doc = t.includes('\\documentclass') ? t.indexOf('\\begin{document}') : -1
  for (const m of t.matchAll(/\\(begin|end)\s*\{([^{}\n]{1,60})\}/g)) {
    const name = m[2].trim(), from = m.index!, to = from + m[0].length
    if (from < doc) continue
    if (m[1] === 'begin') { stack.push({ name, from, to }); continue }
    if (stack.length && stack[stack.length - 1].name === name) { stack.pop(); continue }
    const at = stack.map((s) => s.name).lastIndexOf(name)
    if (at < 0) {
      out.push({ from, to, severity: 'error', message: `\\end{${name}} has no matching \\begin{${name}}.` })
      continue
    }
    for (const open of stack.splice(at + 1).reverse()) {
      out.push({ from: open.from, to: open.to, severity: 'error', message: `\\begin{${open.name}} isn’t closed before \\end{${name}}.` })
    }
    stack.pop()
  }
  for (const open of stack) out.push({ from: open.from, to: open.to, severity: 'warning', message: `\\begin{${open.name}} isn’t closed in this file.` })
}

function braces(t: string, out: Check[]) {
  const open: number[] = []
  for (let i = 0; i < t.length; i++) {
    const c = t[i]
    if (c === '\\') { i++; continue } // \{ \} \\ and any command
    if (c === '{') open.push(i)
    else if (c === '}') {
      if (open.length) open.pop()
      else out.push({ from: i, to: i + 1, severity: 'error', message: 'This } closes nothing: there are more } than { before it.' })
    }
  }
  for (const i of open.slice(0, 20)) out.push({ from: i, to: i + 1, severity: 'warning', message: 'This { isn’t closed in this file.' })
}

/** \label before \caption in a float: \ref then gives the section number, not the figure's. */
function labelBeforeCaption(t: string, out: Check[]) {
  for (const [a, b] of spans(t, envOpen('figure\\*?|table\\*?|wrapfigure|wraptable|subfigure|subtable'), envClose)) {
    const body = t.slice(a, b)
    const caps = [...body.matchAll(/\\caption\s*[[{]/g)].map((m) => m.index!)
    for (const m of body.matchAll(/\\label\s*\{[^{}]{0,200}\}/g)) {
      const at = m.index!
      if (caps.some((c) => c < at)) continue // a caption (this float's or a subfigure's) comes first
      if (!caps.some((c) => c > at)) continue // no caption at all: nothing to number
      out.push({ from: a + at, to: a + at + m[0].length, severity: 'warning', message: 'Put \\label after \\caption: here \\ref gives the number of the section, not of this float.' })
    }
  }
}

// ---- typesetting hints -------------------------------------------------------------------

/** Ranges that aren't prose: maths, the preamble, and arguments that hold keys, files or code. */
function proseMask(text: string, t: string): string {
  const skip: [number, number][] = mathSpans(text).map((s) => [s.from, s.to])
  const doc = t.indexOf('\\begin{document}')
  if (doc > 0 && t.includes('\\documentclass')) skip.push([0, doc])
  for (const m of t.matchAll(/\\(?:label|ref|eqref|[cC]ref|autoref|pageref|nameref|cite\w*|nocite|includegraphics|input|include|subfile|usepackage|url|href|path|begin|end|bibliography\w*|addbibresource|graphicspath|lstinline|newcommand|renewcommand|def|hypersetup|setlength|(?:re)?newenvironment)\*?\s*(?:\[[^\]]{0,300}\]\s*){0,3}(?:\{[^{}]{0,2000}\})?/g)) {
    skip.push([m.index!, m.index! + m[0].length])
  }
  for (const [a, b] of spans(t, envOpen('tikzpicture|tabular\\*?|array|lstlisting|minted|verbatim\\*?|algorithmic|algorithm2e'), envClose)) skip.push([a, b])
  return blank(t, skip)
}

const NOT_DOUBLED = new Set(['that', 'had', 'is', 'can', 'do', 'la', 'ha', 'bye', 'so', 'very', 'no', 'yes', 'well'])

function hints(text: string, t: string, out: Check[]) {
  const p = proseMask(text, t)
  const german = /\\usepackage\s*(?:\[[^\]]*\b(?:n?german|ngerman|austrian|swissgerman|dutch|icelandic)\b[^\]]*\])\s*\{babel\}/.test(t)
  if (!german) {
    for (const m of p.matchAll(/(^|[\s(\[])"([^"\n]{1,200}?)"(?=[\s.,;:!?)\]]|$)/gm)) {
      const from = m.index! + m[1].length, to = m.index! + m[0].length
      out.push({ from, to, severity: 'info', message: "Straight quotes come out as ” on both sides: write ``text'' (two backquotes, then two apostrophes).", fix: `\`\`${m[2]}''`, fixLabel: 'Use `` and \'\'' })
    }
  }
  for (const m of p.matchAll(/(?<![.\\])\.\.\.(?!\.)/g)) {
    out.push({ from: m.index!, to: m.index! + 3, severity: 'info', message: 'Write \\dots for an ellipsis: three full stops are spaced too tightly.', fix: '\\dots', fixLabel: 'Use \\dots' })
  }
  for (const m of p.matchAll(/(?<![A-Za-z\\])([A-Za-z]{2,})(\s+)\1(?![A-Za-z])/gi)) {
    if (NOT_DOUBLED.has(m[1].toLowerCase())) continue
    if (m[2].includes('\n\n')) continue // across paragraphs
    out.push({ from: m.index!, to: m.index! + m[0].length, severity: 'info', message: `“${m[1]}” twice in a row.`, fix: m[1], fixLabel: 'Remove the repeat' })
  }
  // an abbreviation's full stop followed by a space reads as the end of a sentence (wider space)
  for (const m of p.matchAll(/(?<![A-Za-z\\])(e\.g\.|i\.e\.|cf\.|vs\.|resp\.|approx\.|et al\.)( )(?=[a-z0-9(\\$])/g)) {
    const from = m.index!, to = from + m[1].length
    out.push({ from, to, severity: 'info', message: `LaTeX takes the full stop in “${m[1]}” as the end of a sentence and adds a wide space: write ${m[1]}\\ (or ${m[1]}~).`, fix: `${m[1]}\\`, fixLabel: `Use ${m[1]}\\` })
  }
}

function commandHints(text: string, t: string, out: Check[]) {
  // a line break between "Figure" and its number: tie them
  for (const m of t.matchAll(/([A-Za-z)])( )(\\(?:ref|eqref|autoref|pageref|cite)\{)/g)) {
    const at = m.index! + m[1].length
    out.push({ from: at, to: at + 1, severity: 'info', message: `A non-breaking space (~) keeps ${m[3].slice(0, -1)} on the same line as the word before it.`, fix: '~', fixLabel: 'Use ~' })
  }
  for (const [a, b] of spans(t, /\$\$/g, () => '$$')) {
    out.push({ from: a, to: b, severity: 'info', message: '$$…$$ is plain TeX: in LaTeX, \\[…\\] spaces displays correctly (and works with fleqn).', fix: `\\[${text.slice(a + 2, b - 2)}\\]`, fixLabel: 'Use \\[ \\]' })
  }
  for (const m of t.matchAll(/\\(bf|it|rm|sc|tt|sf|sl)(?![A-Za-z@])/g)) {
    const to = m.index! + m[0].length
    const better = { bf: 'textbf', it: 'textit', rm: 'textrm', sc: 'textsc', tt: 'texttt', sf: 'textsf', sl: 'textsl' }[m[1]]
    out.push({ from: m.index!, to, severity: 'info', message: `\\${m[1]} is from LaTeX 2.09 and doesn’t combine (bold italic): use \\${better}{…} or \\${m[1]}${m[1] === 'sc' || m[1] === 'sl' ? 'shape' : m[1] === 'bf' ? 'series' : m[1] === 'it' ? 'shape' : 'family'}.` })
  }
  for (const m of t.matchAll(/(\\cite[pt]?)(\{[^{}]{1,500}\})\s*~?\s*\1\{([^{}]{1,500})\}/g)) {
    out.push({ from: m.index!, to: m.index! + m[0].length, severity: 'info', message: `Cite them together (${m[1]}{a,b}) for one bracket and sorted numbers.`, fix: `${m[1]}${m[2].slice(0, -1)},${m[3]}}`, fixLabel: 'Combine' })
  }
  // function names typed as letters in maths: "sin x" is the product s·i·n·x in italics
  for (const s of mathSpans(text)) {
    if (s.to - s.from > 20000) continue
    const inner = s.from
    const tex = text.slice(s.from, s.to).replace(/\\(?:begin|end)\s*\{[^{}]*\}/g, (x) => ' '.repeat(x.length)).replace(/\\(?:text\w*|mathrm|operatorname\*?|mbox|label|ref|eqref|mathit|mathsf|mathtt|textrm|hbox)\s*\{[^{}]{0,300}\}/g, (x) => ' '.repeat(x.length))
    for (const m of tex.matchAll(/(?<![\\A-Za-z_^{])(sin|cos|tan|cot|sec|csc|sinh|cosh|tanh|arcsin|arccos|arctan|log|ln|exp|det|lim|max|min|sup|inf|gcd|deg)(?![A-Za-z])/g)) {
      const from = inner + m.index!
      out.push({ from, to: from + m[1].length, severity: 'info', message: `Write \\${m[1]}: as plain letters, “${m[1]}” is set in italics like a product of variables.`, fix: `\\${m[1]}`, fixLabel: `Use \\${m[1]}` })
    }
  }
}

/** Mistakes in a .tex file. */
export function checkLatex(text: string): Check[] {
  if (text.length > MAX) return []
  const out: Check[] = []
  const t = code(text)
  environments(t, out)
  braces(t, out)
  labelBeforeCaption(t, out)
  hints(text, t, out)
  commandHints(text, t, out)
  return out.sort((a, b) => a.from - b.from)
}
