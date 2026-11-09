// Starting points for new projects, and importing existing ones: a .zip (an Overleaf
// download, a GitHub archive, …) or an arXiv paper's source (fetched by the server).
import * as Y from 'yjs'
import { unzipSync } from 'fflate'
import type { Replica } from './replica'
import { dialog, h } from './dom'
import { cleanPath, confirmAdding, detectMain, isText, putBinary, putText } from './projectfiles'
import { latexTitle } from './projects'
import { t, tn } from './i18n'

export interface Template { id: string; name: string; info: string; main: string; files: Record<string, string> }

const BIB = `@book{knuth1984texbook,
  author    = {Knuth, Donald E.},
  title     = {The {\\TeX}book},
  publisher = {Addison-Wesley},
  year      = {1984}
}

@article{lamport1986latex,
  author  = {Lamport, Leslie},
  title   = {{\\LaTeX}: A Document Preparation System},
  journal = {Addison-Wesley},
  year    = {1986}
}
`

export const TEMPLATES: Template[] = [
  { id: 'blank', name: t('Blank article'), info: t('A minimal article.'), main: 'main.tex', files: {
    'main.tex': `\\documentclass{article}
\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}

\\title{Untitled}
\\author{}
\\date{\\today}

\\begin{document}
\\maketitle

Hello, world.

\\end{document}
` } },
  { id: 'paper', name: t('Paper with references'), info: t('Article with sections, figures, maths and a .bib file.'), main: 'main.tex', files: {
    'main.tex': `\\documentclass[11pt]{article}
\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}
\\usepackage{amsmath,amssymb}
\\usepackage{graphicx}
\\usepackage[margin=1in]{geometry}
\\usepackage{natbib}
\\usepackage{hyperref}

\\title{Title of the Paper}
\\author{First Author \\and Second Author}
\\date{}

\\begin{document}
\\maketitle

\\begin{abstract}
One paragraph summarising the problem, the approach and the results.
\\end{abstract}

\\section{Introduction}
\\label{sec:intro}
Typesetting with \\TeX{} \\citep{knuth1984texbook} and \\LaTeX{} \\citep{lamport1986latex}.
Section~\\ref{sec:method} describes the method.

\\section{Method}
\\label{sec:method}
An equation:
\\begin{equation}
  E = mc^2.
  \\label{eq:energy}
\\end{equation}

% Paste or drop an image into the editor to add a figure.

\\section{Conclusion}
What was shown, and what comes next.

\\bibliographystyle{plainnat}
\\bibliography{references}

\\end{document}
`, 'references.bib': BIB } },
  { id: 'beamer', name: t('Slides (Beamer)'), info: t('A presentation with title page and sections.'), main: 'main.tex', files: {
    'main.tex': `\\documentclass{beamer}
\\usetheme{default}
\\setbeamertemplate{navigation symbols}{}

\\title{Talk Title}
\\author{Speaker}
\\institute{Institution}
\\date{\\today}

\\begin{document}

\\begin{frame}
  \\titlepage
\\end{frame}

\\begin{frame}{Outline}
  \\tableofcontents
\\end{frame}

\\section{Motivation}
\\begin{frame}{Motivation}
  \\begin{itemize}
    \\item First point
    \\item<2-> Appears on the second click
  \\end{itemize}
\\end{frame}

\\section{Results}
\\begin{frame}{Results}
  \\begin{columns}
    \\column{0.5\\textwidth}
    Left column.
    \\column{0.5\\textwidth}
    Right column.
  \\end{columns}
\\end{frame}

\\end{document}
` } },
  { id: 'thesis', name: t('Report or thesis'), info: t('Chapters in separate files, included from main.tex.'), main: 'main.tex', files: {
    'main.tex': `\\documentclass[12pt,a4paper]{report}
\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}
\\usepackage{amsmath,amssymb}
\\usepackage{graphicx}
\\usepackage{natbib}
\\usepackage{hyperref}

\\title{Thesis Title}
\\author{Author Name}
\\date{\\today}

\\begin{document}
\\maketitle

\\begin{abstract}
The abstract.
\\end{abstract}

\\tableofcontents

\\include{chapters/introduction}
\\include{chapters/background}
\\include{chapters/conclusion}

\\bibliographystyle{plainnat}
\\bibliography{references}

\\end{document}
`,
    'chapters/introduction.tex': `\\chapter{Introduction}
\\label{ch:intro}
Start here. Chapter~\\ref{ch:background} covers related work.
`,
    'chapters/background.tex': `\\chapter{Background}
\\label{ch:background}
Prior work \\citep{knuth1984texbook}.
`,
    'chapters/conclusion.tex': `\\chapter{Conclusion}
\\label{ch:conclusion}
Summary and future work.
`,
    'references.bib': BIB } },
  { id: 'letter', name: t('Letter'), info: t('A formal letter.'), main: 'main.tex', files: {
    'main.tex': `\\documentclass[11pt]{letter}
\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}

\\signature{Your Name}
\\address{Your Street \\\\ Your Town \\\\ Postcode}

\\begin{document}
\\begin{letter}{Recipient Name \\\\ Their Street \\\\ Their Town}

\\opening{Dear Recipient,}

The body of the letter.

\\closing{Yours sincerely,}

\\end{letter}
\\end{document}
` } },
]

/** Fill an empty project from a template. */
export function applyTemplate(r: Replica, tpl: Template, by: string) {
  r.doc.transact(() => {
    for (const [p, text] of Object.entries(tpl.files)) if (!r.files.has(p)) r.files.set(p, new Y.Text(text))
    r.meta.set('main', tpl.main); r.meta.set('seeded', true)
    if (!r.meta.get('createdBy')) r.meta.set('createdBy', by)
  })
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------
const JUNK = /(^|\/)(__MACOSX|\.DS_Store|Thumbs\.db|\.git|\.svn|\.texloom)(\/|$)|(^|\/)\._/
// Like the folder-sync list, but keeps .bbl: arXiv sources often ship one instead of a .bib.
const BUILD = /\.(aux|log|out|toc|lof|lot|fls|fdb_latexmk|synctex\.gz|synctex|blg|bcf|run\.xml|nav|snm|vrb|xdv|dvi)$/i
const TEXTISH = /\.(tex|bib|bbl|sty|cls|bst|cfg|def|clo|ltx|txt|md|dtx|ins|tikz|pgf|csv|dat)$/i

/** Things a folder or archive holds that aren't part of a LaTeX project. */
export const isJunk = (path: string) => JUNK.test(path) || BUILD.test(path)

/** Unpack and add files to the project. Returns the number added and skipped. */
export async function importEntries(r: Replica, entries: Record<string, Uint8Array>, opts: { replaceAll?: boolean } = {}): Promise<{ added: number; skipped: string[] } | null> {
  let names = Object.keys(entries).filter((n) => !n.endsWith('/') && !JUNK.test(n))
  // a single top-level folder (GitHub "repo-main/", some Overleaf exports) is dropped
  const tops = new Set(names.map((n) => n.split('/')[0]))
  const strip = tops.size === 1 && names.every((n) => n.includes('/')) ? [...tops][0].length + 1 : 0
  const skipped: string[] = []
  const files: { path: string; data: Uint8Array }[] = []
  for (const n of names) {
    const path = cleanPath(n.slice(strip))
    if (!path || BUILD.test(path)) { skipped.push(n); continue }
    if (path.toLowerCase().endsWith('.pdf') && names.some((m) => m.slice(strip) === path.replace(/\.pdf$/i, '.tex'))) { skipped.push(n); continue } // compiled output
    files.push({ path, data: entries[n] })
  }
  // Replacing everything was asked for by ticking the box, so only the size is worth asking about
  if (!confirmAdding(r, files.map((f) => f.path), files.reduce((sum, f) => sum + f.data.length, 0), !opts.replaceAll)) return null
  const blobs = new Map<string, string>()
  const texts = new Map<string, string>()
  for (const f of files) {
    let text = isText(f.path, f.data)
    // older sources are often Latin-1 rather than UTF-8
    if (text === null && TEXTISH.test(f.path) && f.data.length < 4 * 1024 * 1024 && !f.data.subarray(0, 8192).includes(0)) text = new TextDecoder('windows-1252').decode(f.data)
    if (text !== null) texts.set(f.path, text)
    else blobs.set(f.path, await r.putBlob(f.data))
  }
  r.doc.transact(() => {
    if (opts.replaceAll) { for (const p of [...r.files.keys()]) r.files.delete(p); for (const p of [...r.bin.keys()]) r.bin.delete(p) }
    for (const [p, text] of texts) putText(r, p, text)
    for (const f of files) if (blobs.has(f.path)) putBinary(r, f.path, blobs.get(f.path)!, f.data.length)
    // pick the main file (the compiler's rule), unless one is chosen already
    const cur = r.meta.get('main')
    if (!cur || !r.files.has(cur)) {
      const main = detectMain(texts.keys(), (p) => texts.get(p) ?? '')
      if (main) r.meta.set('main', main)
    }
    // a .bbl without a .bib (typical of arXiv): use it as is
    if (![...r.files.keys()].some((p) => p.endsWith('.bib')) && [...r.files.keys()].some((p) => p.endsWith('.bbl'))) r.meta.set('bib', 'none')
    r.meta.set('seeded', true)
    // name an unnamed project after the document's title
    const main = r.meta.get('main'), name = r.meta.get('name')
    const title = main && texts.has(main) ? latexTitle(texts.get(main)!) : undefined
    if (title && (!name || name === 'Untitled project')) r.meta.set('name', title.slice(0, 120))
  })
  return { added: files.length, skipped }
}

export function arxivId(input: string): string | null {
  const m = /(?:arxiv\.org\/(?:abs|pdf|e-print)\/|arxiv:\s*|^)((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?\/\d{7})(?:v\d+)?)(?:\.pdf)?\/?$/i.exec(input.trim())
  return m ? m[1] : null
}

export async function fetchArxivZip(id: string): Promise<Uint8Array> {
  const res = await fetch(`/api/arxiv/${encodeURIComponent(id.replace('/', '_'))}`)
  if (!res.ok) throw new Error((await res.text().catch(() => '')) || t('The server answered {{status}}.', { status: res.status }))
  return new Uint8Array(await res.arrayBuffer())
}

/** The Import dialog: a .zip from this computer, or an arXiv paper. */
export function importDialog(r: Replica, opts: { onDone?: () => void; fresh?: boolean } = {}) {
  const msg = h('p', { class: 'muted small' })
  const fileIn = h('input', { type: 'file', accept: '.zip,application/zip', hidden: true })
  const arxivIn = h('input', { placeholder: t('arXiv id or link, e.g. 1706.03762'), 'aria-label': t('arXiv id or link') })
  const replace = h('input', { type: 'checkbox', 'aria-label': t('Replace current files') })
  const busy = (on: boolean) => dlg.querySelectorAll('button').forEach((b) => (on ? b.setAttribute('disabled', '') : b.removeAttribute('disabled')))
  const finish = (n: { added: number; skipped: string[] }, from: string) => {
    msg.className = 'small'
    msg.textContent = tn(n.added, 'Imported {{n}} file from {{from}}.', 'Imported {{n}} files from {{from}}.', { from }) +
      (n.skipped.length ? ' ' + tn(n.skipped.length, 'Skipped {{n}} (build files or unsupported names).', 'Skipped {{n}} (build files or unsupported names).') : '')
    opts.onDone?.()
  }
  const fail = (e: unknown) => { msg.className = 'err small'; msg.textContent = String(e instanceof Error ? e.message : e) }
  const nonEmpty = r.files.size + r.bin.size > 0 && !opts.fresh
  const dlg = dialog('settings importdlg',
    h('h3', {}, t('Import')),
    h('h5', {}, t('From a .zip file')),
    h('p', { class: 'muted small' }, t('For example an Overleaf download (Menu → Download → Source), a GitHub “Download ZIP”, or any folder you zipped.')),
    h('div', { class: 'row' }, h('button', { onclick: () => fileIn.click() }, t('Choose .zip…')), fileIn),
    h('h5', {}, t('From arXiv')),
    h('p', { class: 'muted small' }, t('The paper’s LaTeX source, fetched through this server.')),
    h('div', { class: 'row' }, arxivIn, h('button', { onclick: () => fromArxiv() }, t('Import'))),
    nonEmpty ? h('label', { class: 'check small' }, replace, t('Replace the project’s current files (they stay in History)')) : null,
    msg,
    h('p', { class: 'muted small' }, t('From git: clone the repository, then run “texloom new <folder>” in it, or open this project in a folder with “texloom run” and copy the files in.')),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Done'))))
  fileIn.addEventListener('change', async () => {
    const f = fileIn.files?.[0]
    fileIn.value = ''
    if (!f) return
    if (f.size > 200 * 1024 * 1024) return fail(t('That file is over 200 MB.'))
    busy(true); msg.className = 'muted small'; msg.textContent = t('Reading {{name}}…', { name: f.name })
    try {
      const entries = unzipSync(new Uint8Array(await f.arrayBuffer()))
      const added = await importEntries(r, entries, { replaceAll: replace.checked })
      if (added) finish(added, f.name); else msg.textContent = ''
    } catch (e) { fail(e instanceof Error && /invalid zip|unexpected EOF/i.test(e.message) ? t('That isn’t a readable .zip file.') : e) } finally { busy(false) }
  })
  async function fromArxiv() {
    const id = arxivId(arxivIn.value)
    if (!id) return fail(t('That doesn’t look like an arXiv id (e.g. 1706.03762 or hep-th/9901001).'))
    busy(true); msg.className = 'muted small'; msg.textContent = t('Fetching arXiv:{{id}}…', { id })
    try {
      const entries = unzipSync(await fetchArxivZip(id))
      const added = await importEntries(r, entries, { replaceAll: replace.checked })
      if (!added) { msg.textContent = ''; return }
      finish(added, `arXiv:${id}`)
      if (!r.meta.get('name') || r.meta.get('name') === 'Untitled project') r.meta.set('name', `arXiv ${id}`)
    } catch (e) { fail(e) } finally { busy(false) }
  }
  return dlg
}

/** New project: pick a template (or import). Resolves to the template id, 'import', or null. */
export function chooseTemplate(): Promise<string | null> {
  return new Promise((resolve) => {
    let choice: string | null = null
    const pick = (id: string) => { choice = id; dlg.close() }
    const dlg = dialog('settings templates',
      h('h3', {}, t('New project')),
      h('div', { class: 'tgrid' },
        ...TEMPLATES.map((tpl) => h('button', { class: 'radio choice', onclick: () => pick(tpl.id) }, h('span', {}, h('b', {}, tpl.name), h('small', { class: 'muted' }, tpl.info)))),
        h('button', { class: 'radio choice', onclick: () => pick('import') }, h('span', {}, h('b', {}, t('Import…')), h('small', { class: 'muted' }, t('From a .zip (e.g. Overleaf, GitHub) or an arXiv paper.'))))),
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel'))))
    dlg.addEventListener('close', () => resolve(choice))
  })
}
