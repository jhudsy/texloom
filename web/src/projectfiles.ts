// Project file and folder operations on the shared Yjs document, plus zip export.
// Folders are implied by file paths ("sections/intro.tex"); the `dirs` map records
// explicit folders so that empty ones exist too.
import * as Y from 'yjs'
import { zipSync, type Zippable } from 'fflate'
import type { Replica } from './replica'
import { setText } from './diff'

export const IMAGE_RE = /\.(png|jpe?g|gif|svg|webp|pdf|eps|ps|bmp|tiff?)$/i

// Keep in sync with safe_rel() in texloom/folder.py.
// Files TeX tools read as configuration or run (latexmk runs latexmkrc as Perl), and folders
// whose contents tools act on (.git hooks/config, editor settings): never part of a project,
// whatever a collaborator or an imported archive puts there.
const UNSAFE_NAMES = new Set(['latexmkrc', '.latexmkrc', 'texmf.cnf', '.texmf.cnf', 'mktexlsr', 'ls-r'])
const PROTECTED_DIRS = new Set(['.texloom', '.git', '.vscode', '.idea'])

/** Normalise and validate a project-relative path; returns null if unacceptable. Every path
 *  that reaches a disk (folder sync, import) passes through here. */
export function cleanPath(input: string): string | null {
  const p = input.trim().replace(/\\/g, '/').replace(/\/+/g, '/').replace(/^\.?\//, '').replace(/\/$/, '')
  if (!p || p.length > 300) return null
  for (const seg of p.split('/')) {
    if (!seg || seg === '.' || seg === '..' || /[\x00-\x1f<>:"|?*]/.test(seg)) return null
    // Windows drops trailing dots and spaces (".git." is ".git") and has 8.3 aliases ("GIT~1")
    if (/[. ]$/.test(seg) || /~\d/.test(seg)) return null
    // compared the way case-insensitive file systems (macOS, Windows) see names: ".GIT" is ".git"
    const n = seg.normalize('NFKC').toLowerCase()
    if (PROTECTED_DIRS.has(n) || UNSAFE_NAMES.has(n) || n.endsWith('.latexmkrc')) return null
  }
  return p
}

export function isText(path: string, data: Uint8Array): string | null {
  if (data.length > 4 * 1024 * 1024 || data.subarray(0, 8192).includes(0)) return null
  if (/\.(png|jpe?g|gif|pdf|eps|webp|zip|gz|ttf|otf|woff2?|bmp|tiff?)$/i.test(path)) return null
  try { return new TextDecoder('utf-8', { fatal: true }).decode(data) } catch { return null }
}

/** The document to compile when none is chosen: main.tex, else a file with \documentclass,
 *  top-level folders first. Same rule as main_file() in texloom/paths.py. */
export function detectMain(paths: Iterable<string>, textOf: (p: string) => string): string | null {
  const depth = (p: string) => p.split('/').length
  const tex = [...paths].filter((p) => p.endsWith('.tex') && cleanPath(p) === p)
    .sort((a, b) => Number(a !== 'main.tex') - Number(b !== 'main.tex') || depth(a) - depth(b) || (a < b ? -1 : a > b ? 1 : 0))
  return tex.find((p) => textOf(p).includes('\\documentclass')) ?? null
}

/** The file compiled as the document: the chosen one, else detectMain(). */
export function mainFile(r: Replica): string | null {
  const m = r.meta.get('main')
  if (typeof m === 'string' && r.files.has(m) && cleanPath(m) === m) return m
  return detectMain(r.files.keys(), (p) => r.files.get(p)?.toString() ?? '')
}

/** Put text at `path` (replacing a binary file there). Existing text changes by a minimal diff,
 *  so collaborators' concurrent edits and cursors survive. Batch calls inside r.doc.transact. */
export function putText(r: Replica, path: string, text: string) {
  r.bin.delete(path)
  const yt = r.files.get(path)
  if (!yt) r.files.set(path, new Y.Text(text))
  else if (yt.toString() !== text) setText(yt, text)
}

/** Point `path` at stored file content (replacing text there). */
export function putBinary(r: Replica, path: string, id: string, size: number) {
  r.files.delete(path)
  r.bin.set(path, { h: id, size })
}

/** Add or replace a file from its bytes: text if it decodes as such, else stored as a binary file. */
export async function putFile(r: Replica, path: string, data: Uint8Array) {
  const text = isText(path, data)
  if (text !== null) { r.doc.transact(() => putText(r, path, text)); return }
  const id = await r.putBlob(data)
  r.doc.transact(() => putBinary(r, path, id, data.length))
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
