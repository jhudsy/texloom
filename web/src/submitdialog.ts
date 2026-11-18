// The "Submission" download: a .zip for arXiv or a journal (what goes in: submission.ts).
import { zipSync, type Zippable } from 'fflate'
import { browserCompileAvailable, compileInBrowser, defaultEngine } from './compile'
import { dialog, h, saveFile } from './dom'
import { t, tn } from './i18n'
import { cleanPath, fileBase, mainFile, zipOpts } from './projectfiles'
import type { Replica } from './replica'
import { needsBbl, submission, usedFiles, type Files } from './submission'

const stemOf = (p: string) => p.replace(/\.[^./]+$/, '')

export function submissionDialog(r: Replica) {
  const main = mainFile(r)
  if (!main) { alert(t('No .tex file with \\documentclass found.')); return }
  // Texts as they are; binaries only by name until the download (their content may need fetching).
  const names = (): Files => {
    const f: Files = new Map()
    r.files.forEach((text, p) => f.set(p, text.toString()))
    for (const p of r.bin.keys()) f.set(p, new Uint8Array())
    return f
  }
  const check = (label: string, title: string, on: boolean) => {
    const input = h('input', { type: 'checkbox', checked: on, onchange: () => preview() }) as HTMLInputElement
    return { input, el: h('label', { class: 'check', title }, input, label) }
  }
  const strip = check(t('Remove comments'), t('arXiv publishes the source, comments included. Lines that are only a comment go; a comment after code leaves its %.'), true)
  const one = check(t('One .tex file'), t('Put the files read with \\input and \\include into the main file, for journals that want a single file. With BibTeX, the bibliography goes in too.'), false)
  const summary = h('div', { class: 'small' })
  const status = h('p', { class: 'muted small', role: 'status' })
  const go = h('button', { class: 'primary', onclick: () => run() }, t('Download .zip'))
  let busy = false
  const dlg = dialog('settings',
    h('h3', {}, t('Download for submission')),
    h('p', {}, t('A .zip for arXiv or a journal: the files {{main}} uses, with the compiled bibliography (.bbl) instead of the .bib. Build files and unused files are left out.', { main })),
    strip.el, one.el, summary, status,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go),
  )
  dlg.addEventListener('cancel', (e) => { if (busy) e.preventDefault() })

  const list = (paths: string[]) => h('ul', { class: 'small' }, ...paths.map((p) => h('li', {}, p)))
  function preview() {
    const files = names()
    const s = submission(files, main!, needsBbl(files, main!) ? '' : null, { stripComments: strip.input.checked, oneFile: one.input.checked })
    const inZip = [...s.files.keys()].sort()
    summary.replaceChildren(
      h('details', {}, h('summary', {}, tn(inZip.length, '{{n}} file goes in', '{{n}} files go in')), list(inZip)),
      s.leftOut.length ? h('details', {}, h('summary', {}, tn(s.leftOut.length, '{{n}} file is left out', '{{n}} files are left out')), list(s.leftOut)) : '')
  }
  preview()

  async function run() {
    if (busy) return
    busy = true
    go.setAttribute('disabled', '')
    try {
      const files = names()
      const missing: string[] = []
      status.textContent = t('Gathering the project’s files…')
      for (const p of usedFiles(files, main!)) {
        const b = r.bin.get(p)
        if (!b) continue
        try { files.set(p, await r.getBlob(b.h)) } catch { files.delete(p); missing.push(p) }
      }
      let bbl: string | null = null
      const notes: string[] = []
      if (needsBbl(files, main!)) {
        status.textContent = t('Compiling for the bibliography…')
        if (await browserCompileAvailable()) {
          try {
            const res = await compileInBrowser(files, main!, { engine: r.meta.get('engine') || defaultEngine(files), bib: r.meta.get('bib') || 'auto' })
            bbl = res.bbl
            if (!res.pdf && bbl) notes.push(t('The document has errors: check the bibliography in the .zip.'))
          } catch { /* falls back below */ }
        }
        if (bbl === null) {
          const own = files.get(stemOf(main!) + '.bbl')
          if (typeof own === 'string') { bbl = own; notes.push(t('The bibliography couldn’t be compiled, so the project’s own .bbl went in.')) }
          else notes.push(t('The bibliography couldn’t be compiled, so the .bib went in instead of a .bbl. arXiv may not build it.'))
        }
      }
      const s = submission(files, main!, bbl, { stripComments: strip.input.checked, oneFile: one.input.checked })
      const tree: Zippable = {}
      for (const [p, v] of s.files) {
        const path = cleanPath(p) // never "../x" or "/abs" entries
        if (!path) { missing.push(p); continue }
        tree[path] = [typeof v === 'string' ? new TextEncoder().encode(v) : v, zipOpts(path)]
      }
      saveFile(`${fileBase(r)}-submission.zip`, zipSync(tree), 'application/zip')
      if (missing.length) notes.push(t('These files weren’t included because their content isn’t available yet (offline, and no collaborator online has them):\n\n{{files}}', { files: missing.join('\n') }))
      if (notes.length) alert(notes.join('\n\n'))
      dlg.close()
    } catch (e) {
      status.textContent = t('Couldn’t make the .zip: {{error}}', { error: e })
    } finally {
      busy = false
      go.removeAttribute('disabled')
    }
  }
}
