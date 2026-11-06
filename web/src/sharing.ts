// Sharing: the owner's link, personal edit links (one per person, revocable), the
// read-only link, and moving a project to new keys after revoking someone.
import * as Y from 'yjs'
import { Keys } from './crypto'
import { Replica } from './replica'
import { dialog, h } from './dom'
import { projects } from './projects'

function copyRow(text: string) {
  const inp = h('input', { value: text, readonly: true, onfocus: (e: any) => e.target.select() })
  return h('div', { class: 'row' }, inp, h('button', { onclick: (e: any) => { navigator.clipboard.writeText(text); e.target.textContent = 'Copied' } }, 'Copy'))
}

const when = (t: number) => new Date(t).toLocaleDateString([], { dateStyle: 'medium' })

export function shareDialog(r: Replica, server: string, me: string) {
  const keys = r.keys
  const people = h('ul', { class: 'members' })
  const nameIn = h('input', { placeholder: 'Name, e.g. Alice', 'aria-label': 'Name for the personal link', maxlength: 60 })
  const newLink = h('div', {})

  const render = () => {
    const rows = [...r.members.entries()].sort((a, b) => (a[1]?.at ?? 0) - (b[1]?.at ?? 0))
    people.replaceChildren(...(rows.length ? rows.map(([pub, m]) => h('li', { class: m?.revoked ? 'revoked' : '' },
      h('span', { class: 'mname' }, String(m?.name ?? '?').slice(0, 60)),
      h('span', { class: 'muted small' }, m?.revoked ? ` revoked ${when(m.revoked)}` : ` since ${when(m?.at ?? 0)}`),
      h('span', { class: 'spacer' }),
      keys.owner && !m?.revoked ? h('button', { onclick: () => revoke(pub, String(m?.name ?? '')) }, 'Revoke') : null))
      : [h('li', { class: 'muted small' }, 'No personal links yet.')]))
  }

  function create() {
    const name = nameIn.value.trim()
    if (!name) return nameIn.focus()
    const { pub, keys: personal } = keys.personal(name)
    r.members.set(pub, { name: name.slice(0, 60), at: Date.now(), by: me })
    newLink.replaceChildren(h('label', {}, `Edit link for ${name} — send it to them only`), copyRow(personal.link(server, true)))
    nameIn.value = ''
  }

  function revoke(pub: string, name: string) {
    if (!confirm(`Revoke ${name}’s edit link?\n\nThe server will refuse their changes from now on, and so will everyone’s browsers. Edits they already made stay.`)) return
    if (!r.revoke(pub)) return alert('You’re offline: connect first so the server can be told.')
    r.members.set(pub, { ...r.members.get(pub), revoked: Date.now() })
    if (confirm(`${name} can no longer edit, but their link still lets them read new changes.\n\nMove the project to new keys so they can’t? Everyone else then needs a new link from you.`)) {
      dlg.close()
      rotateKeys(r, server, me)
    }
  }

  const ownerPart = keys.owner ? [
    h('label', {}, 'Your owner link — full access, including creating and revoking personal links. Keep it to yourself.'),
    copyRow(keys.link(server, true)),
    h('h4', {}, 'Personal edit links'),
    h('p', { class: 'muted small' }, 'Give each collaborator their own edit link, so you can later revoke one person without affecting the others. Their edits are signed with their own key.'),
    h('div', { class: 'row' }, nameIn, h('button', { class: 'primary', onclick: create }, 'Create link')),
    newLink,
    people,
  ] : keys.member ? [
    h('label', {}, `Your personal edit link (as ${keys.member.name}) — don’t share it; ask the owner for links for others`),
    copyRow(keys.link(server, true)),
  ] : []
  nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') create() })

  const dlg = dialog('share',
    h('h3', {}, 'Share this project'),
    ...ownerPart,
    h('h4', {}, 'Read-only link'), copyRow(keys.link(server, false)),
    h('p', { class: 'muted small' }, 'Keys stay in the part after “#”, which is never sent to the server.' +
      (keys.owner ? ' To shut someone out completely, revoke their link and move the project to new keys.' : '')),
    keys.owner ? h('details', {}, h('summary', {}, 'Move to new keys…'),
      h('p', { class: 'muted small' }, 'Copies everything (with history) into a new project with new keys and deletes the server’s copy of this one. Every existing link stops receiving changes; you hand out new ones.'),
      h('button', { class: 'danger', onclick: () => { dlg.close(); rotateKeys(r, server, me) } }, 'Move to new keys')) : null,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, 'Close')))
  r.members.observe(render)
  dlg.addEventListener('close', () => r.members.unobserve(render))
  render()
}

/** Copy the project into new keys (re-encrypting files), retire the old one, open the new one. */
export async function rotateKeys(old: Replica, server: string, me: string) {
  if (!old.keys.owner) return
  if (!confirm('Move this project to new keys?\n\nAll current links (yours included) will point to a retired copy. You get a new owner link and create new links for collaborators.')) return
  const note = dialog('settings', h('h3', {}, 'Moving to new keys…'), h('p', { class: 'muted' }, 'Re-encrypting files. Keep this tab open.'))
  try {
    const keys = Keys.generate()
    const fresh = new Replica(keys, server, 'live')
    await fresh.open()
    // Files (re-encrypted: blob ids depend on the key) first, so the copy never refers to missing blobs.
    const ids = new Map<string, string>()
    for (const [, b] of old.bin) {
      if (!b?.h || ids.has(b.h)) continue
      ids.set(b.h, await fresh.putBlob(await old.getBlob(b.h)))
    }
    Y.applyUpdate(fresh.doc, Y.encodeStateAsUpdate(old.doc))
    fresh.doc.transact(() => {
      for (const [p, b] of [...fresh.bin.entries()]) if (b?.h && ids.has(b.h)) fresh.bin.set(p, { ...b, h: ids.get(b.h)! })
      fresh.members.clear() // old personal links don't apply to the new keys
      fresh.out.delete('latest') // refers to old blobs; the next compile republishes
      for (const k of ['movedAt', 'storage']) fresh.meta.delete(k)
      fresh.meta.set('rotatedFrom', old.keys.project.slice(0, 8))
    })
    // let it reach the server (or stay in this browser if offline: it syncs when opened)
    for (let i = 0; i < 60 && (fresh.status !== 'online' || fresh.pending); i++) await new Promise((res) => setTimeout(res, 250))
    await fresh.close()
    // Retire the old project: tell its readers, then remove the server's copy.
    old.meta.set('movedAt', Date.now())
    await new Promise((res) => setTimeout(res, 800))
    await old.setStorage('relay')
    await new Promise((res) => setTimeout(res, 500))
    projects.update(keys.project, { fragment: keys.fragment(), writable: true, name: String(old.meta.get('name') ?? ''), at: Date.now() })
    try { localStorage.setItem(`texloom.device.${keys.project}`, localStorage.getItem(`texloom.device.${old.keys.project}`) ?? '{}') } catch {}
    await old.close()
    await projects.forget(old.keys.project).catch(() => {})
    note.close()
    alert('Done. This is the project at its new keys: use Share to send collaborators new links.')
    location.hash = keys.fragment()
  } catch (e) {
    note.close()
    alert(`Moving failed: ${e}. Nothing was retired; the project is unchanged.`)
  }
}
