// Small LaTeX scanning helpers shared by editor features (outline, spelling, references,
// package detection), so they agree on what is a comment.

/** A comment: '%' to the end of the line, unless the '%' is escaped by an odd number of
 *  backslashes ("\%" is a percent sign; "\\%" is a line break followed by a comment).
 *  The match starts at the '%'. */
export const COMMENT = /(?<=(?:^|[^\\])(?:\\\\)*)%[^\n]*/gm

/** The text without comments (offsets change). */
export const stripComments = (t: string) => t.replace(COMMENT, '')

/** Spans from each match of `open` to the next `close(match)`, in linear time: each closer is
 *  looked up once with indexOf, and when one is missing, later openers of that kind are
 *  skipped instead of rescanning to the end (crafted files full of unclosed "\\[" stay fast). */
export function spans(text: string, open: RegExp, close: (m: RegExpExecArray) => string): [number, number][] {
  const out: [number, number][] = []
  const missing = new Set<string>()
  const re = new RegExp(open.source, open.flags.includes('g') ? open.flags : open.flags + 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const end = close(m)
    if (missing.has(end)) continue
    const j = text.indexOf(end, m.index + m[0].length)
    if (j < 0) { missing.add(end); continue }
    out.push([m.index, j + end.length])
    re.lastIndex = j + end.length
  }
  return out
}

/** Display and inline maths delimiters (not "\\\\[2pt]", a line break with spacing). */
export const MATH_OPEN = /\$\$|(?<!\\)\\\[|(?<!\\)\\\(/g
export const mathClose = (m: RegExpExecArray) => (m[0] === '$$' ? '$$' : m[0] === '\\[' ? '\\]' : '\\)')
/** Inline $…$ (bounded, so unclosed dollars can't make it quadratic). */
export const INLINE_MATH = /(?<!\\)\$(?:\\.|[^$\\]){0,5000}\$/g
/** \begin{env} for the given environment names; close with envClose. */
export const envOpen = (names: string) => new RegExp(String.raw`\\begin\{(${names})(\*?)\}`, 'g')
export const envClose = (m: RegExpExecArray) => `\\end{${m[1]}${m[2]}}`

// ---- lines that ask to run programs (keep in sync with texloom/scan.py) ----------------
/** TeX can't run programs unless shell escape is on (browser compiles never do; texloom's
 *  helper doesn't by default). These lines matter when someone compiles a synced folder
 *  with their own tools: with shell escape ("shell"), or with tools that follow arara
 *  directives or editor magic comments ("tools"). Plainly written forms only: macros can
 *  hide a command from any scanner. */
export interface ProgramLine { path: string; line: number; kind: 'shell' | 'tools'; what: string; text: string }

const SHELL: [RegExp, string][] = [
  [/\\write\s*18\b/, 'runs a shell command (\\write18)'],
  [/\\(?:Delayed)?ShellEscape\b/, 'runs a shell command (\\ShellEscape)'],
  [/\\input\s*\{?\s*\|/, 'reads the output of a command (\\input|)'],
  [/\b(?:os\.execute|io\.popen|os\.spawn|os\.exec)\b/, 'runs a program from Lua'],
  [/\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]{0,300}\])?\s*\{[^}]{0,300}\b(?:minted|pythontex|sagetex|gnuplottex|svg|auto-pst-pdf|shellesc|bashful|pyluatex)\b/, 'loads a package that runs programs'],
  [/\\tikzexternalize\b|\\usetikzlibrary\s*\{[^}]{0,300}\bexternal\b/, 'TikZ externalisation runs LaTeX as a command'],
]
const TOOLS: [RegExp, string][] = [
  [/^\s*%\s*arara\s*:/, 'tells arara which programs to run'],
  [/^\s*%\s*!\s*(?:TeX|TEX|tex|BIB|bib)\s+(?:TS-|TXS-)?program\b/, 'sets the program your LaTeX editor runs'],
]
const SCANNED = /\.(tex|sty|cls|ltx|dtx|def|cfg|lua|tikz)$/i

/** The line up to its comment: a '%' after an even number of backslashes. */
function codePart(line: string): string {
  let run = 0
  for (let i = 0; i < line.length; i++) {
    const c = line[i]
    if (c === '\\') { run++; continue }
    if (c === '%' && run % 2 === 0) return line.slice(0, i)
    run = 0
  }
  return line
}

export function programRequests(path: string, text: string): ProgramLine[] {
  if (!SCANNED.test(path) || text.length > 4 * 1024 * 1024) return []
  const lua = /\.lua$/i.test(path), out: ProgramLine[] = []
  text.split('\n').forEach((line, i) => {
    const shown = line.trim().slice(0, 160), code = lua ? line : codePart(line)
    const s = SHELL.find(([rx]) => rx.test(code))
    if (s) { out.push({ path, line: i + 1, kind: 'shell', what: s[1], text: shown }); return }
    const t = lua ? undefined : TOOLS.find(([rx]) => rx.test(line))
    if (t) out.push({ path, line: i + 1, kind: 'tools', what: t[1], text: shown })
  })
  return out
}

/** Program lines in `next` that weren't in `prev` (what a change brought in). */
export function addedProgramLines(path: string, prev: string | null, next: string): ProgramLine[] {
  const before = new Set(programRequests(path, prev ?? '').map((f) => f.text))
  return programRequests(path, next).filter((f) => !before.has(f.text))
}
