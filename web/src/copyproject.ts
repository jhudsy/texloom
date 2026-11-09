// Make a new project from an existing one: its current files and settings, as a starting point
// that owes nothing to the original. No history, comments, collaborators or links come along.
import * as Y from 'yjs'
import { Keys } from './crypto'
import { dialog, h } from './dom'
import { t, tn } from './i18n'
import { cleanPath } from './projectfiles'
import { projects } from './projects'
import { Replica } from './replica'
import { chooseNewProject } from './storagenotice'

const SETTINGS = ['main', 'bib', 'engine', 'lang'] // what else in the shared document a copy keeps
const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms))
const hasFiles = (r: Replica) => r.files.size > 0 || r.bin.size > 0

/** Fill the empty `to` from `from`. Returns the files that couldn't be copied. */
async function fill(from: Replica, to: Replica, name: string, by: string, progress: (done: number, total: number) => void): Promise<string[]> {
  const missing: string[] = []
  const binaries = [...from.bin.entries()]
  const stored: [string, { h: string; size: number }][] = []
  for (const [i, [p, b]] of binaries.entries()) {
    progress(i, binaries.length)
    const path = cleanPath(p) // never "../x", whatever a collaborator named it
    if (!path) { missing.push(p); continue }
    try {
      // encrypted with this project's key, so the content has to be read and stored again
      const data = await from.getBlob(b.h)
      stored.push([path, { h: await to.putBlob(data), size: data.length }])
    } catch { missing.push(p) }
  }
  to.doc.transact(() => {
    from.files.forEach((text, p) => {
      const path = cleanPath(p)
      if (path) to.files.set(path, new Y.Text(text.toString()))
      else missing.push(p)
    })
    for (const [path, b] of stored) to.bin.set(path, b)
    for (const d of from.dirs.keys()) { const path = cleanPath(d); if (path) to.dirs.set(path, true) }
    for (const k of SETTINGS) if (from.meta.has(k)) to.meta.set(k, from.meta.get(k))
    to.meta.set('name', name)
    to.meta.set('seeded', true)
    if (by) to.meta.set('createdBy', by)
  })
  return missing
}

/** A project that may not be on this device yet: bring it here, as far as that's possible now. */
async function fetchSource(keys: Keys, server: string): Promise<Replica> {
  const from = new Replica(keys, server, 'live')
  await from.open()
  await from.caughtUp(10_000)
  if (!hasFiles(from)) await sleep(3000) // with no server copy, its content comes from people online
  return from
}

/** Ask for a name, make the copy and open it. Give `replica` when the project is already open. */
export function copyDialog(source: { name: string; by: string; server: string } & ({ replica: Replica } | { keys: Keys })) {
  const nameIn = h('input', { value: t('Copy of {{name}}', { name: source.name }), 'aria-label': t('Name of the copy') })
  const status = h('p', { class: 'muted small', role: 'status' })
  const cancel = h('button', { onclick: () => dlg.close() }, t('Cancel'))
  const go = h('button', { class: 'primary', onclick: () => run() }, t('Make a copy'))
  let busy = false
  const dlg = dialog('settings',
    h('h3', {}, t('Make a copy')),
    h('p', {}, t('The copy is a new project of your own, with its own links. It starts with the current files and settings, without the history, comments or collaborators, and it counts towards your storage.')),
    h('label', {}, t('Name'), nameIn),
    status,
    h('div', { class: 'row end' }, cancel, go),
  )
  dlg.addEventListener('cancel', (e) => { if (busy) e.preventDefault() })
  nameIn.select()
  nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); run() } })

  async function run() {
    if (busy) return
    const name = nameIn.value.trim() || source.name
    const where = await chooseNewProject()
    if (!where) return
    busy = true
    go.disabled = cancel.disabled = nameIn.disabled = true
    status.textContent = t('Copying…')
    let from: Replica | null = null
    try {
      from = 'replica' in source ? source.replica : await fetchSource(source.keys, source.server)
      if (!hasFiles(from)) throw new Error(t('its files aren’t available right now. If it has no server copy, someone who has it needs to be online.'))
      // Not connected: the copy goes to the server when it is opened, with the storage chosen for it.
      const keys = Keys.generate()
      const to = new Replica(keys, source.server, 'manual')
      await to.open()
      const missing = await fill(from, to, name, source.by, (done, total) => { status.textContent = t('Copying… ({{done}} of {{total}})', { done: done + 1, total }) })
      await to.close()
      const storage = where === 'relay' || from.storage === 'relay' ? 'relay' : 'server'
      try { if (storage === 'relay') localStorage.setItem(`texloom.newRelay.${keys.project}`, '1') } catch {}
      projects.update(keys.project, { fragment: keys.fragment(), writable: true, at: Date.now(), name, storage, createdBy: source.by || undefined })
      if (missing.length) alert(tn(missing.length, 'The copy is missing {{n}} file whose content isn’t available right now: {{files}}', 'The copy is missing {{n}} files whose content isn’t available right now: {{files}}', { files: missing.slice(0, 5).join(', ') }))
      location.hash = keys.fragment()
    } catch (e) {
      status.textContent = t('Couldn’t copy the project: {{err}}', { err: (e as Error).message })
      busy = false
      go.disabled = cancel.disabled = nameIn.disabled = false
    } finally {
      if (from && !('replica' in source)) await from.close().catch(() => {})
    }
  }
}
