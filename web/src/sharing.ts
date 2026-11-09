// Sharing: the owner's link, personal edit links (one per person, revocable), the
// read-only link, and moving a project to new keys after revoking someone.
import * as Y from 'yjs'
import { Keys, canonicalPub } from './crypto'
import { Replica } from './replica'
import { dialog, h } from './dom'
import { projects } from './projects'
import { t, locale } from './i18n'
import { privateServerAllows } from './storagenotice'

function copyRow(text: string, mail?: { subject: string; body: string }) {
  const inp = h('input', { value: text, readonly: true, onfocus: (e: any) => e.target.select() })
  return h('div', { class: 'row' }, inp,
    h('button', { onclick: (e: any) => { navigator.clipboard.writeText(text); e.target.textContent = t('Copied') } }, t('Copy')),
    mail ? h('a', { class: 'button mail', href: mailto(mail), title: t('Opens your own email app with the link filled in') }, t('Email…')) : null)
}

/** A message for the user's own mail app. The link travels in that email, never through
 *  the TeXLoom server, which mustn't see the keys in it. */
const mailto = ({ subject, body }: { subject: string; body: string }) =>
  `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`

/** The invitation text for a link. `who`: the person it's for (personal links). */
function invitation(r: Replica, link: string, access: 'edit' | 'read', me: string, who?: string) {
  const name = String(r.meta.get('name') ?? '').trim().slice(0, 80) || t('a LaTeX project')
  return {
    subject: access === 'edit' ? t('Invitation to edit “{{name}}” on TeXLoom', { name }) : t('Read-only link to “{{name}}” on TeXLoom', { name }),
    body: [
      who ? t('Hi {{who}},', { who }) : t('Hi,'),
      '',
      access === 'edit'
        ? (me ? t('{{me}} has invited you to edit “{{name}}” on TeXLoom. Open this link in your browser:', { me, name })
          : t('I have invited you to edit “{{name}}” on TeXLoom. Open this link in your browser:', { name }))
        : t('Here is a read-only link to “{{name}}” on TeXLoom. Open it in your browser:', { name }),
      '',
      link,
      '',
      access === 'edit' && who
        ? t('This link is for you only: please don’t forward it. Anyone who has it can read the project and change it as you.')
        : t('Anyone who has this link can read the project, so please don’t forward it.'),
    ].join('\n'),
  }
}

const when = (at: number) => new Date(at).toLocaleDateString(locale, { dateStyle: 'medium' })

/** Links issued from this browser (pub -> name, time): the shared `members` map can be edited
 *  by any collaborator, so the owner's own record is what can't be hidden from them. */
// Also keeps each personal link itself, so it can be copied or emailed again later. That's
// no new exposure: this browser already holds the owner key, which can do more than any of them.
type Issued = Record<string, { name: string; at: number; link?: string }>
const issuedKey = (project: string) => `texloom.issued.${project}`
function loadIssued(project: string): Issued {
  try { return JSON.parse(localStorage.getItem(issuedKey(project)) || '{}') } catch { return {} }
}
function saveIssued(project: string, v: Issued) {
  try { localStorage.setItem(issuedKey(project), JSON.stringify(v)) } catch {}
}

interface Person { pub: string; name: string; at: number; revoked?: number; verified: boolean; link?: string }

/** Everyone holding a personal link, from what can be trusted: entries with a valid grant
 *  (shown with their granted name), this browser's own record, and keys seen signing edits. */
function people(r: Replica): Person[] {
  const keys = r.keys, out = new Map<string, Person>()
  const add = (pub: string, p: Omit<Person, 'pub'>) => {
    const c = canonicalPub(pub)
    if (!c) return
    const old = out.get(c)
    out.set(c, { ...old, ...p, pub: c, verified: p.verified || !!old?.verified, revoked: p.revoked ?? old?.revoked, at: Math.min(old?.at ?? Infinity, p.at), link: p.link ?? old?.link })
  }
  for (const [pub, m] of r.members.entries()) {
    const name = String(m?.name ?? '')
    if (keys.grantValid(pub, name, m?.g)) add(pub, { name, at: Number(m?.at) || 0, revoked: Number(m?.revoked) || undefined, verified: true })
  }
  for (const [pub, m] of Object.entries(loadIssued(keys.project))) add(pub, { name: m.name, at: m.at, verified: true, link: typeof m.link === 'string' ? m.link : undefined })
  for (const [pub, name] of keys.signers) add(pub, { name, at: out.get(canonicalPub(pub) ?? '')?.at ?? Date.now(), verified: true })
  for (const p of out.values()) if (keys.revoked.has(p.pub)) p.revoked ??= p.at
  return [...out.values()].sort((a, b) => a.at - b.at)
}

export function shareDialog(r: Replica, server: string, me: string) {
  const keys = r.keys
  const list = h('ul', { class: 'members' })
  const nameIn = h('input', { placeholder: t('Name, e.g. Alice'), 'aria-label': t('Name for the personal link'), maxlength: 60 })
  const newLink = h('div', {})

  const render = () => {
    const rows = people(r)
    list.replaceChildren(...(rows.length ? rows.map((p) => h('li', { class: p.revoked ? 'revoked' : '' },
      h('span', { class: 'mname' }, p.name.slice(0, 60)),
      h('code', { class: 'muted small', title: t('Start of this link’s key: tells apart people with the same name') }, ` ${p.pub.slice(0, 6)}`),
      h('span', { class: 'muted small' }, p.revoked ? ' ' + t('revoked {{date}}', { date: when(p.revoked) }) : p.at ? ' ' + t('since {{date}}', { date: when(p.at) }) : ''),
      h('span', { class: 'spacer' }),
      keys.owner && !p.revoked && p.link ? h('button', { title: t('Copy {{name}}’s edit link', { name: p.name }), onclick: (e: any) => { navigator.clipboard.writeText(p.link!); e.target.textContent = t('Copied') } }, t('Copy link')) : null,
      keys.owner && !p.revoked && p.link ? h('a', { class: 'button mail', href: mailto(invitation(r, p.link, 'edit', me, p.name)), title: t('Opens your own email app with the link filled in') }, t('Email…')) : null,
      keys.owner && !p.revoked ? h('button', { onclick: () => revoke(p.pub, p.name) }, t('Revoke')) : null))
      : [h('li', { class: 'muted small' }, t('No personal links yet.'))]))
  }

  function create() {
    const name = nameIn.value.trim()
    if (!name) return nameIn.focus()
    const { pub, grant, keys: personal } = keys.personal(name)
    const at = Date.now(), granted = personal.member!.name
    r.members.set(pub, { name: granted, at, by: me, g: grant }) // g: the owner's grant, so the entry can be checked
    const link = personal.link(server, true)
    saveIssued(keys.project, { ...loadIssued(keys.project), [pub]: { name: granted, at, link } })
    newLink.replaceChildren(h('label', {}, t('Edit link for {{name}} — send it to them only (also kept in the list below, on this device)', { name })), copyRow(link, invitation(r, link, 'edit', me, granted)))
    nameIn.value = ''
  }

  function revoke(pub: string, name: string) {
    if (!confirm(t('Revoke {{name}}’s edit link?\n\nThe server will refuse their changes from now on, and so will everyone’s browsers. Edits they already made stay.', { name }))) return
    if (!r.revoke(pub)) return alert(t('You’re offline: connect first so the server can be told.'))
    const m = r.members.get(pub)
    if (m) r.members.set(pub, { ...m, revoked: Date.now() })
    if (confirm(t('{{name}} can no longer edit, but their link still lets them read new changes.\n\nMove the project to new keys so they can’t? Everyone else then needs a new link from you.', { name }))) {
      dlg.close()
      rotateKeys(r, server)
    }
  }

  const ownerPart = keys.owner ? [
    h('label', {}, t('Your owner link — full access, including creating and revoking personal links. Keep it to yourself.')),
    copyRow(keys.link(server, true)),
    h('h4', {}, t('Personal edit links')),
    h('p', { class: 'muted small' }, t('Give each collaborator their own edit link, so you can later revoke one person without affecting the others. Their edits are signed with their own key. For a group, one link named after the group works too (revoking it cuts off the whole group). Links you create are kept on this device, to copy or email again.')),
    h('div', { class: 'row' }, nameIn, h('button', { class: 'primary', onclick: create }, t('Create link'))),
    newLink,
    list,
  ] : keys.member ? [
    h('label', {}, t('Your personal edit link (as {{name}}) — don’t share it; ask the owner for links for others', { name: keys.member.name })),
    copyRow(keys.link(server, true)),
  ] : []
  nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') create() })

  const dlg = dialog('share',
    h('h3', {}, t('Share this project')),
    ...ownerPart,
    h('h4', {}, t('Read-only link')), copyRow(keys.link(server, false), invitation(r, keys.link(server, false), 'read', me)),
    h('p', { class: 'muted small' }, t('“Email…” opens your own email app. The link travels in that email (not through this server), so anyone who can read the email can open the project.')),
    h('p', { class: 'muted small' }, t('Keys stay in the part after “#”, which is never sent to the server.') +
      (keys.owner ? ' ' + t('To shut someone out completely, revoke their link and move the project to new keys.') : '')),
    keys.owner ? h('details', {}, h('summary', {}, t('Move to new keys…')),
      h('p', { class: 'muted small' }, t('Copies everything (with history) into a new project with new keys and deletes the server’s copy of this one. Every existing link stops receiving changes; you hand out new ones.')),
      h('button', { class: 'danger', onclick: () => { dlg.close(); rotateKeys(r, server) } }, t('Move to new keys'))) : null,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Close'))))
  r.members.observe(render)
  const timer = setInterval(render, 5000) // revocations and signers arrive from the network too
  dlg.addEventListener('close', () => { r.members.unobserve(render); clearInterval(timer) })
  render()
}

/** Copy the project into new keys (re-encrypting files), retire the old one, open the new one. */
export async function rotateKeys(old: Replica, server: string) {
  if (!old.keys.owner || !(await privateServerAllows())) return // the new keys are a new project
  if (!confirm(t('Move this project to new keys?\n\nAll current links (yours included) will point to a retired copy. You get a new owner link and create new links for collaborators.'))) return
  let retiring = false
  const note = dialog('settings', h('h3', {}, t('Moving to new keys…')), h('p', { class: 'muted' }, t('Re-encrypting files. Keep this tab open.')))
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
    // Retire the old project: tell its readers (signed, so no editor can fake it), then remove the server's copy.
    retiring = true
    const at = Date.now()
    old.meta.set('movedAt', { at, s: old.keys.ownerSign(`moved:${at}`) })
    await new Promise((res) => setTimeout(res, 800))
    await old.setStorage('relay')
    await new Promise((res) => setTimeout(res, 500))
    projects.update(keys.project, { fragment: keys.fragment(), writable: true, name: String(old.meta.get('name') ?? ''), at: Date.now() })
    try { localStorage.setItem(`texloom.device.${keys.project}`, localStorage.getItem(`texloom.device.${old.keys.project}`) ?? '{}') } catch {}
    await old.close()
    await projects.forget(old.keys.project).catch(() => {})
    note.close()
    alert(t('Done. This is the project at its new keys: use Share to send collaborators new links.'))
    location.hash = keys.fragment()
  } catch (e) {
    note.close()
    alert(retiring
      ? t('Moving failed part-way: {{error}}. The new copy may exist already; reopen this project to check, and try again.', { error: e })
      : t('Moving failed: {{error}}. Nothing was retired; the project is unchanged.', { error: e }))
  }
}
