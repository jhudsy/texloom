// The Dropbox screens: connecting an account (start page and project Settings), and linking the
// project to a Dropbox folder in project Settings, with what's happening. The work is in dropbox.ts.
import type { Replica } from './replica'
import { dialog, h } from './dom'
import { t, tn } from './i18n'
import { appHolds, approve, approvedPath, dropboxAccount, dropboxAppKey, folderPath, linkFolder, linkOf, pull, push, signIn, signInProblem, startDropbox, topFolders, unlink } from './dropbox'

const ago = (at: number) => new Date(at).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
const say = (e: unknown) => (e instanceof Error ? e.message : String(e))

let engine: ReturnType<typeof startDropbox> | null = null
const listeners = new Set<() => void>()

/** Start keeping the open project in step with its Dropbox folder, if it has one (and whenever it gets one). */
export function runDropbox(r: Replica, me: () => string) {
  engine = startDropbox(r, me, () => listeners.forEach((f) => f()))
  r.meta.observe((ev) => { if (ev.keysChanged.has('dropbox') || ev.keysChanged.has('dropboxLease')) listeners.forEach((f) => f()) })
}

/** Connected as …, or a button to connect. `changed` runs after either. Nothing if this server has no Dropbox app. */
export function dropboxAccountBox(changed: () => void = () => {}): HTMLElement {
  const box = h('div', { class: 'zotero-account dropbox-account' })
  const render = async () => {
    const a = dropboxAccount.get()
    if (a) {
      box.replaceChildren(h('div', { class: 'row' },
        h('span', { class: 'spacer small' }, t('Dropbox: connected as {{name}}.', { name: a.name || '?' })),
        h('button', { class: 'small', onclick: () => { dropboxAccount.set(null); render(); changed() } }, t('Disconnect'))))
      return
    }
    if (!(await dropboxAppKey())) { box.replaceChildren(h('p', { class: 'muted small' }, t('This server isn’t set up for Dropbox.'))); return }
    const msg = h('p', { class: 'err small', role: 'status' }, signInProblem())
    const go = h('button', { onclick: async () => {
      msg.textContent = ''
      go.setAttribute('disabled', '')
      try { await signIn() } // (goes to Dropbox, and comes back here)
      catch (e) { msg.textContent = say(e); go.removeAttribute('disabled') }
    } }, t('Connect Dropbox…'))
    box.replaceChildren(
      h('p', { class: 'muted small' }, t('Keep a project in step with a folder in your Dropbox, or one shared with you there. Connecting takes you to Dropbox to allow TeXLoom, then back here.')),
      h('div', { class: 'row' }, go),
      msg,
      h('p', { class: 'muted small' }, t('The connection stays in this browser. With a project list, it travels with the list, encrypted, so anyone with your sync link could use it.')))
  }
  render()
  return box
}

/** Project Settings: the link and what's happening, Sync now and Unlink; or linking one. */
export function dropboxSection(r: Replica, me: () => string, writable: boolean): HTMLElement {
  const box = h('div', { class: 'dropbox' })
  const msg = h('p', { class: 'small', role: 'status' })
  const note = (text: string, err = false) => { msg.className = err ? 'small err' : 'small muted'; msg.textContent = text }
  let shown = ''
  const render = () => {
    if (!box.isConnected && shown) { listeners.delete(render); return }
    const a = dropboxAccount.get(), l = linkOf(r), st = engine?.state, path = approvedPath(r, l)
    const now = JSON.stringify([!!a, l, path, st?.role, st?.holder, st?.error, st?.busy])
    if (now === shown) return
    shown = now
    if (!l) {
      box.replaceChildren(...[
        h('p', { class: 'muted small' }, t('Not linked to a Dropbox folder.')),
        writable && a ? h('div', { class: 'row' }, h('button', { onclick: () => linkDialog(r, me, render) }, t('Link a Dropbox folder…'))) : null,
        dropboxAccountBox(render),
      ].filter((x): x is HTMLElement => !!x))
      return
    }
    const app = appHolds(r, l)
    const where = h('div', {}, h('b', {}, path ?? l.path), h('span', { class: 'muted' }, ' · Dropbox'))
    const role = !a ? t('Connect your Dropbox account to sync it from this browser.')
      : !writable ? t('Read-only link: this browser doesn’t sync it.')
      : !path ? t('A collaborator linked this project to the Dropbox folder “{{name}}”. This browser doesn’t sync it until you approve, with where that folder is in your Dropbox: syncing uses your Dropbox connection.', { name: l.name })
      : st?.role === 'app' || app ? t('The TeXLoom app on {{name}} keeps this folder in step (Dropbox brings it there), so browsers don’t.', { name: st?.holder || app?.by || '?' })
      : st?.role === 'syncing' ? t('This browser keeps it in step: it brings changes in every minute, and sends changes once the project has been quiet for two minutes.')
      : st?.role === 'standby' ? t('{{name}} keeps it in step now (another browser). This browser takes over if they stop.', { name: st.holder ?? '?' })
      : t('Waiting to sync (offline, or listening for a TeXLoom app that keeps this folder).')
    const blocked = l.blocked ? h('div', { class: 'risk' },
      h('span', {}, h('b', {}, '⚠ '), t('The Dropbox folder has changes that clash with the project’s in {{files}}. Nothing was brought in. Make one side match the other, or keep the project’s version where they clash.', { files: (l.clash ?? []).join(', ') || '?' })),
      writable && a && path ? h('span', { class: 'actions' }, h('button', { onclick: async (e: Event) => {
        const b = e.currentTarget as HTMLButtonElement
        if (!confirm(t('Bring in the Dropbox folder’s changes, keeping the project’s version where the two clash? The project’s version then goes to Dropbox.'))) return
        b.disabled = true; note(t('Syncing…'))
        try { await pull(r, a, me(), true); await push(r, a, me()); note(t('Done.')) } catch (err) { note(say(err), true) } finally { b.disabled = false; render() }
      } }, t('Keep the project’s version'))) : null) : null
    box.replaceChildren(...[
      where,
      h('p', { class: 'muted small' }, role),
      h('p', { class: 'muted small' }, t('Last synced {{when}} by {{name}}.', { when: ago(l.at), name: l.by || '?' })),
      blocked,
      st?.error ? h('p', { class: 'small err' }, st.error) : null,
      msg,
      writable ? h('div', { class: 'row' },
        a && !path ? h('button', { class: 'small primary', onclick: () => approveDialog(r, render) }, t('Approve and sync…')) : null,
        a && path && st?.role !== 'standby' && st?.role !== 'app' ? h('button', { class: 'small', disabled: st?.busy, onclick: async () => {
          note(t('Syncing…'))
          try { await engine?.now(); note(engine?.state.error ?? (engine?.state.role === 'syncing' && !linkOf(r)?.blocked ? t('Done.') : '')) } catch (err) { note(say(err), true) }
          render()
        } }, t('Sync now')) : null,
        h('button', { class: 'small', onclick: () => {
          if (!confirm(t('Stop keeping this project in step with the Dropbox folder “{{name}}”? Both stay as they are.', { name: l.name }))) return
          unlink(r); render()
        } }, t('Unlink'))) : null,
      a ? null : dropboxAccountBox(render),
    ].filter((x): x is HTMLElement => !!x))
  }
  listeners.add(render)
  render()
  return box
}

/** A folder field, with this Dropbox's top folders to choose from. */
function folderField(value = ''): HTMLInputElement {
  const id = 'dbx-folders-' + Math.random().toString(36).slice(2)
  const input = h('input', { placeholder: '/Papers/my-paper', 'aria-label': t('Folder in your Dropbox'), spellcheck: 'false', list: id, value }) as HTMLInputElement
  const list = h('datalist', { id })
  const a = dropboxAccount.get()
  if (a) topFolders(a).then((fs) => list.replaceChildren(...fs.map((f) => h('option', { value: f + '/' })))).catch(() => {})
  queueMicrotask(() => input.after(list))
  return input
}

/** Choose the folder; combine the two; start syncing. */
function linkDialog(r: Replica, me: () => string, done: () => void) {
  const a = dropboxAccount.get()
  if (!a) return
  const folder = folderField()
  const many = [...r.files.keys(), ...r.bin.keys()].length > 1
  const prefer = (v: 'dropbox' | 'project', label: string) => h('label', { class: 'radio' }, h('input', { type: 'radio', name: 'dbx-prefer', value: v, checked: (v === 'project') === many }), label)
  const msg = h('p', { class: 'small', role: 'status' })
  const tell = (text: string, err = false) => { msg.className = err ? 'small err' : 'small muted'; msg.textContent = text }
  const go = h('button', { class: 'primary', onclick: () => run() }, t('Link'))
  const dlg = dialog('settings zlinkdlg',
    h('h3', {}, t('Link a Dropbox folder')),
    h('p', { class: 'muted small' }, t('In {{name}}’s Dropbox.', { name: a.name || '?' })),
    h('label', { class: 'field' }, t('Folder in your Dropbox'), folder),
    h('p', { class: 'muted small' }, t('A new folder is made if there isn’t one. Files only in the folder come into the project, and files only in the project go to the folder. Where both have a file and they differ, keep:')),
    prefer('dropbox', t('the Dropbox folder’s version')),
    prefer('project', t('the project’s version (it goes to Dropbox)')),
    h('p', { class: 'muted small' }, t('Everyone in the project sees the link. It syncs from the browser of whoever has connected Dropbox and has the folder, one at a time; collaborators the folder is shared with in Dropbox can sync it too. If the TeXLoom app on someone’s computer keeps the same folder in step, browsers leave it to the app.')),
    msg,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go))
  async function run() {
    const path = folderPath(folder.value)
    if (!path) { tell(t('Enter a folder in your Dropbox, such as /Papers/my-paper.'), true); return }
    go.setAttribute('disabled', ''); tell(t('Fetching…'))
    try {
      const keep = (dlg.querySelector('input[name=dbx-prefer]:checked') as HTMLInputElement | null)?.value === 'project' ? 'project' : 'dropbox'
      const got = await linkFolder(r, a!, me(), path, keep)
      tell(got ? tn(got, 'Linked; {{n}} file came in from Dropbox.', 'Linked; {{n}} files came in from Dropbox.') : t('Linked.'))
      dlg.close(); done()
      engine?.now()
    } catch (e) { tell(say(e), true) } finally { go.removeAttribute('disabled') }
  }
}

/** A collaborator's link: this browser syncs it once its user says where that folder is in their Dropbox. */
function approveDialog(r: Replica, done: () => void) {
  const l = linkOf(r), a = dropboxAccount.get()
  if (!l || !a) return
  const folder = folderField(l.path)
  const msg = h('p', { class: 'small', role: 'status' })
  const go = h('button', { class: 'primary', onclick: async () => {
    const path = folderPath(folder.value)
    if (!path) { msg.className = 'small err'; msg.textContent = t('Enter a folder in your Dropbox, such as /Papers/my-paper.'); return }
    go.setAttribute('disabled', ''); msg.className = 'small muted'; msg.textContent = t('Fetching…')
    try {
      const { folderIdOf } = await import('./dropbox')
      if ((await folderIdOf(a, path, false)) !== l.folder) throw new Error(t('{{path}} in your Dropbox isn’t the folder this project is linked to.', { path }))
      approve(r, l, path)
      dlg.close(); done(); engine?.now()
    } catch (e) { msg.className = 'small err'; msg.textContent = say(e) } finally { go.removeAttribute('disabled') }
  } }, t('Approve and sync'))
  const dlg = dialog('settings zlinkdlg',
    h('h3', {}, t('Sync the Dropbox folder “{{name}}”', { name: l.name })),
    h('p', { class: 'muted small' }, t('Syncing uses your Dropbox connection: it brings that folder’s files into the project and sends the project’s changes to it. Where is the folder in your Dropbox? (A folder shared with you can be somewhere else in yours than in theirs.)')),
    h('label', { class: 'field' }, t('Folder in your Dropbox'), folder),
    msg,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go))
}
