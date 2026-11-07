// Add a bibliography entry from a DOI or arXiv id: fetched as BibTeX from doi.org
// (arXiv papers have DOIs 10.48550/arXiv.<id>), given a readable key, and appended
// to the project's .bib file.
import * as Y from 'yjs'
import type { Replica } from './replica'
import type { ProjectIndex } from './latex'
import { dialog, h } from './dom'
import { cleanPath, mainFile } from './projectfiles'

/** The DOI for a pasted DOI, doi.org/arXiv URL or arXiv id, or null. */
export function parseId(input: string): string | null {
  const s = input.trim().replace(/^<|>$/g, '')
  const doi = /(?:doi:\s*|doi\.org\/|^)(10\.\d{4,9}\/\S+)$/i.exec(s.replace(/^https?:\/\/(dx\.)?/i, ''))
  if (doi) {
    const d = decodeURIComponent(doi[1]).replace(/[.,;]+$/, '')
    const ax = /^10\.48550\/arxiv\.(.+)$/i.exec(d)
    return ax ? `10.48550/arXiv.${ax[1].replace(/v\d+$/, '')}` : d
  }
  const ax = /(?:arxiv(?:\.org\/(?:abs|pdf)\/|:)\s*|^)((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?\/\d{7}))(?:v\d+)?(?:\.pdf)?$/i.exec(s)
  return ax ? `10.48550/arXiv.${ax[1]}` : null
}

export async function fetchBibtex(doi: string): Promise<string> {
  const res = await fetch(`https://doi.org/${doi.split('/').map(encodeURIComponent).join('/')}`, {
    headers: { Accept: 'application/x-bibtex; charset=utf-8' },
  })
  if (res.status === 404) throw new Error(`No record found for ${doi}.`)
  if (!res.ok) throw new Error(`doi.org answered ${res.status}.`)
  const text = (await res.text()).trim()
  if (!text.startsWith('@')) throw new Error('doi.org didn’t return BibTeX for this id.')
  if (text.length > 50_000) throw new Error('The record is unexpectedly large.')
  return text
}

/** Parse one BibTeX entry into type and fields (values keep their braces stripped of the outer pair). */
export function parseEntry(bib: string): { type: string; key: string; fields: [string, string][] } | null {
  const head = /^@(\w+)\s*\{\s*([^,]*),/.exec(bib)
  if (!head) return null
  const fields: [string, string][] = []
  let i = head[0].length
  while (i < bib.length) {
    const m = /^\s*([A-Za-z][\w-]*)\s*=\s*/.exec(bib.slice(i))
    if (!m) break
    i += m[0].length
    let value = ''
    if (bib[i] === '{' || bib[i] === '"') {
      const close = bib[i] === '{' ? '}' : '"'
      let depth = 0, j = i
      for (; j < bib.length; j++) {
        const c = bib[j]
        if (c === '\\') { j++; continue }
        if (close === '"' && c === '"' && j > i) break
        if (c === '{') depth++
        else if (c === '}') { depth--; if (close === '}' && depth === 0) break }
      }
      value = bib.slice(i + 1, j); i = j + 1
    } else {
      const v = /^[^,}\s]+/.exec(bib.slice(i)); value = v?.[0] ?? ''; i += value.length
    }
    fields.push([m[1].toLowerCase(), value.replace(/\s+/g, ' ').trim()])
    const sep = /^\s*,?/.exec(bib.slice(i)); i += sep?.[0].length ?? 0
  }
  return { type: head[1].toLowerCase(), key: head[2].trim(), fields }
}

const ascii = (s: string) => s.normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/\\.\{?(\w)\}?/g, '$1').replace(/[^A-Za-z0-9]/g, '')
const STOP = new Set(['a', 'an', 'the', 'on', 'of', 'in', 'for', 'and', 'to', 'with', 'is', 'are', 'towards', 'toward', 'from', 'at', 'by'])

/** A key like vaswani2017attention, unique among `taken`. */
export function makeKey(fields: [string, string][], taken: Set<string>): string {
  const get = (n: string) => fields.find(([k]) => k === n)?.[1] ?? ''
  const first = get('author').split(/\s+and\s+/i)[0] ?? ''
  const last = first.includes(',') ? first.split(',')[0] : first.trim().split(/\s+/).pop() ?? ''
  const word = get('title').replace(/[{}$\\]/g, '').split(/[^\p{L}\p{N}]+/u).find((w) => w && !STOP.has(w.toLowerCase())) ?? ''
  const base = (ascii(last).toLowerCase() || 'ref') + (get('year').replace(/\D/g, '').slice(0, 4)) + ascii(word).toLowerCase()
  let key = base
  for (let i = 0; taken.has(key); i++) key = base + String.fromCharCode(97 + (i % 26)) + (i >= 26 ? Math.floor(i / 26) : '')
  return key
}

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

export function formatEntry(type: string, key: string, fields: [string, string][]): string {
  const order = ['author', 'title', 'journal', 'booktitle', 'publisher', 'volume', 'number', 'pages', 'year', 'month', 'doi', 'url', 'eprint', 'archiveprefix', 'primaryclass']
  const keep = fields.filter(([k]) => !['copyright', 'issn', 'keywords'].includes(k))
  keep.sort((a, b) => (order.indexOf(a[0]) + 1 || 99) - (order.indexOf(b[0]) + 1 || 99))
  const pad = Math.max(...keep.map(([k]) => k.length))
  const lines = keep.map(([k, v]) => {
    if (k === 'month' && MONTHS.includes(v.toLowerCase().slice(0, 3))) return `  ${k.padEnd(pad)} = ${v.toLowerCase().slice(0, 3)}`
    return `  ${k.padEnd(pad)} = {${k === 'pages' ? v.replace(/[–—]/g, '--').replace(/(\d)-(\d)/, '$1--$2') : v}}`
  })
  return `@${type}{${key},\n${lines.join(',\n')}\n}\n`
}

export interface Added { key: string; file: string; existed: boolean; entry: string }

// ---- which .bib file -------------------------------------------------------------------------
const stripComments = (t: string) => t.replace(/(^|[^\\])%.*$/gm, '$1')
const dirOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/') + 1) : '')

/** Where a new entry can go, from what the document itself names. */
export interface BibPlan {
  /** Files the entry can go to, best first (the document's own, or every .bib if it names none). */
  options: string[]
  /** The document already names a bibliography (\bibliography or \addbibresource). */
  declared: boolean
  /** Files in `options` that don't exist yet (named by the document, or the default). */
  missing: Set<string>
  main: string | null
  biblatex: boolean
}

export function bibPlan(r: Replica, index: ProjectIndex): BibPlan {
  const idx = index.get()
  const main = mainFile(r)
  const base = main ? dirOf(main) : ''
  const names: string[] = []
  let biblatex = false
  for (const p of [...(main ? [main] : []), ...idx.texFiles.filter((f) => f !== main)]) {
    const t = stripComments(r.files.get(p)?.toString() ?? '')
    if (/\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bbiblatex\b/.test(t)) biblatex = true
    for (const m of t.matchAll(/\\(addbibresource|bibliography)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}/g)) {
      for (const n of m[1] === 'bibliography' ? m[2].split(',') : [m[2]]) {
        const name = n.trim().replace(/^\.\//, '')
        if (name) names.push(m[1] === 'bibliography' && !/\.bib$/i.test(name) ? name + '.bib' : name)
      }
    }
  }
  // names are relative to the main file's folder (where TeX runs); accept project-root paths too
  const resolve = (n: string) => [base + n, n].find((p) => idx.bibFiles.includes(p))
  const found = [...new Set(names.map(resolve).filter((p): p is string => !!p))]
  const plan = { declared: names.length > 0, main, biblatex, missing: new Set<string>() }
  if (found.length) return { ...plan, options: found }
  if (names.length) { // the document names a file that isn't there yet: create it
    const want = [...new Set(names.map((n) => cleanPath(base + n)).filter((p): p is string => !!p && /\.bib$/i.test(p)))]
    if (want.length) return { ...plan, options: want, missing: new Set(want) }
  }
  if (idx.bibFiles.length) return { ...plan, options: idx.bibFiles }
  const fresh = base + 'references.bib'
  return { ...plan, options: [fresh], missing: new Set([fresh]) }
}

/** The line(s) that make the document print `file`, for showing before adding them. */
export function bibliographyLine(plan: BibPlan, file: string): string {
  const base = plan.main ? dirOf(plan.main) : ''
  const rel = file.startsWith(base) ? file.slice(base.length) : file
  return plan.biblatex ? `\\addbibresource{${rel}}` : `\\bibliography{${rel.replace(/\.bib$/i, '')}}`
}

/** Add \bibliography (or biblatex's \addbibresource and \printbibliography) to the main file. */
export function addBibliography(r: Replica, plan: BibPlan, file: string): boolean {
  const yt = plan.main ? r.files.get(plan.main) : undefined
  if (!yt) return false
  const t = yt.toString(), clean = stripComments(t)
  const end = clean.search(/\\end\s*\{document\}/), begin = clean.search(/\\begin\s*\{document\}/)
  if (end < 0 || begin < 0) return false
  const atLine = (i: number) => (i > 0 && t[i - 1] !== '\n' ? '\n' : '')
  const line = bibliographyLine(plan, file)
  r.doc.transact(() => {
    if (plan.biblatex) { // later position first, so the earlier one stays valid
      if (!/\\printbibliography/.test(clean)) yt.insert(end, atLine(end) + '\\printbibliography\n\n')
      yt.insert(begin, atLine(begin) + line + '\n')
    } else {
      let all = ''
      for (const [p, f] of r.files) if (p.endsWith('.tex')) all += stripComments(f.toString())
      const natbib = /\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bnatbib\b/.test(all)
      const style = /\\bibliographystyle\s*\{/.test(all) ? '' : `\\bibliographystyle{${natbib ? 'plainnat' : 'plain'}}\n`
      yt.insert(end, atLine(end) + style + line + '\n\n')
    }
  })
  return true
}

/** Fetch and append to `file` (created if needed); a DOI already in the document's bibliography is reused. */
export async function addReference(r: Replica, index: ProjectIndex, input: string, file?: string): Promise<Added> {
  const doi = parseId(input)
  if (!doi) throw new Error('That doesn’t look like a DOI (10.xxxx/…) or an arXiv id (e.g. 1706.03762).')
  const idx = index.get()
  const plan = bibPlan(r, index)
  file ??= plan.options[0]
  // already there? Only files the document uses count (an entry elsewhere would print as “?”).
  for (const f of plan.declared ? plan.options : idx.bibFiles) {
    const text = r.files.get(f)?.toString() ?? ''
    const at = text.toLowerCase().indexOf(doi.toLowerCase())
    if (at >= 0) {
      const start = text.lastIndexOf('@', at)
      const key = /^@\w+\s*\{\s*([^,\s]+)/.exec(text.slice(start))?.[1]
      if (key) return { key, file: f, existed: true, entry: '' }
    }
  }
  const raw = await fetchBibtex(doi)
  const e = parseEntry(raw)
  if (!e) throw new Error('Couldn’t read the BibTeX that came back.')
  if (doi.startsWith('10.48550/')) { // arXiv: the eprint fields biblatex and natbib styles understand
    const id = doi.slice('10.48550/arXiv.'.length)
    e.fields = e.fields.filter(([k]) => k !== 'publisher' && k !== 'url')
    e.fields.push(['eprint', id], ['archiveprefix', 'arXiv'], ['url', `https://arxiv.org/abs/${id}`])
    if (!e.fields.some(([k]) => k === 'doi')) e.fields.push(['doi', doi])
  }
  const key = makeKey(e.fields, new Set(idx.bib.map((b) => b.key)))
  const entry = formatEntry(e.type, key, e.fields)
  r.doc.transact(() => {
    let yt = r.files.get(file!)
    if (!yt) { yt = new Y.Text(); r.files.set(file!, yt) }
    const t = yt.toString()
    yt.insert(yt.length, (t && !t.endsWith('\n\n') ? (t.endsWith('\n') ? '\n' : '\n\n') : '') + entry)
  })
  return { key, file: file!, existed: false, entry }
}

/** A small dialog: paste a DOI/arXiv id, get a key back (inserted by the caller). */
export function referenceDialog(r: Replica, index: ProjectIndex, onKey: (key: string) => void, seed = '') {
  const input = h('input', { placeholder: '10.1145/361604.361612 or arXiv:1706.03762', value: seed, 'aria-label': 'DOI or arXiv id', style: 'width: 100%' })
  const msg = h('p', { class: 'muted small' }, 'Paste a DOI, a doi.org or arXiv link, or an arXiv id. The entry is fetched from doi.org and added to your .bib file.')
  const pre = h('pre', { class: 'bibpreview', hidden: true })
  const target = h('div', { class: 'reftarget' })
  const go = h('button', { class: 'primary', onclick: () => run() }, 'Add reference')
  const dlg = dialog('refdlg', h('h3', {}, 'Add a reference'), input, target, msg, pre,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, 'Close'), go))
  const REMEMBER = `texloom.bibfile.${r.keys.project}`
  let plan = bibPlan(r, index)
  let select: HTMLSelectElement | null = null
  let addLine: HTMLInputElement | null = null
  const chosen = () => select?.value ?? plan.options[0]

  function render() {
    plan = bibPlan(r, index)
    let prev = select?.value
    try { prev ??= localStorage.getItem(REMEMBER) ?? undefined } catch {}
    const label = (f: string) => f + (plan.missing.has(f) ? ' (new file)' : '')
    const kids: (HTMLElement | string)[] = []
    if (plan.options.length > 1) {
      select = h('select', { 'aria-label': 'Bibliography file', onchange: () => { try { localStorage.setItem(REMEMBER, select!.value) } catch {}; lineText() } },
        ...plan.options.map((f) => h('option', { value: f }, label(f))))
      if (prev && plan.options.includes(prev)) select.value = prev
      kids.push(h('div', { class: 'row' }, h('span', { class: 'small' }, 'Add to'), select))
    } else {
      select = null
      kids.push(h('p', { class: 'small' }, plan.missing.has(plan.options[0]) ? `Creates ${plan.options[0]} for the entry.` : `Adds to ${plan.options[0]}.`))
    }
    addLine = null
    if (!plan.declared && plan.main) {
      addLine = h('input', { type: 'checkbox', checked: true })
      kids.push(h('label', { class: 'small check' }, addLine, h('span', { class: 'linetext' })))
    }
    target.replaceChildren(...kids)
    lineText()
  }
  function lineText() {
    const span = target.querySelector('.linetext')
    if (span) span.textContent = ` Also add ${bibliographyLine(plan, chosen())}${plan.biblatex ? ' and \\printbibliography' : ''} to ${plan.main}, so the list is printed`
  }

  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); run() } })
  render()
  input.focus()
  async function run() {
    go.setAttribute('disabled', ''); msg.className = 'muted small'; msg.textContent = 'Fetching…'
    try {
      const file = chosen()
      const a = await addReference(r, index, input.value, file)
      onKey(a.key)
      msg.textContent = a.existed ? `Already in ${a.file} as “${a.key}”; cited it.` : `Added to ${a.file} as “${a.key}”.`
      if (a.entry) { pre.textContent = a.entry; pre.hidden = false }
      if (!plan.declared) {
        if (addLine?.checked && addBibliography(r, plan, a.file)) msg.textContent += ` Added ${bibliographyLine(plan, a.file)} to ${plan.main}.`
        else msg.textContent += ` Add ${bibliographyLine(plan, a.file)} to your document to print it.`
      }
      input.value = ''
      render()
    } catch (e) {
      msg.className = 'err small'
      msg.textContent = String(e instanceof Error ? e.message : e).replace(/^(TypeError: )?(Failed to fetch|Load failed|NetworkError.*)$/, 'Couldn’t reach doi.org (offline?).')
    } finally { go.removeAttribute('disabled') }
  }
}
