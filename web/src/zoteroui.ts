// The Zotero screens: connecting an account (start page and project Settings), and linking a
// collection to a .bib file in project Settings. The work itself is in zotero.ts.
import type { Replica } from './replica'
import { dialog, h } from './dom'
import { t } from './i18n'
import { collections, connect, libraries, links, refresh, setLink, zoteroAccount, type Collection, type Library, type Link } from './zotero'

const NEW_KEY = 'https://www.zotero.org/settings/keys/new'
const ago = (at: number) => new Date(at).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })

/** Connected as …, or a box to connect with an API key. `changed` runs after either. */
export function zoteroAccountBox(changed: () => void = () => {}): HTMLElement {
  const box = h('div', { class: 'zotero-account' })
  const render = () => {
    const a = zoteroAccount.get()
    if (a) {
      box.replaceChildren(h('div', { class: 'row' },
        h('span', { class: 'spacer small' }, t('Connected as {{name}}.', { name: a.username || `#${a.userID}` })),
        h('button', { class: 'small', onclick: () => { zoteroAccount.set(null); render(); changed() } }, t('Disconnect'))))
      return
    }
    const key = h('input', { placeholder: t('Zotero API key'), 'aria-label': t('Zotero API key'), autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement
    const msg = h('p', { class: 'err small', role: 'status' })
    const go = h('button', { onclick: async () => {
      msg.textContent = ''
      go.setAttribute('disabled', '')
      try { zoteroAccount.set(await connect(key.value)); render(); changed() }
      catch (e) { msg.textContent = e instanceof Error ? e.message : String(e) }
      finally { go.removeAttribute('disabled') }
    } }, t('Connect'))
    key.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click() })
    box.replaceChildren(
      h('p', { class: 'muted small' }, t('Keep a .bib file in step with a Zotero collection. Make a key with read access to your library (and to your groups, if you use them), then paste it here.'), ' ',
        h('a', { href: NEW_KEY, target: '_blank', rel: 'noopener' }, t('Make a key at zotero.org'))),
      h('div', { class: 'row' }, key, go),
      msg,
      h('p', { class: 'muted small' }, t('The key stays in this browser. With a project list, it travels with the list, encrypted, so anyone with your sync link could use it.')))
  }
  render()
  return box
}

/** Project Settings: the account if needed, then the project's links with Refresh and Unlink. */
export function zoteroSection(r: Replica, me: () => string, writable: boolean): HTMLElement {
  const box = h('div', { class: 'zotero' })
  const render = () => {
    const mine = zoteroAccount.get()
    const linked = Object.entries(links(r)).sort(([a], [b]) => a.localeCompare(b))
    const rows = linked.map(([file, l]) => {
      const msg = h('span', { class: 'small', role: 'status' })
      return h('div', { class: 'zlink' },
        h('div', {}, h('code', {}, file), ' ← ', h('b', {}, l.collName), h('span', { class: 'muted' }, ` (${l.libName}, ${l.format === 'biblatex' ? 'BibLaTeX' : 'BibTeX'})`)),
        h('div', { class: 'row' },
          h('span', { class: 'muted small spacer' }, t('Refreshed {{when}} by {{name}}.', { when: ago(l.at), name: l.by || '?' })),
          msg,
          writable && mine ? h('button', { class: 'small', onclick: async (e: Event) => {
            const b = e.currentTarget as HTMLButtonElement
            b.disabled = true; msg.className = 'small muted'; msg.textContent = t('Fetching…')
            try { msg.textContent = (await refresh(r, file, me())) ? t('Updated.') : t('Already up to date.'); render() }
            catch (err) { msg.className = 'small err'; msg.textContent = err instanceof Error ? err.message : String(err) }
            finally { b.disabled = false }
          } }, t('Refresh')) : null,
          writable ? h('button', { class: 'small', onclick: () => { if (confirm(t('Stop keeping {{file}} in step with Zotero? The file stays as it is.', { file }))) { setLink(r, file, null); render() } } }, t('Unlink')) : null))
    })
    const parts: (HTMLElement | null)[] = [
      ...rows,
      linked.length ? h('p', { class: 'muted small' }, t('A linked file is replaced with the collection’s entries on each refresh: change entries in Zotero, not in the file.')) : null,
      mine ? null : zoteroAccountBox(render),
      writable && mine ? h('div', { class: 'row' }, h('button', { onclick: () => linkDialog(r, me, render) }, t('Link a collection…'))) : null,
      !linked.length && !writable ? h('p', { class: 'muted small' }, t('No files here are kept in step with Zotero.')) : null,
    ]
    box.replaceChildren(...parts.filter((x): x is HTMLElement => !!x))
  }
  render()
  return box
}

/** Choose a library, a collection (or all of it), the format and the file; then fetch it. */
function linkDialog(r: Replica, me: () => string, done: () => void) {
  const a = zoteroAccount.get()
  if (!a) return
  const lib = h('select', { 'aria-label': t('Library') }) as HTMLSelectElement
  const coll = h('select', { 'aria-label': t('Collection'), disabled: true }) as HTMLSelectElement
  const usesBiblatex = [...r.files.entries()].some(([p, f]) => p.endsWith('.tex') && /\\usepackage\s*(\[[^\]]{0,300}\])?\s*\{[^}]{0,200}\bbiblatex\b/.test(f.toString()))
  const format = h('select', { 'aria-label': t('Format') },
    h('option', { value: 'biblatex', selected: usesBiblatex }, 'BibLaTeX'), h('option', { value: 'bibtex', selected: !usesBiblatex }, 'BibTeX')) as HTMLSelectElement
  const bibs = [...r.files.keys()].filter((p) => p.endsWith('.bib') && !links(r)[p]).sort()
  const file = h('input', { value: 'zotero.bib', list: 'zotero-bibs', 'aria-label': t('File') }) as HTMLInputElement
  const msg = h('p', { class: 'small', role: 'status' })
  const go = h('button', { class: 'primary', disabled: true, onclick: () => run() }, t('Link and fetch'))
  const dlg = dialog('settings zlinkdlg',
    h('h3', {}, t('Link a Zotero collection')),
    h('label', { class: 'field' }, t('Library'), lib),
    h('label', { class: 'field' }, t('Collection'), coll),
    h('label', { class: 'field' }, t('Format'), format),
    h('label', { class: 'field' }, t('File'), file),
    h('datalist', { id: 'zotero-bibs' }, ...bibs.map((b) => h('option', { value: b }))),
    h('p', { class: 'muted small' }, t('The file is replaced with the collection’s entries now and on each refresh. Pick a file of its own if you also add entries by hand.')),
    msg,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go))
  let libs: Library[] = [], colls: Collection[] = []
  const say = (text: string, err = false) => { msg.className = err ? 'small err' : 'small muted'; msg.textContent = text }
  const loadCollections = async () => {
    coll.disabled = true; go.disabled = true; say(t('Fetching…'))
    try {
      colls = await collections(a, lib.value)
      coll.replaceChildren(h('option', { value: '' }, t('The whole library')), ...colls.map((c) => h('option', { value: c.key }, c.name)))
      coll.disabled = false; go.disabled = false; say('')
    } catch (e) { say(e instanceof Error ? e.message : String(e), true) }
  }
  lib.addEventListener('change', loadCollections)
  ;(async () => {
    say(t('Fetching…'))
    try {
      libs = await libraries(a)
      lib.replaceChildren(...libs.map((l) => h('option', { value: l.id }, l.name)))
      await loadCollections()
    } catch (e) { say(e instanceof Error ? e.message : String(e), true) }
  })()
  async function run() {
    const path = file.value.trim().replace(/^\/+/, '')
    if (!/^[^\\:*?"<>|]+\.bib$/i.test(path) || path.split('/').some((s) => s === '..' || s === '.')) { say(t('Choose a .bib file name, such as zotero.bib.'), true); return }
    if (r.files.has(path) && !confirm(t('{{file}} already exists. Replace its contents with the collection’s entries?', { file: path }))) return
    const chosen = colls.find((c) => c.key === coll.value)
    const link: Link = { lib: lib.value, libName: libs.find((l) => l.id === lib.value)?.name ?? lib.value, coll: chosen?.key ?? null,
      collName: chosen?.name ?? t('The whole library'), format: format.value as Link['format'], by: me(), at: Date.now() }
    go.disabled = true; say(t('Fetching…'))
    setLink(r, path, link)
    try { await refresh(r, path, me()); dlg.close(); done() }
    catch (e) { setLink(r, path, null); say(e instanceof Error ? e.message : String(e), true); go.disabled = false }
  }
}
