// Equation preview: with the cursor in maths, a tooltip shows it typeset by KaTeX, using the
// project's own \newcommand / \DeclareMathOperator macros. KaTeX (and its fonts) loads the
// first time it is needed. Collaborators write this text, so KaTeX runs with trust off.

import { StateField, type EditorState, type Extension, type Text } from '@codemirror/state'
import { EditorView, showTooltip, type Tooltip } from '@codemirror/view'
import type * as Y from 'yjs'
import { COMMENT, envClose, envOpen, INLINE_MATH, MATH_OPEN, mathClose, spans } from './texscan'
import { t } from './i18n'

export interface MathSpan { from: number; to: number; tex: string; display: boolean }

const ENVS = 'equation|align|gather|multline|eqnarray|math|displaymath|flalign|alignat'
const MAX_DOC = 2 * 1024 * 1024
const MAX_TEX = 8000
const PARAGRAPH = /\n[ \t]*\n/ // a blank line ends a paragraph
const DOLLAR = /(?<!\\)\$/g

const blank = (t: string, rs: [number, number][]) => {
  let out = '', at = 0
  for (const [a, b] of rs) { out += t.slice(at, a) + ' '.repeat(b - a); at = b }
  return out + t.slice(at)
}

/** Every piece of maths in `text`, outside comments. */
export function mathSpans(text: string): MathSpan[] {
  if (text.length > MAX_DOC) return []
  let t = text.replace(COMMENT, (c) => ' '.repeat(c.length)) // offsets kept
  const out: MathSpan[] = []
  const envs = spans(t, envOpen(ENVS), envClose)
  for (const [a, b] of envs) out.push({ from: a, to: b, tex: envTex(text.slice(a, b)), display: !text.startsWith('\\begin{math}', a) })
  t = blank(t, envs)
  const delimited = spans(t, MATH_OPEN, mathClose)
  for (const [a, b] of delimited) {
    const open = text.slice(a, a + 2)
    out.push({ from: a, to: b, tex: text.slice(a + 2, b - 2), display: open !== '\\(' })
  }
  t = blank(t, delimited)
  // $…$ is paired within a paragraph, as in TeX (it can't run past a blank line). A paragraph with an
  // odd number of $ is being typed (a $ whose closing one isn't there yet): pairing it would match
  // that $ with the next formula's opening one and turn the rest inside out, so none of its $…$
  // counts until it's even again.
  for (let at = 0; at <= t.length;) {
    const brk = PARAGRAPH.exec(t.slice(at)), end = brk ? at + brk.index : t.length
    const para = t.slice(at, end)
    if ((para.match(DOLLAR) ?? []).length % 2 === 0) {
      for (const m of para.matchAll(INLINE_MATH)) {
        const a = at + m.index!, b = a + m[0].length
        if (b - a > 2) out.push({ from: a, to: b, tex: text.slice(a + 1, b - 1), display: false })
      }
    }
    if (!brk) break
    at = end + brk[0].length
  }
  return out.sort((x, y) => x.from - y.from)
}

/** An environment as KaTeX understands it: labels dropped, LaTeX-only ones mapped. */
function envTex(src: string): string {
  const m = /^\\begin\{(\w+)(\*?)\}/.exec(src)!
  const name = m[1], inner = src.slice(m[0].length, src.length - `\\end{${name}${m[2]}}`.length)
  const body = inner.replace(/\\label\s*\{[^{}]{0,200}\}/g, '')
  switch (name) {
    case 'math': case 'displaymath': return body
    case 'eqnarray': return `\\begin{array}{rcl}${body}\\end{array}`
    case 'flalign': return `\\begin{align*}${body}\\end{align*}`
    case 'multline': return `\\begin{gather*}${body}\\end{gather*}`
    default: return `\\begin{${name}*}${body}\\end{${name}*}` // numbering off: there is nothing to number
  }
}

/** The maths containing `pos` (delimiters included). */
export function mathAt(text: string, pos: number): MathSpan | null {
  for (const s of mathSpans(text)) {
    if (s.from > pos) break
    if (pos <= s.to && pos >= s.from) return s
  }
  return null
}

// ---- the project's macros -------------------------------------------------------------

/** The `{…}` group starting at `i` (after spaces), or a single token. */
function group(t: string, i: number): [string, number] | null {
  while (t[i] === ' ' || t[i] === '\n' || t[i] === '\t') i++
  if (t[i] === '\\') { const m = /^\\(?:[A-Za-z@]+|.)/.exec(t.slice(i, i + 100)); return m ? [m[0], i + m[0].length] : null }
  if (t[i] !== '{') return null
  let depth = 0
  for (let j = i; j < t.length && j < i + 4000; j++) {
    if (t[j] === '\\') { j++; continue }
    if (t[j] === '{') depth++
    else if (t[j] === '}' && --depth === 0) return [t.slice(i + 1, j), j + 1]
  }
  return null
}

/** Macros defined in the project, as KaTeX's `macros` option. Commands with an optional
 *  argument are left out (KaTeX macros can't take one). */
export function projectMacros(texts: Iterable<string>): Record<string, string> {
  const macros: Record<string, string> = {}
  let n = 0
  for (const raw of texts) {
    const t = raw.replace(COMMENT, '')
    for (const m of t.matchAll(/\\(?:(?:re|provide)?newcommand|DeclareMathOperator|DeclareRobustCommand|def)(\*?)/g)) {
      if (n >= 2000) return macros
      const kind = m[0].slice(1).replace('*', ''), star = m[1] === '*'
      let i = m.index! + m[0].length
      let name: string | undefined, body: string | undefined
      if (kind === 'def') {
        const nm = /^\s*(\\[A-Za-z@]+)((?:#\d)*)\s*/.exec(t.slice(i, i + 120))
        if (!nm) continue
        const g = group(t, i + nm[0].length)
        if (!g || !t.slice(i + nm[0].length).trimStart().startsWith('{')) continue
        name = nm[1]; body = g[0]
      } else {
        const g = group(t, i)
        if (!g || !/^\\[A-Za-z@]+$/.test(g[0].trim())) continue
        name = g[0].trim(); i = g[1]
        if (kind === 'DeclareMathOperator') {
          const b = group(t, i)
          if (!b) continue
          body = `\\operatorname${star ? '*' : ''}{${b[0]}}`
        } else {
          const rest = t.slice(i, i + 40)
          const args = /^\s*\[(\d)\]/.exec(rest)
          if (args) { i += args[0].length; if (/^\s*\[/.test(t.slice(i, i + 20))) continue } // optional argument
          const b = group(t, i)
          if (!b || !t.slice(i).trimStart().startsWith('{')) continue
          body = b[0]
        }
      }
      if (body.length > 2000 || name === '\\newcommand') continue
      macros[name] = body
      n++
    }
  }
  return macros
}

// ---- rendering -------------------------------------------------------------------------

type KaTeX = typeof import('katex').default
let katex: Promise<KaTeX> | null = null
export const loadKatex = () => (katex ??= Promise.all([import('katex'), import('katex/dist/katex.min.css')]).then(([k]) => k.default))

export function render(k: KaTeX, el: HTMLElement, s: Pick<MathSpan, 'tex' | 'display'>, macros: Record<string, string> = {}) {
  if (s.tex.length > MAX_TEX) { el.textContent = t('Too long to preview.'); return }
  try {
    k.render(s.tex, el, {
      displayMode: s.display, throwOnError: false, trust: false, strict: 'ignore', output: 'html',
      macros: { ...macros }, maxSize: 20, maxExpand: 500, errorColor: 'var(--danger, #e5534b)',
    })
  } catch (e) {
    el.textContent = String((e as Error).message ?? e).slice(0, 300)
  }
}

/** The tooltip, for text files of the project in `files` (macros are read from them all). */
export function mathPreview(files: Y.Map<Y.Text>): Extension {
  let macroCache: Record<string, string> | null = null
  files.observeDeep(() => (macroCache = null))
  const macros = () => (macroCache ??= projectMacros([...files.entries()].filter(([p]) => /\.(tex|sty|cls|ltx)$/i.test(p)).map(([, t]) => t.toString())))

  const cache = new WeakMap<Text, MathSpan[]>()
  const spansOf = (doc: Text) => { let s = cache.get(doc); if (!s) cache.set(doc, (s = mathSpans(doc.toString()))); return s }
  const find = (state: EditorState): MathSpan | null => {
    const sel = state.selection.main
    if (state.selection.ranges.length > 1) return null
    for (const s of spansOf(state.doc)) {
      if (s.from > sel.from) break
      if (sel.from >= s.from && sel.to <= s.to) return s
    }
    return null
  }
  const tooltip = (s: MathSpan, head: number): Tooltip => ({
    pos: s.display ? s.to : head, above: false, strictSide: false, arrow: false,
    create: () => {
      const dom = document.createElement('div')
      dom.className = 'cm-math-preview'
      const body = dom.appendChild(document.createElement('div'))
      loadKatex().then((k) => render(k, body, s, macros()), () => { body.textContent = t('Preview unavailable offline until loaded once.') })
      return { dom }
    },
  })

  const field = StateField.define<{ span: MathSpan | null; tip: Tooltip | null }>({
    create: (state) => { const span = find(state); return { span, tip: span ? tooltip(span, state.selection.main.head) : null } },
    update(v, tr) {
      if (!tr.docChanged && !tr.selection) return v
      const span = find(tr.state)
      if (!span) return { span: null, tip: null }
      // same maths, cursor moved within it: keep the tooltip (no flicker)
      if (v.span && v.tip && span.tex === v.span.tex && span.display === v.span.display && (span.display || !tr.docChanged)) {
        return { span, tip: tr.docChanged ? { ...v.tip, pos: tr.changes.mapPos(v.tip.pos) } : v.tip }
      }
      return { span, tip: tooltip(span, tr.state.selection.main.head) }
    },
    provide: (f) => showTooltip.from(f, (v) => v.tip),
  })
  return [field, EditorView.baseTheme({
    '.cm-tooltip.cm-math-preview, .cm-math-preview': { padding: '6px 10px', maxWidth: 'min(90vw, 720px)', overflowX: 'auto', fontSize: '1.05em' },
    '.cm-math-preview .katex-display': { margin: '0' },
  })]
}
