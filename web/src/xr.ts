// Documents that take labels from another document of the project (\externaldocument, from the
// xr, xr-hyper or zref-xr packages; xcite's \externalcitedocument for citations). xr reads the other document's .aux, which only exists once
// that document has been compiled, so a compile builds those documents first.
// Keep in sync with texloom/xr.py; scripts/xr-vectors.json checks both.
import { stripComments } from './texscan'

/** A project path joined and tidied ('a/../b' is 'b'); null if it leaves the project. */
function resolve(dir: string, name: string): string | null {
  if (!name || name.startsWith('/')) return null
  const out: string[] = []
  for (const seg of `${dir}/${name}`.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') { if (!out.length) return null; out.pop() } else out.push(seg)
  }
  return out.length ? out.join('/') : null
}

const dirOf = (p: string) => p.slice(0, Math.max(0, p.lastIndexOf('/')))
const isDocument = (files: Map<string, string>, p: string) => /\\documentclass\b/.test(stripComments(files.get(p) ?? ''))

/** `doc` and the files it reads with \input, \include or \subfile, as TeX finds them: relative to
 *  the directory `doc` is compiled in. */
function sources(files: Map<string, string>, doc: string): string[] {
  const dir = dirOf(doc), seen = new Set<string>([doc]), todo = [doc]
  while (todo.length) {
    const text = stripComments(files.get(todo.pop()!) ?? '')
    for (const m of text.matchAll(/\\(?:input|include|subfile)\s*\{([^}]{1,300})\}/g)) {
      const p = resolve(dir, m[1].trim())
      const hit = p && [p, p + '.tex'].find((q) => files.has(q))
      if (hit && !seen.has(hit)) { seen.add(hit); todo.push(hit) }
    }
  }
  return [...seen]
}

/** The documents `doc` takes labels from: where its compile reads each one's .aux, and that
 *  document's .tex. Only documents of the project (with \documentclass) are listed. */
export function externalDocuments(files: Map<string, string>, doc: string): { aux: string; tex: string }[] {
  const out = new Map<string, string>()
  for (const src of sources(files, doc)) {
    const text = stripComments(files.get(src) ?? '')
    for (const m of text.matchAll(/\\(?:z?externaldocument|externalcitedocument)\s*(?:\[[^\]]{0,200}\]\s*)?\{([^}]{1,300})\}/g)) {
      const base = resolve(dirOf(doc), m[1].trim())
      if (base && base + '.tex' !== doc && isDocument(files, base + '.tex')) out.set(base + '.aux', base + '.tex')
    }
  }
  return [...out].map(([aux, tex]) => ({ aux, tex }))
}

/** The documents to compile before `main`, in order, so that each finds the .aux files it reads.
 *  Documents that take labels from each other (main and a supplement, say) form a cycle: it is
 *  cut where it closes, so a document already on the way is not compiled again for another. */
export function compileOrder(files: Map<string, string>, main: string): string[] {
  const order: string[] = [], seen = new Set<string>([main])
  const visit = (doc: string) => {
    for (const { tex } of externalDocuments(files, doc)) {
      if (seen.has(tex)) continue
      seen.add(tex)
      visit(tex)
      order.push(tex)
    }
  }
  visit(main)
  return order
}
