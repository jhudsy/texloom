// Keep a folder on this computer in sync from the browser itself (Chrome, Edge and
// other Chromium browsers: the File System Access API), with nothing to install.
// Works while the tab is open; the texloom helper does the same without a browser.
import type { Replica } from './replica'
import { cleanPath, isText, mainFile, movePath, putBinary, putText } from './projectfiles'
import { applyText, merge3 } from './diff'
import { addedProgramLines, type ProgramLine } from './texscan'
import { tn, t, locale } from './i18n'

const DB = 'texloom-folders'
const SKIP_DIRS = new Set(['.texloom', '.git', '.svn', 'node_modules', '__pycache__', '.idea', '.vscode'])
// Build output and editor droppings: never synced. Same list as IGNORE_* in texloom/folder.py.
export const BUILD = /\.(aux|log|out|toc|lof|lot|fls|fdb_latexmk|synctex\.gz|synctex|bbl|blg|bcf|run\.xml|nav|snm|vrb|idx|ilg|ind|xdv|dvi|swp|swx|tmp)$|~$|(^|\/)\.#|(^|\/)\.DS_Store$|(^|\/)Thumbs\.db$/i
const MAX = 50 * 1024 * 1024

type Known = Record<string, { h: string; m: number; z: number; c?: boolean }> // c: the file on disk ends lines with \r\n

/** Whether text ends its lines with \r\n (saved on Windows): kept that way on disk, though the project has \n. */
function crlf(d: Uint8Array): boolean {
  const i = d.indexOf(10)
  return i > 0 && d[i - 1] === 13
}
type Dir = FileSystemDirectoryHandle

export const folderSyncSupported = () => 'showDirectoryPicker' in window

function db(): Promise<IDBDatabase> {
  return new Promise((res, rej) => {
    const q = indexedDB.open(DB, 1)
    q.onupgradeneeded = () => q.result.createObjectStore('kv')
    q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error)
  })
}
async function kv<T>(key: string, value?: T | null): Promise<T | undefined> {
  const d = await db()
  return new Promise((res, rej) => {
    const tx = d.transaction('kv', value === undefined ? 'readonly' : 'readwrite')
    const st = tx.objectStore('kv')
    const q = value === undefined ? st.get(key) : value === null ? st.delete(key) : st.put(value, key)
    q.onsuccess = () => res(value === undefined ? q.result : undefined); q.onerror = () => rej(q.error)
  })
}

async function sha(data: Uint8Array): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', data as BufferSource))
  return Array.from(d.subarray(0, 16), (b) => b.toString(16).padStart(2, '0')).join('')
}

async function walk(dir: Dir, prefix = '', out = new Map<string, FileSystemFileHandle>(), dirs?: Set<string>) {
  for await (const [name, handle] of (dir as any).entries() as AsyncIterable<[string, FileSystemHandle]>) {
    const rel = prefix + name
    if (handle.kind === 'directory') {
      if (SKIP_DIRS.has(name) || name.startsWith('.')) continue
      dirs?.add(rel)
      await walk(handle as Dir, rel + '/', out, dirs)
    }
    else if (!BUILD.test(rel) && !name.startsWith('.') ) out.set(rel, handle as FileSystemFileHandle)
  }
  return out
}

async function fileHandle(root: Dir, rel: string, create: boolean): Promise<FileSystemFileHandle | null> {
  const parts = rel.split('/')
  let d = root
  try {
    for (const p of parts.slice(0, -1)) d = await d.getDirectoryHandle(p, { create })
    return await d.getFileHandle(parts[parts.length - 1], { create })
  } catch { return null }
}

/** Files in a folder that sync would pick up (to warn before importing an unrelated folder). */
export async function folderFiles(dir: Dir): Promise<string[]> {
  return [...(await walk(dir)).keys()]
}

export interface FolderSync {
  name: string; stop(): void; status: () => string; onChange: (() => void) | null
  /** Lines that ask to run programs, newly written into the folder (from collaborators). */
  onPrograms: ((lines: ProgramLine[]) => void) | null
}

/** Start syncing `root` with the project. Disk wins for files changed on both sides. */
export async function startFolderSync(r: Replica, root: Dir): Promise<FolderSync> {
  const KEY = `known.${r.keys.project}`
  const DKEY = `dirs.known.${r.keys.project}`
  // What was in sync is remembered for the folder last linked. For any other folder, start
  // afresh: otherwise every project file missing from it would count as deleted on disk.
  const prev = await savedFolder(r.keys.project)
  const same = !!prev && (await (prev as any).isSameEntry(root).catch(() => false))
  const known: Known = (same && (await kv<Known>(KEY))) || {}
  const knownDirs = new Set<string>((same && (await kv<string[]>(DKEY))) || []) // folders mirrored both ways
  let busy = false, stopped = false, last = '', errors = 0
  // Text files: the text last synced with disk (the base of a three-way merge), and the hashes
  // of versions recently written or read here. An older one reappearing on disk is a replay
  // (an editor saving or reloading a stale copy), not an edit. Same rules as texloom/folder.py.
  const base = new Map<string, string>(), recent = new Map<string, string[]>()
  let rewrite = false
  const remember = (p: string, h: string) => {
    const seen = (recent.get(p) ?? []).filter((x) => x !== h)
    seen.push(h); recent.set(p, seen.slice(-30))
  }
  const stale = (p: string, h: string) => (recent.get(p) ?? []).slice(0, -1).includes(h)
  const save = () => Promise.all([kv(KEY, known), kv(DKEY, [...knownDirs])]).catch(() => {})
  const sync: FolderSync = { name: root.name, stop, status: () => last, onChange: null, onPrograms: null }
  const note = (s: string) => { last = s; sync.onChange?.() }

  async function writeOut() {
    let n = 0
    const want = new Set<string>(), programs: ProgramLine[] = []
    for (const [p, yt] of r.files) {
      if (!cleanPath(p) || BUILD.test(p)) continue
      want.add(p)
      const text = yt.toString(), data = new TextEncoder().encode(known[p]?.c ? text.replace(/\r?\n/g, '\r\n') : text)
      const h = await sha(data)
      base.set(p, text) // what disk holds, or will
      if (known[p]?.h === h) continue
      const prev = await readText(p)
      if (await put(p, data, h)) { n++; programs.push(...addedProgramLines(p, prev, text)) }
    }
    if (programs.length) sync.onPrograms?.(programs)
    for (const [p, b] of r.bin) {
      if (!cleanPath(p) || BUILD.test(p) || !b?.h) continue
      want.add(p)
      if (known[p]?.h === b.h) continue
      try { if (await put(p, await r.getBlob(b.h), b.h)) n++ } catch { /* not fetched yet: next round */ }
    }
    const dirs = new Set([...r.dirs.keys()])
    for (const d of dirs) { // explicit (possibly empty) folders
      if (!knownDirs.has(d) && cleanPath(d)) {
        try { let h = root; for (const x of d.split('/')) h = await h.getDirectoryHandle(x, { create: true }); knownDirs.add(d); n++ } catch {}
      }
    }
    for (const d of [...knownDirs].filter((x) => !dirs.has(x)).sort((a, b) => b.length - a.length)) { // deleted remotely
      knownDirs.delete(d); n++
      try {
        const parts = d.split('/')
        let h = root
        for (const x of parts.slice(0, -1)) h = await h.getDirectoryHandle(x)
        const sub = await h.getDirectoryHandle(parts[parts.length - 1])
        let empty = true
        for await (const _ of (sub as any).keys()) { empty = false; break }
        if (empty) await h.removeEntry(parts[parts.length - 1])
      } catch {}
    }
    for (const p of Object.keys(known)) {
      if (want.has(p)) continue
      const parts = p.split('/')
      try {
        let d = root
        for (const x of parts.slice(0, -1)) d = await d.getDirectoryHandle(x)
        await d.removeEntry(parts[parts.length - 1])
      } catch {}
      delete known[p]; n++
    }
    if (n) { await save(); note(tn(n, '↓ {{n}} file · {{time}}', '↓ {{n}} files · {{time}}', { time: new Date().toLocaleTimeString(locale) })) }
  }

  /** A file's current text in the folder, or null. */
  async function readText(p: string): Promise<string | null> {
    const fh = await fileHandle(root, p, false)
    try { return fh ? await (await fh.getFile()).text() : null } catch { return null }
  }

  async function put(p: string, data: Uint8Array, h: string): Promise<boolean> {
    const fh = await fileHandle(root, p, true)
    if (!fh) return false
    const w = await fh.createWritable()
    await w.write(data as BufferSource); await w.close()
    const f = await fh.getFile()
    known[p] = { h, m: f.lastModified, z: f.size, c: crlf(data) }
    remember(p, h)
    return true
  }

  async function ingest() {
    if (!r.keys.writable) return
    const diskDirs = new Set<string>()
    const disk = await walk(root, '', new Map(), diskDirs)
    let n = 0
    const addDirs = [...diskDirs].filter((d) => cleanPath(d) && !knownDirs.has(d) && !r.dirs.has(d))
    const goneDirs = [...knownDirs].filter((d) => !diskDirs.has(d))
    if (addDirs.length || goneDirs.length) {
      r.doc.transact(() => {
        for (const d of addDirs) r.dirs.set(d, true)
        for (const d of goneDirs) for (const k of [...r.dirs.keys()]) if (k === d || k.startsWith(d + '/')) r.dirs.delete(k)
      })
      for (const d of addDirs) knownDirs.add(d)
      for (const d of goneDirs) knownDirs.delete(d)
      n += addDirs.length + goneDirs.length
    }
    for (const d of diskDirs) if (r.dirs.has(d)) knownDirs.add(d)
    const main = mainFile(r)
    const ownPdf = main ? main.replace(/\.tex$/, '.pdf') : null // compiled on this computer: output, not source
    // Renamed or moved in the folder: a file gone from it whose last synced content is now in a
    // file the project doesn't have yet has moved there. It moves as renaming here does
    // (movePath), so comments and suggestions on it follow. Same rule as _moves() in texloom/folder.py.
    const gone = Object.keys(known).filter((p) => !disk.has(p))
    if (gone.length) {
      const sizes = new Set(gone.map((g) => known[g].z))
      const name = (x: string) => x.slice(x.lastIndexOf('/') + 1)
      for (const [p, fh] of disk) {
        if (!gone.length) break
        if (cleanPath(p) !== p || known[p] || r.files.has(p) || r.bin.has(p) || p === ownPdf) continue
        const f = await fh.getFile()
        if (!sizes.has(f.size)) continue
        const data = new Uint8Array(await f.arrayBuffer())
        const text = isText(p, data)
        const h = text !== null ? await sha(data) : await r.keys.blobId(data)
        const same = gone.filter((g) => known[g].h === h)
        if (!same.length) continue
        const from = same.find((g) => name(g) === name(p)) ?? same[0] // the same name first, as when a folder is renamed
        gone.splice(gone.indexOf(from), 1)
        movePath(r, from, p)
        if (base.has(from)) { base.set(p, base.get(from)!); base.delete(from) }
        delete known[from]
        known[p] = { h, m: f.lastModified, z: f.size, c: crlf(data) }; remember(p, h); n++
        if (text !== null && r.files.get(p)?.toString() !== text) rewrite = true // collaborators changed it since
      }
    }
    for (const [p, fh] of disk) {
      const path = cleanPath(p)
      if (!path || path === ownPdf) continue
      const f = await fh.getFile()
      const k = known[path]
      if (k && k.m === f.lastModified && k.z === f.size) continue
      if (f.size > MAX) continue
      const data = new Uint8Array(await f.arrayBuffer())
      const text = isText(path, data)
      const h = text !== null ? await sha(data) : await r.keys.blobId(data)
      if (k?.h === h) { known[path] = { h, m: f.lastModified, z: f.size, c: crlf(data) }; if (text !== null) base.set(path, text); continue }
      if (text !== null && stale(path, h)) {
        // an older version came back: not an edit (it would undo collaborators' changes)
        console.warn(`${path}: an older version reappeared in the folder; keeping the project's text`)
        delete known[path]; rewrite = true
        continue
      }
      if (text !== null) {
        r.doc.transact(() => {
          const yt = r.files.get(path)
          if (!yt || r.bin.has(path)) { putText(r, path, text); base.set(path, text); return }
          // only what changed on disk since it was last synced, on top of collaborators' changes
          const cur = yt.toString(), b = base.get(path)
          const merged = b !== undefined ? merge3(b, cur, text) : text
          applyText(yt, cur, merged)
          base.set(path, merged)
          if (merged !== text) rewrite = true // the file lacks collaborators' changes
        })
      } else { const id = await r.putBlob(data); r.doc.transact(() => putBinary(r, path, id, data.length)) }
      known[path] = { h, m: f.lastModified, z: f.size, c: crlf(data) }; remember(path, h); n++
    }
    for (const p of Object.keys(known)) {
      if (disk.has(p)) continue
      r.doc.transact(() => { r.files.delete(p); r.bin.delete(p) }) // deleted on disk
      delete known[p]; n++
    }
    if (n) { await save(); note(tn(n, '↑ {{n}} file · {{time}}', '↑ {{n}} files · {{time}}', { time: new Date().toLocaleTimeString(locale) })) }
    if (rewrite) { rewrite = false; await writeOut() }
  }

  async function round(dir: 'in' | 'out' | 'both') {
    if (busy || stopped) return
    busy = true
    try {
      // A texloom helper (the app, `texloom run`) keeps this folder in sync too: two syncs on
      // one folder each take the other's writes for edits, duplicating text. Leave it to the helper.
      if (await root.getDirectoryHandle('.texloom').then(() => true, () => false)) {
        note(t('stopped: the TeXLoom app (or texloom helper) already syncs this folder'))
        stop()
        return
      }
      if (dir !== 'out') await ingest()
      if (dir !== 'in') await writeOut()
      errors = 0
    } catch (e) {
      if (++errors >= 3) { note(t('stopped: {{error}}', { error: e })); stop() }
    } finally { busy = false }
  }

  let outTimer: ReturnType<typeof setTimeout> | undefined
  const onDoc = (_e: unknown, tr: { local: boolean }) => { clearTimeout(outTimer); outTimer = setTimeout(() => round('out'), tr?.local ? 1200 : 300) }
  r.files.observeDeep(onDoc as any); r.bin.observe(onDoc as any); r.dirs.observe(onDoc as any)
  const poll = setInterval(() => { if (document.visibilityState === 'visible') round('in') }, 2000)
  function stop() {
    stopped = true
    clearInterval(poll); clearTimeout(outTimer)
    r.files.unobserveDeep(onDoc as any); r.bin.unobserve(onDoc as any); r.dirs.unobserve(onDoc as any)
  }
  await round('both')
  if (!last) note(t('in sync'))
  return sync
}

// ---- remembering the folder across visits --------------------------------------------------
export async function savedFolder(project: string): Promise<Dir | undefined> {
  try { return await kv<Dir>(`dir.${project}`) } catch { return undefined }
}

export async function rememberFolder(project: string, dir: Dir | null) {
  await kv(`dir.${project}`, dir)
  if (!dir) { await kv(`known.${project}`, null); await kv(`dirs.known.${project}`, null) }
}

/** Permission to use a remembered folder: 'granted', or 'prompt' (needs a click). */
export async function folderPermission(dir: Dir, ask: boolean): Promise<PermissionState> {
  const opts = { mode: 'readwrite' } as const
  const h = dir as any
  let s: PermissionState = await h.queryPermission(opts)
  if (s !== 'granted' && ask) s = await h.requestPermission(opts)
  return s
}

export async function pickFolder(): Promise<Dir | null> {
  try { return await (window as any).showDirectoryPicker({ mode: 'readwrite', id: 'texloom' }) } catch { return null }
}
