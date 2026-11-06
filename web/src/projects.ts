// The list of projects opened in this browser (start page). Kept in localStorage only:
// the server can't read projects, so it can't list them for you.
import { forgetLocalCopy } from './replica'
import { b64d } from './crypto'

export interface Recent {
  project: string // unique id (from the link)
  fragment: string // link fragment with the keys
  name: string // shared project name
  at: number // last opened (ms)
  writable: boolean
  nick?: string // private label, only in this browser
  docTitle?: string // \title{…} of the main file
  people?: string[] // collaborators seen in this project, most recent first
  createdBy?: string
  files?: number
  storage?: 'server' | 'relay'
}

const KEY = 'texloom.recent'
const read = (): Recent[] => { try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] } }
const write = (list: Recent[]) => { try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, 200))) } catch {} }

/** Short, readable project code derived from its id, e.g. "K7P2-QX4M" (Crockford
 *  base32: no lookalike letters, no '-' or '_' from the base64 id). */
export function projectCode(id: string): string {
  const A = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  let bits = 0, n = 0, out = ''
  for (const byte of b64d(id).subarray(0, 5)) { // 40 bits -> 8 characters
    n = (n << 8) | byte; bits += 8
    while (bits >= 5) { out += A[(n >> (bits - 5)) & 31]; bits -= 5 }
  }
  return `${out.slice(0, 4)}-${out.slice(4, 8)}`
}

export const projects = {
  list(): Recent[] {
    return read().sort((a, b) => b.at - a.at)
  },

  /** Insert or merge. An edit link is never downgraded by later opening a read-only one. */
  update(project: string, patch: Partial<Recent>) {
    const list = read()
    let r = list.find((x) => x.project === project)
    if (!r) {
      if (!patch.fragment) return
      r = { project, fragment: patch.fragment, name: '', at: Date.now(), writable: !!patch.writable }
      list.push(r)
    }
    const keep = r.writable && patch.writable === false ? r.fragment : null // don't lose the edit link
    Object.assign(r, Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined)))
    if (keep) { r.writable = true; r.fragment = keep }
    write(list)
  },

  /** Remember collaborators' names seen in a project (excluding yourself). */
  sawPeople(project: string, names: string[], me: string) {
    const r = read().find((x) => x.project === project)
    if (!r) return
    const fresh = names.filter((n) => n && n !== me && !/^anon-\d+$/.test(n))
    if (!fresh.length) return
    const people = [...new Set([...fresh, ...(r.people ?? [])])].slice(0, 8)
    if (people.join('\n') !== (r.people ?? []).join('\n')) projects.update(project, { people })
  },

  async forget(project: string) {
    await forgetLocalCopy(project)
    write(read().filter((x) => x.project !== project))
    try { localStorage.removeItem(`texloom.device.${project}`) } catch {}
  },
}

/** Pull a plain-text title out of a LaTeX source, if it has \title{…}. */
export function latexTitle(src: string): string | undefined {
  const m = /\\title\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}/.exec(src)
  if (!m) return undefined
  const t = m[1]
    .replace(/\\\\/g, ' ')
    .replace(/\\[a-zA-Z]+\*?\s*/g, '')
    .replace(/[{}~]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return t ? t.slice(0, 120) : undefined
}

export function whenOpened(at: number): string {
  const d = new Date(at), now = new Date()
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day(now) - day(d)) / 86400000)
  if (diff === 0) return `today ${time}`
  if (diff === 1) return `yesterday ${time}`
  if (diff < 7) return d.toLocaleDateString([], { weekday: 'long' })
  return d.toLocaleDateString([], { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}
