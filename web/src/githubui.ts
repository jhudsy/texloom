// The screens for keeping a project in step with a git repository (GitHub, GitLab, Gitea or
// Forgejo): connecting accounts (start page and project Settings), and linking the project to a
// repository in project Settings, with what's happening. The work is in github.ts and forges.ts.
import type { Replica } from './replica'
import { dialog, errorText, h, shortTime } from './dom'
import { t, tn } from './i18n'
import { accountFor, accounts, approve, approved, connect, GitError, linkOf, linkRepo, pull, push, setAccount, setLink, startGitHub } from './github'
import { forge, hostOf, kindOf, normalHost, repoPath, serviceName, tokenPage, type Account, type Kind } from './forges'
import type { Status } from './integrationcard'
import { UNTITLED } from './appinfo'

const DEFAULT_HOST: Record<Kind, string> = { github: 'https://github.com', gitlab: 'https://gitlab.com', gitea: 'https://codeberg.org' }

// the running link for the open project (main.ts starts it), and who wants to hear about it
let engine: ReturnType<typeof startGitHub> | null = null
const listeners = new Set<() => void>()

/** Start keeping the open project in step with its repository, if it has one (and whenever it gets one). */
export function runGitHub(r: Replica, me: () => string) {
  engine = startGitHub(r, me, () => listeners.forEach((f) => f()))
  r.meta.observe((ev) => { if (ev.keysChanged.has('github') || ev.keysChanged.has('githubLease')) listeners.forEach((f) => f()) })
}

/** The accounts this browser has, in a line (the start page's card). */
export function gitAccountsStatus(): Status {
  const have = accounts()
  return have.length ? { text: have.map((a) => t('{{service}} as {{name}}', { service: serviceName(a), name: a.login || '?' })).join(', '), tone: 'ok' }
    : { text: t('Not connected'), tone: 'off' }
}

/** The project's link and what's happening, in a line (its card in project Settings). */
export function gitStatus(r: Replica): Status {
  const l = linkOf(r), st = engine?.state
  if (!l) return accounts().length ? { text: t('Not linked'), tone: 'off' } : { text: t('Not connected'), tone: 'off' }
  const where = `${l.repo} · ${serviceName(l)}`
  if (l.blocked) return { text: t('⚠ {{where}}: changes clash', { where }), tone: 'warn', open: true }
  if (st?.error) return { text: t('⚠ {{where}}: not syncing', { where }), tone: 'err', open: true }
  if (!accountFor(l)) return { text: t('{{where}} (synced from another browser)', { where }), tone: 'off', open: true }
  if (!approved(r, l)) return { text: t('{{where}}: waiting for your approval', { where }), tone: 'warn', open: true }
  return { text: st?.role === 'syncing' ? t('{{where}}: in step', { where }) : st?.role === 'standby' ? t('{{where}}: kept in step by {{name}}', { where, name: st.holder ?? '?' }) : where, tone: 'ok', open: true }
}

/** The accounts this browser has connected, each with Disconnect, and a form to connect another
 *  (or, with `only`, that host's). `changed` runs after either. */
export function githubAccountBox(changed: () => void = () => {}, only?: { kind?: Kind; host?: string }): HTMLElement {
  const box = h('div', { class: 'zotero-account git-accounts' })
  const render = () => {
    const have = only ? [accountFor(only)].filter((a): a is Account => !!a) : accounts()
    const rows = have.map((a) => h('div', { class: 'row' },
      h('span', { class: 'spacer small' }, t('{{service}}: connected as {{name}}.', { service: serviceName(a), name: a.login || '?' })),
      h('button', { class: 'small', onclick: () => { setAccount(a, null); render(); changed() } }, t('Disconnect'))))
    if (only && have.length) { box.replaceChildren(...rows); return }
    const kind = h('select', { 'aria-label': t('Service') },
      h('option', { value: 'github' }, 'GitHub'),
      h('option', { value: 'gitlab' }, 'GitLab'),
      h('option', { value: 'gitea' }, t('Gitea or Forgejo (e.g. Codeberg)'))) as HTMLSelectElement
    const host = h('input', { 'aria-label': t('Server'), spellcheck: 'false', autocomplete: 'off' }) as HTMLInputElement
    const hostField = h('label', { class: 'field' }, t('Server'), host)
    const key = h('input', { type: 'password', placeholder: t('Token'), 'aria-label': t('Token'), autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement
    const make = h('a', { target: '_blank', rel: 'noopener' })
    const how = h('p', { class: 'muted small' })
    const msg = h('p', { class: 'err small', role: 'status' })
    const k = () => kind.value as Kind
    const server = () => (k() === 'github' ? DEFAULT_HOST.github : normalHost(host.value || DEFAULT_HOST[k()]))
    const explain = () => {
      hostField.hidden = k() === 'github'
      host.placeholder = DEFAULT_HOST[k()].replace('https://', '')
      const s = server()
      make.textContent = t('Make a token at {{site}}', { site: (s ?? DEFAULT_HOST[k()]).replace(/^https?:\/\//, '') })
      make.setAttribute('href', tokenPage(k(), s ?? DEFAULT_HOST[k()]))
      how.replaceChildren(k() === 'github'
        ? t('Make a fine-grained token for just the repositories you’ll use, with read and write access to their contents, then paste it here.')
        : k() === 'gitlab'
          ? t('Make a personal access token, then paste it here. A fine-grained one: choose only the projects you’ll use and give them Projects ▸ Project: Read, and Repository ▸ Repository: Read, Branch: Read and Create, and Commit: Create; then on the User tab, System Access ▸ User: Read. A legacy one needs the “api” scope.')
          : t('Make an access token with read and write access to repositories, and read access to your user, then paste it here. A server of your own must allow other sites to use its API (CORS).'),
      ' ', make)
    }
    if (only) { kind.value = kindOf(only); host.value = hostOf(only).replace('https://', '') }
    kind.addEventListener('change', () => { host.value = ''; explain() })
    host.addEventListener('input', explain)
    explain()
    const go = h('button', { onclick: async () => {
      msg.textContent = ''
      const s = server()
      if (!s) { msg.textContent = t('That isn’t a server address.'); return }
      go.setAttribute('disabled', '')
      try { const a = await connect(k(), s, key.value); setAccount(a, a); render(); changed() }
      catch (e) { msg.textContent = errorText(e) }
      finally { go.removeAttribute('disabled') }
    } }, t('Connect'))
    key.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click() })
    box.replaceChildren(...[
      ...rows,
      h('p', { class: 'muted small' }, have.length
        ? t('Connect another:')
        : t('Keep a project in step with a git repository on GitHub, GitLab, or Gitea or Forgejo (such as Codeberg).')),
      only ? null : h('label', { class: 'field' }, t('Service'), kind),
      only ? null : hostField,
      how,
      h('div', { class: 'row' }, key, go),
      msg,
      h('p', { class: 'muted small' }, t('The token stays in this browser. With a project list, it travels with the list, encrypted, so anyone with your sync link could use it.')),
    ].filter((x) => !!x) as HTMLElement[])
  }
  render()
  return box
}

/** Project Settings: the link and what's happening, Sync now and Unlink; or linking one. */
export function githubSection(r: Replica, me: () => string, writable: boolean): HTMLElement {
  const box = h('div', { class: 'github' })
  const msg = h('p', { class: 'small', role: 'status' })
  const note = (text: string, err = false) => { msg.className = err ? 'small err' : 'small muted'; msg.textContent = text }
  let shown = ''
  const render = () => {
    if (!box.isConnected && shown) { listeners.delete(render); return } // the dialog closed
    const l = linkOf(r), a = accountFor(l), st = engine?.state, svc = serviceName(l)
    const ok = approved(r, l) // this browser syncs only a link its user made or approved
    const now = JSON.stringify([accounts().map((x) => [x.kind, x.host]), l, ok, st?.role, st?.holder, st?.error, st?.busy])
    if (now === shown) return // nothing to redraw (and a token being typed stays)
    shown = now
    if (!l) {
      box.replaceChildren(...[
        h('p', { class: 'muted small' }, t('Not linked to a git repository.')),
        writable && accounts().length ? h('div', { class: 'row' }, h('button', { onclick: () => linkDialog(r, me, render) }, t('Link a repository…'))) : null,
        githubAccountBox(render),
      ].filter((x): x is HTMLElement => !!x))
      return
    }
    const where = h('div', {}, h('b', {}, l.repo), h('span', { class: 'muted' }, ` · ${svc} · ${l.branch}${l.dir ? ` · ${l.dir}/` : ''}`))
    const role = !a ? t('Connect your {{service}} account to sync it from this browser.', { service: svc })
      : !writable ? t('Read-only link: this browser doesn’t sync it.')
      : !ok ? t('A collaborator linked this project to {{repo}} on {{service}}. This browser doesn’t sync it until you approve: syncing uses your token, so it brings that repository’s files into the project and sends the project’s changes to it.', { repo: l.repo, service: svc })
      : st?.role === 'syncing' ? t('This browser keeps it in step: it brings commits in every minute, and sends changes once the project has been quiet for two minutes.')
      : st?.role === 'standby' ? t('{{name}} keeps it in step now (another browser, or the TeXLoom app). This browser takes over if they stop.', { name: st.holder ?? '?' })
      : t('Waiting to sync (offline, or catching up).')
    const blocked = l.blocked ? h('div', { class: 'risk' },
      h('span', {}, h('b', {}, '⚠ '), t('The repository has changes that clash with the project’s in {{files}}. Nothing was brought in. Make one side match the other, or keep the project’s version where they clash.', { files: (l.clash ?? []).join(', ') || '?' })),
      writable && a && ok ? h('span', { class: 'actions' }, h('button', { onclick: async (e: Event) => {
        const b = e.currentTarget as HTMLButtonElement
        if (!confirm(t('Bring in the repository’s changes, keeping the project’s version where the two clash? The project’s version then goes to the repository.'))) return
        b.disabled = true; note(t('Syncing…'))
        try { await pull(r, a, me(), true); await push(r, a, me()); note(t('Done.')) } catch (err) { note(errorText(err), true) } finally { b.disabled = false; render() }
      } }, t('Keep the project’s version'))) : null) : null
    box.replaceChildren(...[
      where,
      h('p', { class: 'muted small' }, role),
      l.head ? h('p', { class: 'muted small' }, t('Last synced {{when}} by {{name}}, at commit {{commit}}.', { when: shortTime(l.at), name: l.by || '?', commit: l.head.slice(0, 7) })) : null,
      blocked,
      st?.error ? h('p', { class: 'small err' }, st.error) : null,
      msg,
      writable ? h('div', { class: 'row' },
        a && !ok ? h('button', { class: 'small primary', onclick: async () => {
          const cur = linkOf(r)
          if (!cur || !confirm(t('Sync this project with {{repo}} on {{service}} (branch {{branch}}) using your token?', { repo: cur.repo, service: svc, branch: cur.branch }))) return
          approve(r, cur); note(t('Syncing…'))
          try { await engine?.now(); note(engine?.state.error ?? '') } catch (err) { note(errorText(err), true) }
          render()
        } }, t('Approve and sync')) : null,
        a && ok && st?.role !== 'standby' ? h('button', { class: 'small', disabled: st?.busy, onclick: async () => {
          note(t('Syncing…'))
          try { await engine?.now(); note(engine?.state.error ?? (engine?.state.role === 'syncing' && !linkOf(r)?.blocked ? t('Done.') : '')) } catch (err) { note(errorText(err), true) }
          render()
        } }, t('Sync now')) : null,
        h('button', { class: 'small', onclick: () => {
          if (!confirm(t('Stop keeping this project in step with {{repo}}? Both stay as they are.', { repo: l.repo }))) return
          setLink(r, null); r.meta.delete('githubLease'); approve(r, null); render()
        } }, t('Unlink'))) : null,
      a ? null : githubAccountBox(render, l),
    ].filter((x): x is HTMLElement => !!x))
  }
  listeners.add(render)
  render()
  return box
}

/** A new project from a git repository (Create project ▸ From a git repository…): an account with the
 *  service first if none is connected yet, then the link, which brings the repository's files in (or
 *  one folder's: nothing else of the repository comes into the project). */
export function repositoryDialog(r: Replica, me: () => string, done: () => void) {
  if (accounts().length) { linkDialog(r, me, done, true); return }
  const dlg = dialog('settings zlinkdlg',
    h('h3', {}, t('From a git repository')),
    h('p', { class: 'muted small' }, t('First connect your account with the service the repository is on.')),
    githubAccountBox(() => { if (accounts().length) { dlg.close(); linkDialog(r, me, done, true) } }),
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel'))))
}

/** Choose the repository, branch and folder; combine the two; start syncing. `fresh`: a project just
 *  made for it (repositoryDialog), so there's nothing to combine: the repository's files come in, and
 *  an unnamed project takes the folder's name (or the repository's). */
function linkDialog(r: Replica, me: () => string, done: () => void, fresh = false) {
  const have = accounts()
  if (!have.length) return
  const which = h('select', { 'aria-label': t('Service') }, ...have.map((a, i) => h('option', { value: String(i) }, `${serviceName(a)} (${a.login || '?'})`))) as HTMLSelectElement
  const account = () => have[Number(which.value)] ?? have[0]
  const repo = h('input', { placeholder: 'owner/repository', 'aria-label': t('Repository'), spellcheck: 'false' }) as HTMLInputElement
  const branch = h('input', { placeholder: 'main', 'aria-label': t('Branch'), spellcheck: 'false' }) as HTMLInputElement
  const dir = h('input', { placeholder: fresh ? t('e.g. papers/2026-loom (empty: the whole repository)') : t('(the whole repository)'), 'aria-label': t('Folder in the repository'), spellcheck: 'false' }) as HTMLInputElement
  const many = [...r.files.keys(), ...r.bin.keys()].length > 1
  const prefer = (v: 'repo' | 'project', label: string) => h('label', { class: 'radio' }, h('input', { type: 'radio', name: 'gh-prefer', value: v, checked: (v === 'project') === many }), label)
  const msg = h('p', { class: 'small', role: 'status' })
  const tell = (text: string, err = false) => { msg.className = err ? 'small err' : 'small muted'; msg.textContent = text }
  const name = () => { const a = account(); return repoPath(kindOf(a), hostOf(a), repo.value) }
  const fill = async () => {
    const n = name()
    if (!n || branch.value) return
    try { const d = (await forge(account()).repoInfo(n)).defaultBranch; if (!branch.value) branch.value = d } catch (e) { tell(errorText(e), true) } // (unless one was typed meanwhile)
  }
  const hint = () => { repo.placeholder = kindOf(account()) === 'gitlab' ? 'group/repository' : 'owner/repository' }
  hint()
  repo.addEventListener('change', fill)
  which.addEventListener('change', () => { hint(); fill() })
  const go = h('button', { class: 'primary', onclick: () => run() }, fresh ? t('Bring it in') : t('Link'))
  const dlg = dialog('settings zlinkdlg',
    h('h3', {}, fresh ? t('From a git repository') : t('Link a git repository')),
    have.length > 1 ? h('label', { class: 'field' }, t('Service'), which) : h('p', { class: 'muted small' }, t('On {{service}}, as {{name}}.', { service: serviceName(have[0]), name: have[0].login || '?' })),
    h('label', { class: 'field' }, t('Repository'), repo),
    h('label', { class: 'field' }, t('Branch'), branch),
    h('label', { class: 'field' }, t('Folder in the repository'), dir),
    ...(fresh ? [
      h('p', { class: 'muted small' }, t('Give the folder’s path from the top of the repository, as the repository’s page shows it (for example papers/2026-loom), and only that folder comes into the project: nothing else of the repository. Leave it empty for all of it. From then on the project and that folder are kept in step, both ways.')),
    ] : [
      h('p', { class: 'muted small' }, t('Files only in the repository come into the project, and files only in the project go to the repository. Where both have a file and they differ, keep:')),
      prefer('repo', t('the repository’s version')),
      prefer('project', t('the project’s version (it goes to the repository as a commit)')),
    ]),
    h('p', { class: 'muted small' }, t('Everyone in the project sees the link. It syncs from the browser (or the TeXLoom app) of whoever has connected an account with that service, one at a time.')),
    h('p', { class: 'muted small' }, t('If you also keep this repository in a folder with the TeXLoom app, turn on “Keep this folder’s git repository in step” there: the app and browsers then take turns, so changes never come in twice.')),
    msg,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go))
  async function run() {
    const a = account(), n = name()
    if (!n) { tell(t('Enter the repository as owner/name, or its address on {{service}}.', { service: serviceName(a) }), true); return }
    const folder = dir.value.trim().replace(/^\/+|\/+$/g, '')
    if (folder && folder.split('/').some((s) => !s || s === '.' || s === '..')) { tell(t('That isn’t a folder name.'), true); return }
    go.setAttribute('disabled', ''); tell(t('Fetching…'))
    try {
      const info = await forge(a).repoInfo(n)
      if (!info.canPush) throw new GitError(t('The token can read {{repo}} but not write to it.', { repo: n }))
      const keep = (dlg.querySelector('input[name=gh-prefer]:checked') as HTMLInputElement | null)?.value === 'project' ? 'project' : 'repo'
      const got = await linkRepo(r, a, me(), n, branch.value.trim() || info.defaultBranch, folder, keep)
      if (fresh && r.meta.get('name') === UNTITLED) r.meta.set('name', (folder || n).split('/').pop() || UNTITLED)
      tell(got ? tn(got, 'Linked; {{n}} file came in from the repository.', 'Linked; {{n}} files came in from the repository.') : t('Linked.'))
      dlg.close(); done()
      engine?.now()
    } catch (e) { tell(errorText(e), true) } finally { go.removeAttribute('disabled') }
  }
}
