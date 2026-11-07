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
