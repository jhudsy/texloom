// Finding your way around a project: search and replace across all files, the
// document outline (following \input/\include from the main file), and word count.
import type { Replica } from './replica'
import { h } from './dom'
import { mainFile } from './projectfiles'
import { t, tn, locale } from './i18n'
import { texCountDialog } from './texcount'
import { COMMENT, envClose, envOpen, INLINE_MATH, MATH_OPEN, mathClose, spans } from './texscan'

export type Reveal = (file: string, from: number, to: number) => void

// ---------------------------------------------------------------------------
// Word count and outline
// ---------------------------------------------------------------------------
export interface Heading { level: number; title: string; file: string; offset: number; line: number; words: number; total: number }
export interface Outline { headings: Heading[]; words: number; files: string[] }

const LEVELS: Record<string, number> = { part: 0, chapter: 1, section: 2, subsection: 3, subsubsection: 4, paragraph: 5, subparagraph: 6 }
const SKIP_ENVS = 'equation|align|gather|multline|eqnarray|math|displaymath|flalign|alignat|verbatim|Verbatim|lstlisting|minted|tikzpicture|comment|filecontents'

/** Replace everything that isn't prose (comments, maths, code) with spaces, keeping offsets and newlines. */
export function blankNonProse(text: string): string {
  const blank = (s: string) => s.replace(/[^\n]/g, ' ')
  const blankSpans = (t: string, ranges: [number, number][]) => {
    let out = '', i = 0
    for (const [a, b] of ranges) { out += t.slice(i, a) + blank(t.slice(a, b)); i = b }
    return out + t.slice(i)
  }
  let t = text.replace(COMMENT, blank)
  t = blankSpans(t, spans(t, envOpen(SKIP_ENVS), envClose))
  t = blankSpans(t, spans(t, MATH_OPEN, mathClose))
  return t.replace(INLINE_MATH, blank)
}

// Chinese and Japanese aren't written with spaces, and are counted by the character.
const CHARACTER_COUNTED = /[\p{sc=Han}\p{sc=Hiragana}\p{sc=Katakana}ー]/gu

/** Words of prose in already-blanked LaTeX (in the spirit of texcount's text words). */
export function countWords(blanked: string): number {
  const t = blanked
    // commands whose arguments aren't prose
    .replace(/\\(?:label|ref|eqref|[cC]ref|autoref|pageref|nameref|cite\w*|nocite|includegraphics|usepackage|RequirePackage|documentclass|bibliography(?:style)?|addbibresource|input|include|subfile|begin|end|(?:re)?newcommand|newenvironment|DeclareMathOperator|url|href|[vh]space|setlength|setcounter|addtocounter|graphicspath|hypersetup|pagestyle|thispagestyle|newtheorem|providecommand)\*?\s*(?:\[[^\]]{0,300}\]\s*){0,4}(?:\{[^{}]{0,2000}\}\s*)?/g, ' ')
    .replace(/\\[A-Za-z@]+\*?/g, ' ')
    .replace(/\\./g, ' ')
  const characters = t.match(CHARACTER_COUNTED)?.length ?? 0
  return characters + (t.replace(CHARACTER_COUNTED, ' ').match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) ?? []).filter((w) => /\p{L}/u.test(w)).length
}

/** The argument starting at `open` ('{'), or null. */
function group(text: string, open: number): { content: string; end: number } | null {
  if (text[open] !== '{') return null
  let depth = 0
  for (let i = open; i < text.length && i < open + 2000; i++) {
    const c = text[i]
    if (c === '\\') { i++; continue }
    if (c === '{') depth++
    else if (c === '}' && --depth === 0) return { content: text.slice(open + 1, i), end: i + 1 }
  }
  return null
}

function resolveInput(r: Replica, from: string, name: string): string | null {
  const dir = from.includes('/') ? from.slice(0, from.lastIndexOf('/') + 1) : ''
  const n = name.trim().replace(/^\.\//, '')
  for (const c of [n, `${n}.tex`, dir + n, `${dir}${n}.tex`]) if (r.files.has(c)) return c
  return null
}

export function outline(r: Replica): Outline {
  const main = mainFile(r)
  const headings: Heading[] = []
  const files: string[] = []
  let words = 0
  const visited = new Set<string>()
  const add = (n: number) => { words += n; if (headings.length) headings[headings.length - 1].words += n }

  const walk = (file: string, isMain: boolean) => {
    if (visited.has(file) || visited.size > 200) return
    visited.add(file); files.push(file)
    const text = r.files.get(file)!.toString()
    const blanked = blankNonProse(text)
    let start = 0, end = blanked.length
    if (isMain) {
      const b = blanked.indexOf('\\begin{document}'), e = blanked.indexOf('\\end{document}')
      if (b >= 0) start = b + '\\begin{document}'.length
      if (e >= 0) end = e
    }
    const re = /\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^\]]{0,300}\]\s*)?(?=\{)|\\(input|include|subfile)\s*\{([^}]{1,300})\}/g
    re.lastIndex = start
    let pos = start, m: RegExpExecArray | null
    let lineAt = 0, line = 1 // counted incrementally: re-splitting the text per heading is quadratic
    while ((m = re.exec(blanked)) && m.index < end) {
      add(countWords(blanked.slice(pos, m.index)))
      if (m[1]) {
        const g = group(text, m.index + m[0].length)
        const title = (g?.content ?? '').replace(/\\[A-Za-z@]+\*?\s*/g, '').replace(/[{}]/g, '').replace(/\s+/g, ' ').trim()
        pos = g?.end ?? re.lastIndex
        re.lastIndex = pos
        for (let i = text.indexOf('\n', lineAt); i >= 0 && i < m.index; i = text.indexOf('\n', i + 1)) { line++; lineAt = i + 1 }
        headings.push({ level: LEVELS[m[1]], title: title || t('(untitled)'), file, offset: m.index, line, words: 0, total: 0 })
        add(countWords(blankNonProse(g?.content ?? '')))
      } else {
        pos = re.lastIndex
        const target = resolveInput(r, file, m[3])
        if (target) walk(target, false)
      }
    }
    add(countWords(blanked.slice(pos, end)))
  }

  if (main) walk(main, true)
  else for (const f of [...r.files.keys()].filter((p) => p.endsWith('.tex')).sort()) walk(f, false)

  // a heading's total includes its subsections
  for (let i = 0; i < headings.length; i++) {
    let t = headings[i].words
    for (let j = i + 1; j < headings.length && headings[j].level > headings[i].level; j++) t += headings[j].words
    headings[i].total = t
  }
  return { headings, words, files }
}

export function createOutlinePanel(r: Replica, reveal: Reveal, selectionWords: () => number | null): { el: HTMLElement; refresh(): void } {
  const list = h('ul', { class: 'outline' })
  const count = h('div', { class: 'wordcount muted small' })
  const detailed = h('button', { class: 'small', title: t('Text, headers, captions and maths, counted by TeXCount'), onclick: () => texCountDialog(r) }, t('Detailed count…'))
  const el = h('div', { class: 'navpanel' }, h('div', { class: 'wordcount-row' }, count, detailed), list)
  let timer: ReturnType<typeof setTimeout> | undefined

  function refresh() {
    const o = outline(r)
    const sel = selectionWords()
    count.textContent = tn(o.words, '{{n}} word', '{{n}} words', { n: o.words.toLocaleString(locale) }) + (sel ? ` · ${tn(sel, '{{n}} selected', '{{n}} selected', { n: sel.toLocaleString(locale) })}` : '')
    count.title = tn(o.files.length, 'Words of text in {{n}} file (from the main file through \\input/\\include). Maths, comments, code and labels aren’t counted.',
      'Words of text in {{n}} files (from the main file through \\input/\\include). Maths, comments, code and labels aren’t counted.')
    const min = Math.min(...o.headings.map((x) => x.level))
    list.replaceChildren(...(o.headings.length ? o.headings.map((x) => h('li', {
      style: `padding-left: ${8 + (x.level - min) * 12}px`, class: `lvl${x.level - min}`, title: `${x.file}:${x.line}`,
      onclick: () => reveal(x.file, x.offset, x.offset),
    }, h('span', { class: 'otitle' }, x.title), h('span', { class: 'muted small' }, x.total.toLocaleString(locale))))
      : [h('li', { class: 'muted small' }, t('No sections yet. \\section{…}, \\chapter{…} etc. appear here.'))]))
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(() => { if (el.isConnected && el.offsetParent) refresh() }, 600) }
  r.files.observeDeep(later)
  r.meta.observe(later)
  return { el, refresh }
}

// ---------------------------------------------------------------------------
// Search and replace across files
// ---------------------------------------------------------------------------
interface Hit { file: string; from: number; to: number; line: number; before: string; match: string; after: string }

export function createSearchPanel(r: Replica, writable: boolean, reveal: Reveal): { el: HTMLElement; focus(seed?: string): void } {
  const q = h('input', { type: 'search', placeholder: t('Search all files'), 'aria-label': t('Search all files') })
  const rep = h('input', { placeholder: t('Replace with'), 'aria-label': t('Replace with'), hidden: !writable })
  const opt = (label: string, title: string) => {
    const b = h('button', { class: 'opt', title, 'aria-pressed': 'false', onclick: () => { b.classList.toggle('on'); b.setAttribute('aria-pressed', String(b.classList.contains('on'))); run() } }, label)
    return b
  }
  const caseBtn = opt('Aa', t('Match case')), wordBtn = opt('ab', t('Whole words')), reBtn = opt('.*', t('Regular expression'))
  const info = h('div', { class: 'muted small searchinfo' })
  const results = h('ul', { class: 'results' })
  const replaceAllBtn = h('button', { hidden: !writable, onclick: () => replaceAll() }, t('Replace all'))
  const el = h('div', { class: 'navpanel search' },
    h('div', { class: 'searchrow' }, q, caseBtn, wordBtn, reBtn),
    writable ? h('div', { class: 'searchrow' }, rep, replaceAllBtn) : null,
    info, results)
  let hits: Hit[] = []
  let timer: ReturnType<typeof setTimeout> | undefined

  function pattern(): RegExp | null {
    const s = q.value
    if (!s) return null
    let src = reBtn.classList.contains('on') ? s : s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    if (wordBtn.classList.contains('on')) src = `(?<![\\p{L}\\p{N}_])(?:${src})(?![\\p{L}\\p{N}_])`
    try { return new RegExp(src, `gu${caseBtn.classList.contains('on') ? '' : 'i'}`) } catch { return null }
  }

  function run() {
    const re = pattern()
    hits = []
    q.classList.toggle('bad', !!q.value && !re)
    if (!re) { results.replaceChildren(); info.textContent = q.value ? t('Invalid regular expression.') : ''; return }
    const files = [...r.files.keys()].sort()
    let capped = false
    for (const f of files) {
      const text = r.files.get(f)!.toString()
      re.lastIndex = 0
      let m: RegExpExecArray | null
      while ((m = re.exec(text))) {
        if (!m[0].length) { re.lastIndex++; continue }
        const ls = text.lastIndexOf('\n', m.index - 1) + 1
        let le = text.indexOf('\n', m.index + m[0].length); if (le < 0) le = text.length
        hits.push({ file: f, from: m.index, to: m.index + m[0].length, line: text.slice(0, m.index).split('\n').length,
          before: text.slice(Math.max(ls, m.index - 40), m.index), match: m[0], after: text.slice(m.index + m[0].length, Math.min(le, m.index + m[0].length + 60)) })
        if (hits.length >= 1000) { capped = true; break }
      }
      if (capped) break
    }
    const nFiles = new Set(hits.map((x) => x.file)).size
    info.textContent = hits.length
      ? tn(hits.length, '{{n}} match in {{files}}', '{{n}} matches in {{files}}', { n: `${hits.length}${capped ? '+' : ''}`, files: tn(nFiles, '{{n}} file', '{{n}} files') })
      : t('No matches.')
    const rows: HTMLElement[] = []
    let last = ''
    for (const x of hits) {
      if (x.file !== last) { rows.push(h('li', { class: 'rfile' }, x.file)); last = x.file }
      rows.push(h('li', { class: 'hit', title: `${x.file}:${x.line}`, onclick: () => reveal(x.file, x.from, x.to) },
        h('span', { class: 'ln muted' }, String(x.line)), h('span', { class: 'ctx' }, x.before, h('mark', {}, x.match), x.after),
        writable ? h('button', { class: 'icon', title: t('Replace this one'), onclick: (e: Event) => { e.stopPropagation(); replaceOne(x) } }, '↺') : null))
    }
    results.replaceChildren(...rows)
  }

  const replacement = (match: string) => {
    if (!reBtn.classList.contains('on')) return rep.value
    const re = pattern()!
    re.lastIndex = 0
    return match.replace(re, rep.value) // $1 etc. work with regular expressions
  }

  function replaceOne(x: Hit) {
    const yt = r.files.get(x.file)
    if (!yt || yt.toString().slice(x.from, x.to) !== x.match) return run()
    r.doc.transact(() => { yt.delete(x.from, x.to - x.from); yt.insert(x.from, replacement(x.match)) })
    run()
  }

  function replaceAll() {
    if (!hits.length) return
    const nFiles = new Set(hits.map((x) => x.file)).size
    if (!confirm(tn(hits.length, 'Replace {{n}} match in {{files}}? (Undo with History if needed.)', 'Replace {{n}} matches in {{files}}? (Undo with History if needed.)',
      { files: tn(nFiles, '{{n}} file', '{{n}} files') }))) return
    r.doc.transact(() => {
      for (const x of [...hits].sort((a, b) => a.file.localeCompare(b.file) || b.from - a.from)) {
        const yt = r.files.get(x.file)
        if (!yt || yt.toString().slice(x.from, x.to) !== x.match) continue
        yt.delete(x.from, x.to - x.from); yt.insert(x.from, replacement(x.match))
      }
    })
    run()
  }

  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 200) })
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter' && hits[0]) reveal(hits[0].file, hits[0].from, hits[0].to) })
  r.files.observeDeep(() => { if (q.value && el.offsetParent) { clearTimeout(timer); timer = setTimeout(run, 400) } })
  return { el, focus: (seed?: string) => { if (seed) { q.value = seed; run() } q.focus(); q.select() } }
}
