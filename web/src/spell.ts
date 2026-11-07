// Spell checking with the browser's own checker, told to skip what isn't prose:
// commands, maths, comments, labels/citations/file names, and the preamble.
import { RangeSetBuilder, type Extension } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import { COMMENT, envClose, envOpen, INLINE_MATH, MATH_OPEN, mathClose, spans } from './texscan'

const NO_CHECK = Decoration.mark({ attributes: { spellcheck: 'false' } })
const MATH_ENVS = 'equation|align|gather|multline|eqnarray|math|displaymath|flalign|alignat|verbatim|Verbatim|lstlisting|minted|tikzpicture|filecontents|tabular\\*?|array'
// commands whose (first) braced argument isn't prose
const ARG_CMDS = 'label|ref|eqref|[cC]ref|autoref|pageref|nameref|cite\\w*|nocite|includegraphics|input|include|subfile|usepackage|RequirePackage|documentclass|bibliography(?:style)?|addbibresource|begin|end|url|href|graphicspath|hypersetup|newcommand|renewcommand|providecommand|newenvironment|renewenvironment|setlength|setcounter|addtocounter|pagestyle|thispagestyle|newtheorem|DeclareMathOperator|color|textcolor|definecolor|vspace|hspace|lstinline|verb'

/** Ranges of `text` that aren't prose, sorted and non-overlapping. */
export function nonProse(text: string): [number, number][] {
  const out: [number, number][] = []
  const add = (a: number, b: number) => { if (b > a) out.push([a, b]) }
  const doc = text.indexOf('\\begin{document}')
  if (text.includes('\\documentclass') && doc > 0) add(0, doc + '\\begin{document}'.length)
  const res: RegExp[] = [
    COMMENT, // comments
    INLINE_MATH,
    new RegExp(String.raw`\\(?:${ARG_CMDS})\*?\s*(?:\[[^\]]{0,300}\]\s*){0,4}(?:\{[^{}]{0,2000}\})?`, 'g'), // commands with non-prose arguments
    /\\[A-Za-z@]+\*?|\\./g, // any other command name
    /\[[^\]\n]{0,80}\]/g, // optional arguments, e.g. [width=0.5\linewidth]
  ]
  for (const re of res) for (const m of text.matchAll(re)) add(m.index!, m.index! + m[0].length)
  for (const [a, b] of [...spans(text, envOpen(MATH_ENVS), envClose), ...spans(text, MATH_OPEN, mathClose)]) add(a, b) // linear time
  out.sort((a, b) => a[0] - b[0])
  const merged: [number, number][] = []
  for (const r of out) {
    const last = merged[merged.length - 1]
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1])
    else merged.push([r[0], r[1]])
  }
  return merged
}

/** Browser spell checking for a LaTeX file, in `lang` (e.g. en-GB) when given. */
export function spellcheck(on: boolean, lang: string, isTex: boolean): Extension {
  if (!on || !isTex) return EditorView.contentAttributes.of({ spellcheck: 'false' })
  const plugin = ViewPlugin.fromClass(class {
    decorations: DecorationSet = Decoration.none
    timer: ReturnType<typeof setTimeout> | undefined
    constructor(readonly view: EditorView) { this.decorations = this.build() }
    build(): DecorationSet {
      const b = new RangeSetBuilder<Decoration>()
      for (const [a, z] of nonProse(this.view.state.doc.toString())) b.add(a, z, NO_CHECK)
      return b.finish()
    }
    update(u: ViewUpdate) {
      if (!u.docChanged) return
      this.decorations = this.decorations.map(u.changes)
      clearTimeout(this.timer)
      this.timer = setTimeout(() => { this.decorations = this.build(); this.view.dispatch({}) }, 400)
    }
    destroy() { clearTimeout(this.timer) }
  }, { decorations: (v) => v.decorations })
  return [
    EditorView.contentAttributes.of({ spellcheck: 'true', autocorrect: 'off', autocapitalize: 'off', ...(lang ? { lang } : {}) }),
    plugin,
  ]
}
