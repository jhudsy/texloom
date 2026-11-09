// Your own tags on projects, kept next to your list of projects (texloom.tags here, and an
// encrypted entry on the server if the list is synced: listsync.ts). They are never written
// into a project, so collaborators don't see them.
import { locale } from './i18n'
import { canon, emptyTags, foldTag, mergeTags, type TagDoc } from './listcore'

export interface Tag { id: string; name: string }
export const MAX_TAG_NAME = 60

const KEY = 'texloom.tags'
const read = (): TagDoc => { try { return { ...emptyTags(), ...JSON.parse(localStorage.getItem(KEY) || '{}') } } catch { return emptyTags() } }
let changed: () => void = () => {}
const write = (doc: TagDoc, quietly = false) => {
  try { localStorage.setItem(KEY, JSON.stringify(doc)) } catch {}
  if (!quietly) changed()
}
/** Later than `prev` even if this device's clock is behind, so a change always takes effect. */
const stamp = (prev = 0) => Math.max(Date.now(), prev + 1)
const newId = () => {
  const out = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(6))))
  return out.replace(/\+/g, '-').replace(/\//g, '_')
}
const live = (doc: TagDoc): Tag[] => Object.entries(doc.tags).filter(([, x]) => !x.gone).map(([id, x]) => ({ id, name: x.n }))
  .sort((a, b) => a.name.localeCompare(b.name, locale))

export const cleanTagName = (name: string) => name.normalize('NFC').replace(/\s+/g, ' ').trim().slice(0, MAX_TAG_NAME)

export const tags = {
  onChange(f: () => void) { changed = f },

  all: (): Tag[] => live(read()),

  /** The tag with this name, however it is capitalised. */
  find(name: string): Tag | undefined {
    const want = foldTag(name)
    return live(read()).find((x) => foldTag(x.name) === want)
  },

  of: (project: string): Tag[] => tags.reader()(project),

  /** For looking up many projects: reads the stored tags once. */
  reader(): (project: string) => Tag[] {
    const doc = read(), all = live(doc)
    return (project) => { const marks = doc.on[project] ?? {}; return all.filter((x) => marks[x.id] > 0) }
  },

  /** How many projects have this tag. */
  uses: (id: string): number => Object.values(read().on).filter((marks) => marks[id] > 0).length,

  create(name: string): Tag | null {
    const clean = cleanTagName(name)
    if (!clean) return null
    const had = tags.find(clean)
    if (had) return had
    const doc = read(), id = newId()
    doc.tags[id] = { n: clean, at: stamp() }
    write(doc)
    return { id, name: clean }
  },

  /** Rename a tag. To the name of another tag, it becomes that tag, keeping that tag's spelling. */
  rename(id: string, name: string) {
    const clean = cleanTagName(name), doc = read(), other = tags.find(clean)
    if (!clean || !doc.tags[id] || doc.tags[id].gone) return
    doc.tags[id] = { n: other && other.id !== id ? other.name : clean, at: stamp(doc.tags[id].at) }
    write(mergeTags(doc, doc))
  },

  remove(id: string) {
    const doc = read()
    if (!doc.tags[id]) return
    doc.tags[id] = { n: '', at: stamp(doc.tags[id].at), gone: true }
    for (const marks of Object.values(doc.on)) delete marks[id]
    write(doc)
  },

  /** Assign a tag to these projects, or take it off them. */
  set(projects: string[], id: string, on: boolean) {
    const doc = read()
    for (const project of projects) {
      const marks = (doc.on[project] ??= {})
      if ((marks[id] > 0) === on) continue
      const when = stamp(Math.abs(marks[id] ?? 0))
      marks[id] = on ? when : -when
    }
    write(doc)
  },

  /** A project that left the list takes its tags with it. */
  forgetProject(project: string) {
    const doc = read()
    if (!doc.on[project]) return
    delete doc.on[project]
    write(doc)
  },

  doc: read,

  /** Take on the merged tags. True if anything here changed. */
  apply(doc: TagDoc): boolean {
    const different = canon(read()) !== canon(doc)
    if (different) write(doc, true)
    return different
  },
}
