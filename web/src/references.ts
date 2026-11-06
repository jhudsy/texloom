// Add a bibliography entry from a DOI or arXiv id: fetched as BibTeX from doi.org
// (arXiv papers have DOIs 10.48550/arXiv.<id>), given a readable key, and appended
// to the project's .bib file.
import * as Y from 'yjs'
import type { Replica } from './replica'
import type { ProjectIndex } from './latex'
import { dialog, h } from './dom'

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

/** Fetch and append to the project's bibliography (creating references.bib if there is none). */
export async function addReference(r: Replica, index: ProjectIndex, input: string): Promise<Added> {
  const doi = parseId(input)
  if (!doi) throw new Error('That doesn’t look like a DOI (10.xxxx/…) or an arXiv id (e.g. 1706.03762).')
  const idx = index.get()
  // already in the project?
  for (const f of idx.bibFiles) {
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
  const file = idx.bibFiles[0] ?? 'references.bib'
  r.doc.transact(() => {
    let yt = r.files.get(file)
    if (!yt) { yt = new Y.Text(); r.files.set(file, yt) }
    const t = yt.toString()
    yt.insert(yt.length, (t && !t.endsWith('\n\n') ? (t.endsWith('\n') ? '\n' : '\n\n') : '') + entry)
  })
  return { key, file, existed: false, entry }
}

/** A small dialog: paste a DOI/arXiv id, get a key back (inserted by the caller). */
export function referenceDialog(r: Replica, index: ProjectIndex, onKey: (key: string) => void, seed = '') {
  const input = h('input', { placeholder: '10.1145/361604.361612 or arXiv:1706.03762', value: seed, 'aria-label': 'DOI or arXiv id', style: 'width: 100%' })
  const msg = h('p', { class: 'muted small' }, 'Paste a DOI, a doi.org or arXiv link, or an arXiv id. The entry is fetched from doi.org and added to your .bib file.')
  const pre = h('pre', { class: 'bibpreview', hidden: true })
  const go = h('button', { class: 'primary', onclick: () => run() }, 'Add reference')
  const dlg = dialog('refdlg', h('h3', {}, 'Add a reference'), input, msg, pre,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, 'Close'), go))
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); run() } })
  input.focus()
  async function run() {
    go.setAttribute('disabled', ''); msg.className = 'muted small'; msg.textContent = 'Fetching…'
    try {
      const a = await addReference(r, index, input.value)
      onKey(a.key)
      msg.textContent = a.existed ? `Already in ${a.file} as “${a.key}”; cited it.` : `Added to ${a.file} as “${a.key}”.`
      if (a.entry) { pre.textContent = a.entry; pre.hidden = false }
      if (!index.get().bibFiles.length || (!a.existed && a.file === 'references.bib' && !mentionsBib(r))) {
        msg.textContent += ' Add \\bibliography{references} (or \\addbibresource{references.bib}) to your document to print it.'
      }
      input.value = ''
    } catch (e) {
      msg.className = 'err small'
      msg.textContent = String(e instanceof Error ? e.message : e).replace(/^(TypeError: )?(Failed to fetch|Load failed|NetworkError.*)$/, 'Couldn’t reach doi.org (offline?).')
    } finally { go.removeAttribute('disabled') }
  }
}

function mentionsBib(r: Replica): boolean {
  for (const [p, yt] of r.files) if (p.endsWith('.tex') && /\\(bibliography|addbibresource)\s*\{/.test(yt.toString())) return true
  return false
}
