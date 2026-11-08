// A small Markdown renderer for the Preview tab. It builds DOM nodes directly and never parses
// HTML, so a document can't inject anything: raw HTML shows as text, links are limited to
// http, https and mailto, and only images from the project itself load (no tracking pixels).
import { t } from './i18n'
import { h } from './dom'

export interface MarkdownHost {
  /** An image from the project, by its path; null if there's no such image. */
  image(path: string, alt: string): HTMLElement | null
  /** Renders $…$ and $$…$$; absent if KaTeX isn't available (the source is then shown). */
  math?(tex: string, display: boolean, into: HTMLElement): void
}

const MAX_DEPTH = 24
const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/
const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/
const FENCE = /^( {0,3})(`{3,}|~{3,})[ \t]*([^\s`]*)/
const QUOTE = /^ {0,3}>/
const ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)/
const INTERRUPTING_ITEM = /^ {0,3}(?:[-*+]|1[.)])[ \t]+\S/
const TABLE_RULE = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/
const COMMENT = /^ {0,3}<!--/
const DISPLAY_MATH = /^ {0,3}\$\$/
const ESCAPABLE = '\\`*_{}[]()#+-.!|~$<>"\''

const indentOf = (l: string) => l.length - l.trimStart().length
const blank = (l: string) => !l.trim()
const startsBlock = (l: string) => HEADING.test(l) || FENCE.test(l) || QUOTE.test(l) || HR.test(l) || INTERRUPTING_ITEM.test(l) || DISPLAY_MATH.test(l) || COMMENT.test(l)

/** The project path `ref` points to, seen from the file `from`; null if it leaves the project. */
function resolvePath(from: string, ref: string): string | null {
  let clean = ref.split(/[?#]/)[0]
  try { clean = decodeURIComponent(clean) } catch {}
  const parts = clean.startsWith('/') ? [] : from.split('/').slice(0, -1)
  for (const seg of clean.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg !== '..') parts.push(seg)
    else if (!parts.length) return null
    else parts.pop()
  }
  return parts.join('/')
}

class Renderer {
  constructor(private file: string, private host: MarkdownHost) {}

  blocks(lines: string[], depth: number, parent: Node) {
    let i = 0
    while (i < lines.length) {
      const l = lines[i]
      if (blank(l)) { i++; continue }
      if (COMMENT.test(l)) {
        while (i < lines.length && !lines[i].includes('-->')) i++
        i++
        continue
      }
      const fence = FENCE.exec(l)
      if (fence) { i = this.fenced(lines, i, fence, parent); continue }
      if (DISPLAY_MATH.test(l)) { i = this.displayMath(lines, i, parent); continue }
      if (indentOf(l) >= 4) { i = this.indented(lines, i, parent); continue }
      const heading = HEADING.exec(l)
      if (heading) {
        const el = h(`h${heading[1].length}` as 'h1', {})
        this.inline(heading[2] ?? '', el, 0)
        parent.appendChild(el)
        i++
        continue
      }
      if (HR.test(l)) { parent.appendChild(h('hr', {})); i++; continue }
      if (depth < MAX_DEPTH && QUOTE.test(l)) { i = this.quote(lines, i, depth, parent); continue }
      if (depth < MAX_DEPTH && ITEM.test(l)) { i = this.list(lines, i, depth, parent); continue }
      if (i + 1 < lines.length && l.includes('|') && TABLE_RULE.test(lines[i + 1]) && lines[i + 1].includes('-')) { i = this.table(lines, i, parent); continue }
      i = this.paragraph(lines, i, parent)
    }
  }

  private fenced(lines: string[], start: number, m: RegExpExecArray, parent: Node): number {
    const close = new RegExp(`^ {0,3}${m[2][0]}{${m[2].length},}[ \\t]*$`)
    const body: string[] = []
    let i = start + 1
    for (; i < lines.length && !close.test(lines[i]); i++) body.push(lines[i].slice(Math.min(m[1].length, indentOf(lines[i]))))
    const code = h('code', {}, body.join('\n'))
    if (m[3]) code.dataset.lang = m[3]
    parent.appendChild(h('pre', {}, code))
    return i + 1
  }

  private indented(lines: string[], start: number, parent: Node): number {
    const body: string[] = []
    let i = start
    for (; i < lines.length && (blank(lines[i]) || indentOf(lines[i]) >= 4); i++) body.push(lines[i].slice(4))
    while (body.length && blank(body[body.length - 1])) body.pop()
    parent.appendChild(h('pre', {}, h('code', {}, body.join('\n'))))
    return i
  }

  private displayMath(lines: string[], start: number, parent: Node): number {
    const first = lines[start].trim().slice(2)
    if (first.endsWith('$$') && first.length > 2) { this.mathInto(first.slice(0, -2), true, parent); return start + 1 }
    const body = [first]
    let i = start + 1
    for (; i < lines.length && !lines[i].trim().endsWith('$$'); i++) body.push(lines[i])
    if (i < lines.length) body.push(lines[i].trim().slice(0, -2))
    this.mathInto(body.join('\n'), true, parent)
    return i + 1
  }

  private mathInto(tex: string, display: boolean, parent: Node) {
    const el = h(display ? 'div' : 'span', { class: display ? 'md-math' : '' })
    if (this.host.math) this.host.math(tex, display, el)
    else el.textContent = display ? `$$${tex}$$` : `$${tex}$`
    parent.appendChild(el)
  }

  private quote(lines: string[], start: number, depth: number, parent: Node): number {
    const body: string[] = []
    let i = start
    for (; i < lines.length; i++) {
      if (QUOTE.test(lines[i])) body.push(lines[i].replace(/^ {0,3}> ?/, ''))
      else if (!blank(lines[i]) && !startsBlock(lines[i]) && body.length && !blank(body[body.length - 1])) body.push(lines[i])
      else break
    }
    const el = h('blockquote', {})
    this.blocks(body, depth + 1, el)
    parent.appendChild(el)
    return i
  }

  private list(lines: string[], start: number, depth: number, parent: Node): number {
    const isOrdered = (m: RegExpExecArray) => /\d/.test(m[2])
    const first = ITEM.exec(lines[start])!
    const ordered = isOrdered(first)
    const list = h(ordered ? 'ol' : 'ul', {})
    if (ordered && parseInt(first[2], 10) !== 1) list.setAttribute('start', String(parseInt(first[2], 10)))
    let i = start
    while (i < lines.length) {
      const m = ITEM.exec(lines[i])
      if (!m || HR.test(lines[i]) || isOrdered(m) !== ordered) break
      const indent = m[3] ? m[0].length : m[1].length + m[2].length + 1
      const body = [lines[i].slice(indent)]
      i++
      while (i < lines.length) {
        const l = lines[i]
        if (blank(l)) {
          let j = i + 1
          while (j < lines.length && blank(lines[j])) j++
          if (j >= lines.length || indentOf(lines[j]) < indent) break
          body.push('')
          i++
        }
        else if (indentOf(l) >= indent) { body.push(l.slice(indent)); i++ }
        else if (ITEM.test(l) || startsBlock(l)) break
        else { body.push(l.trim()); i++ }
      }
      let next = i
      while (next < lines.length && blank(lines[next])) next++
      const following = next < lines.length ? ITEM.exec(lines[next]) : null
      if (following && !HR.test(lines[next]) && isOrdered(following) === ordered) i = next
      list.appendChild(this.item(body, depth))
    }
    parent.appendChild(list)
    return i
  }

  private item(body: string[], depth: number): HTMLElement {
    const li = h('li', {})
    const task = /^\[([ xX])\][ \t]+/.exec(body[0])
    if (task) {
      body[0] = body[0].slice(task[0].length)
      li.className = 'task'
      const box = h('input', { type: 'checkbox', disabled: true }) as HTMLInputElement
      box.checked = task[1] !== ' '
      li.appendChild(box)
    }
    const inner = document.createDocumentFragment()
    this.blocks(body, depth + 1, inner)
    if (!body.includes('')) for (const p of [...inner.children].filter((c) => c.tagName === 'P')) p.replaceWith(...p.childNodes) // a tight list
    li.appendChild(inner)
    return li
  }

  private table(lines: string[], start: number, parent: Node): number {
    const cells = (l: string) => {
      const out: string[] = []
      let cur = '', code = false
      const s = l.trim().replace(/^\|/, '')
      for (let i = 0; i < s.length; i++) {
        if (s[i] === '\\' && s[i + 1] === '|') { cur += '|'; i++ }
        else if (s[i] === '`') { code = !code; cur += '`' }
        else if (s[i] === '|' && !code) { out.push(cur); cur = '' }
        else cur += s[i]
      }
      if (cur.trim()) out.push(cur)
      return out.map((c) => c.trim())
    }
    const aligns = cells(lines[start + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : ''))
    const row = (values: string[], tag: 'th' | 'td') => h('tr', {}, ...aligns.map((a, k) => {
      const cell = h(tag, a ? { class: `md-${a}` } : {})
      this.inline(values[k] ?? '', cell, 0)
      return cell
    }))
    const body = h('tbody', {})
    let i = start + 2
    for (; i < lines.length && !blank(lines[i]) && !startsBlock(lines[i]); i++) body.appendChild(row(cells(lines[i]), 'td'))
    parent.appendChild(h('div', { class: 'md-table' }, h('table', {}, h('thead', {}, row(cells(lines[start]), 'th')), body)))
    return i
  }

  private paragraph(lines: string[], start: number, parent: Node): number {
    const text: string[] = []
    let i = start, level = 0
    for (; i < lines.length && !blank(lines[i]); i++) {
      const setext = i > start ? SETEXT.exec(lines[i]) : null
      if (setext) { level = setext[1][0] === '=' ? 1 : 2; i++; break }
      if (i > start && startsBlock(lines[i])) break
      text.push(i > start ? lines[i].trimStart() : lines[i].trim())
    }
    const el = h(level ? (`h${level}` as 'h1') : 'p', {})
    this.inline(text.join('\n'), el, 0)
    parent.appendChild(el)
    return i
  }

  // ---- within a line ----------------------------------------------------------------------
  private inline(text: string, parent: Node, depth: number) {
    if (depth > MAX_DEPTH) { parent.appendChild(document.createTextNode(text)); return }
    let buf = '', i = 0
    const flush = () => { if (buf) { parent.appendChild(document.createTextNode(buf)); buf = '' } }
    const put = (node: Node, end: number) => { flush(); parent.appendChild(node); i = end }
    while (i < text.length) {
      const c = text[i]
      if (c === '\\' && text[i + 1] === '\n') { put(h('br', {}), i + 2); continue }
      if (c === '\\' && ESCAPABLE.includes(text[i + 1] ?? ' ')) { buf += text[i + 1]; i += 2; continue }
      if (c === '\n') {
        if (/ {2,}$/.test(buf)) { buf = buf.trimEnd(); put(h('br', {}), i + 1) }
        else { buf = `${buf.trimEnd()} `; i++ }
        continue
      }
      if (c === '`') {
        let run = 1
        while (text[i + run] === '`') run++
        const close = this.closingTicks(text, i + run, run)
        if (close >= 0) { put(h('code', {}, text.slice(i + run, close).replace(/\n/g, ' ')), close + run); continue }
        buf += text.slice(i, i + run)
        i += run
        continue
      }
      if (c === '$') {
        const close = this.closingDollar(text, i)
        if (close > 0) { flush(); this.mathInto(text.slice(i + 1, close), false, parent); i = close + 1; continue }
      }
      if (c === '!' && text[i + 1] === '[') {
        const link = this.link(text, i + 1)
        if (link) { put(this.image(link.label, link.dest), link.end); continue }
      }
      if (c === '[') {
        const link = this.link(text, i)
        if (link) { put(this.anchor(link.label, link.dest, link.title, depth), link.end); continue }
      }
      if (c === '<') {
        const auto = /^<((?:https?:|mailto:)[^\s<>]+)>/i.exec(text.slice(i, i + 2100))
        if (auto) { put(this.anchor(auto[1].replace(/^mailto:/i, ''), auto[1], '', depth, true), i + auto[0].length); continue }
      }
      if ((c === 'h' || c === 'H') && !/\w/.test(text[i - 1] ?? '')) {
        const bare = /^https?:\/\/[^\s<]+/i.exec(text.slice(i, i + 2100))
        if (bare) {
          const url = bare[0].replace(/[.,:;!?)*_~'"]+$/, '')
          put(this.anchor(url, url, '', depth, true), i + url.length)
          continue
        }
      }
      const emphasis = this.emphasis(text, i, depth)
      if (emphasis) { put(emphasis.node, emphasis.end); continue }
      buf += c
      i++
    }
    flush()
  }

  private closingTicks(text: string, from: number, run: number): number {
    for (let i = from; i < text.length; i++) {
      if (text[i] !== '`') continue
      let n = 1
      while (text[i + n] === '`') n++
      if (n === run) return i
      i += n - 1
    }
    return -1
  }

  private closingDollar(text: string, open: number): number {
    if (!text[open + 1] || /\s|\$/.test(text[open + 1])) return -1
    for (let i = open + 1; i < text.length && text[i] !== '\n'; i++) {
      if (text[i] === '\\') { i++; continue }
      if (text[i] === '$') return /\s/.test(text[i - 1]) || /\d/.test(text[i + 1] ?? '') ? -1 : i
    }
    return -1
  }

  private emphasis(text: string, i: number, depth: number): { node: Node; end: number } | null {
    for (const [mark, tag] of [['**', 'strong'], ['__', 'strong'], ['~~', 'del'], ['*', 'em'], ['_', 'em']] as const) {
      if (!text.startsWith(mark, i)) continue
      if (mark[0] === '_' && /\w/.test(text[i - 1] ?? '')) continue
      const start = i + mark.length
      if (!text[start] || /\s/.test(text[start])) continue
      const close = this.closingMark(text, mark, start)
      if (close < 0) continue
      const node = h(tag, {})
      this.inline(text.slice(start, close), node, depth + 1)
      return { node, end: close + mark.length }
    }
    return null
  }

  private closingMark(text: string, mark: string, start: number): number {
    for (let i = start + 1; i < text.length; i++) {
      if (text[i] === '\\') { i++; continue }
      if (text[i] === '`') {
        let run = 1
        while (text[i + run] === '`') run++
        const close = this.closingTicks(text, i + run, run)
        if (close >= 0) i = close + run - 1
        else i += run - 1
        continue
      }
      if (!text.startsWith(mark, i) || /\s/.test(text[i - 1])) continue
      if (mark.length === 2 && text[i + 2] === mark[0]) continue // "***": the last two close it
      if (mark.length === 1 && text[i + 1] === mark) { i++; continue } // part of "**"
      if (mark[0] === '_' && /\w/.test(text[i + mark.length] ?? '')) continue
      return i
    }
    return -1
  }

  private link(text: string, open: number): { label: string; dest: string; title: string; end: number } | null {
    let level = 0, close = -1
    for (let i = open; i < text.length; i++) {
      if (text[i] === '\\') { i++; continue }
      if (text[i] === '[') level++
      else if (text[i] === ']' && --level === 0) { close = i; break }
    }
    if (close < 0 || text[close + 1] !== '(') return null
    const m = /^\(\s*(<[^<>\n]*>|(?:[^\s()]|\((?:[^\s()])*\))*)(?:\s+(?:"([^"]*)"|'([^']*)'))?\s*\)/.exec(text.slice(close + 1))
    if (!m) return null
    return { label: text.slice(open + 1, close), dest: m[1].replace(/^<|>$/g, ''), title: m[2] ?? m[3] ?? '', end: close + 1 + m[0].length }
  }

  private anchor(label: string, dest: string, title: string, depth: number, plain = false): Node {
    const out: HTMLElement = /^(?:https?:|mailto:)/i.test(dest)
      ? h('a', { href: dest, target: '_blank', rel: 'noopener noreferrer', title: title || dest })
      : h('span', { title: t('Links inside the project aren’t followed in the preview.') })
    if (plain) out.textContent = label
    else this.inline(label, out, depth + 1)
    return out
  }

  private image(alt: string, dest: string): Node {
    const external = /^[a-z][a-z0-9+.-]*:/i.test(dest)
    const path = external ? null : resolvePath(this.file, dest)
    const found = path === null ? null : this.host.image(path, alt)
    if (found) return found
    if (/^https?:/i.test(dest)) {
      return h('a', { class: 'md-noimage', href: dest, target: '_blank', rel: 'noopener noreferrer', title: t('Images from other sites aren’t loaded, so they can’t tell anyone you’re reading this.') }, `🖼 ${alt || dest}`)
    }
    return h('span', { class: 'md-noimage' }, t('Image not found: {{name}}', { name: dest }))
  }
}

export function renderMarkdown(source: string, file: string, host: MarkdownHost): DocumentFragment {
  const out = document.createDocumentFragment()
  const text = source.replace(/\r\n?/g, '\n').replace(/^---\n[\s\S]*?\n---[ \t]*(?:\n|$)/, '') // front matter isn't part of the text
  const lines = text.split('\n').map((l) => l.replace(/^[ \t]+/, (ws) => ws.replace(/\t/g, '    ')))
  new Renderer(file, host).blocks(lines, 0, out)
  return out
}
