// Project file and folder operations on the shared Yjs document, plus zip export.
// Folders are implied by file paths ("sections/intro.tex"); the `dirs` map records
// explicit folders so that empty ones exist too.
import * as Y from 'yjs'
import { zipSync, type Zippable } from 'fflate'
import type { Replica } from './replica'

export const IMAGE_RE = /\.(png|jpe?g|gif|svg|webp|pdf|eps|ps|bmp|tiff?)$/i

/** Normalise and validate a project-relative path; returns null if unacceptable. */
export function cleanPath(input: string): string | null {
  const p = input.trim().replace(/\\/g, '/').replace(/\/+/g, '/').replace(/^\.?\//, '').replace(/\/$/, '')
  if (!p || p.length > 300) return null
  const parts = p.split('/')
  if (parts.some((s) => !s || s === '.' || s === '..' || /[\x00-\x1f<>:"|?*]/.test(s))) return null
  if (parts[0].toLowerCase() === '.texloom' || parts[0] === '.git') return null
  return p
}

export function isText(path: string, data: Uint8Array): string | null {
  if (data.length > 4 * 1024 * 1024 || data.subarray(0, 8192).includes(0)) return null
  if (/\.(png|jpe?g|gif|pdf|eps|webp|zip|gz|ttf|otf|woff2?|bmp|tiff?)$/i.test(path)) return null
  try { return new TextDecoder('utf-8', { fatal: true }).decode(data) } catch { return null }
}

/** The file compiled as the document: the chosen one, else main.tex, else any file with \documentclass. */
export function mainFile(r: Replica): string | null {
  const m = r.meta.get('main')
  if (m && r.files.has(m)) return m as string
  const tex = [...r.files.keys()].filter((p) => p.endsWith('.tex')).sort((a, b) => Number(b === 'main.tex') - Number(a === 'main.tex') || a.localeCompare(b))
  return tex.find((p) => r.files.get(p)!.toString().includes('\\documentclass')) ?? tex[0] ?? null
}

export const allFiles = (r: Replica) => [...new Set([...r.files.keys(), ...r.bin.keys()])].sort()

/** Every folder: explicit ones plus the parents of every file. */
export function allFolders(r: Replica): string[] {
  const out = new Set<string>(r.dirs.keys())
  for (const f of allFiles(r)) {
    const parts = f.split('/')
    for (let i = 1; i < parts.length; i++) out.add(parts.slice(0, i).join('/'))
  }
  return [...out].sort()
}

export const exists = (r: Replica, p: string) => r.files.has(p) || r.bin.has(p) || allFolders(r).includes(p)
const under = (p: string, folder: string) => p.startsWith(folder + '/')

export function newFolder(r: Replica, path: string) {
  r.dirs.set(path, true)
}

/** Move/rename a file or a folder (with everything in it). */
export function movePath(r: Replica, from: string, to: string) {
  r.doc.transact(() => {
    const moveFile = (a: string, b: string) => {
      const t = r.files.get(a)
      if (t) { r.files.set(b, new Y.Text(t.toString())); r.files.delete(a) }
      const blob = r.bin.get(a)
      if (blob) { r.bin.set(b, blob); r.bin.delete(a) }
      if (r.meta.get('main') === a) r.meta.set('main', b)
    }
    if (r.files.has(from) || r.bin.has(from)) { moveFile(from, to); return }
    for (const f of allFiles(r)) if (under(f, from)) moveFile(f, to + f.slice(from.length))
    for (const d of [...r.dirs.keys()]) {
      if (d === from || under(d, from)) { r.dirs.delete(d); r.dirs.set(to + d.slice(from.length), true) }
    }
    r.dirs.set(to, true)
  })
}

/** Delete a file, or a folder and everything in it. Returns how many files were removed. */
export function deletePath(r: Replica, path: string): number {
  let n = 0
  r.doc.transact(() => {
    for (const f of allFiles(r)) {
      if (f === path || under(f, path)) { r.files.delete(f); r.bin.delete(f); n++ }
    }
    for (const d of [...r.dirs.keys()]) if (d === path || under(d, path)) r.dirs.delete(d)
  })
  return n
}

/** Zip of the whole project. Files whose content isn't available (offline, no peer) are listed in `missing`. */
export async function projectZip(r: Replica): Promise<{ zip: Uint8Array; missing: string[] }> {
  const enc = new TextEncoder()
  const tree: Zippable = {}
  const missing: string[] = []
  const put = (path: string, data: Uint8Array) => {
    const p = cleanPath(path) // never "../x" or "/abs" entries, whatever a collaborator named it
    if (p) tree[p] = [data, { level: /\.(png|jpe?g|gif|pdf|zip|gz)$/i.test(p) ? 0 : 6 }]
    else missing.push(path)
  }
  r.files.forEach((t, p) => put(p, enc.encode(t.toString())))
  for (const [p, b] of r.bin.entries()) {
    try { put(p, await r.getBlob(b.h)) } catch { missing.push(p) }
  }
  for (const d of r.dirs.keys()) {
    const p = cleanPath(d)
    if (p && !Object.keys(tree).some((k) => k.startsWith(p + '/'))) tree[p + '/'] = new Uint8Array()
  }
  return { zip: zipSync(tree), missing }
}

/** A filesystem-friendly version of the project name. */
export const fileBase = (r: Replica) =>
  (String(r.meta.get('name') || 'project').normalize('NFKD').replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-') || 'project').slice(0, 60)
