// The GitHub screens: connecting an account (start page and project Settings), and linking the
// project to a repository in project Settings, with what's happening. The work is in github.ts.
import type { Replica } from './replica'
import { dialog, h } from './dom'
import { t, tn } from './i18n'
import { connect, githubAccount, GitHubError, linkOf, linkRepo, pull, push, repoInfo, repoName, setLink, startGitHub } from './github'

const NEW_TOKEN = 'https://github.com/settings/personal-access-tokens/new'
const ago = (at: number) => new Date(at).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
const say = (e: unknown) => (e instanceof Error ? e.message : String(e))

// the running link for the open project (main.ts starts it), and who wants to hear about it
let engine: ReturnType<typeof startGitHub> | null = null
const listeners = new Set<() => void>()

/** Start keeping the open project in step with its repository, if it has one (and whenever it gets one). */
export function runGitHub(r: Replica, me: () => string) {
  engine = startGitHub(r, me, () => listeners.forEach((f) => f()))
  r.meta.observe((ev) => { if (ev.keysChanged.has('github') || ev.keysChanged.has('githubLease')) listeners.forEach((f) => f()) })
}

/** Connected as …, or a box to connect with a token. `changed` runs after either. */
export function githubAccountBox(changed: () => void = () => {}): HTMLElement {
  const box = h('div', { class: 'zotero-account' })
  const render = () => {
    const a = githubAccount.get()
    if (a) {
      box.replaceChildren(h('div', { class: 'row' },
        h('span', { class: 'spacer small' }, t('Connected as {{name}}.', { name: a.login || '?' })),
        h('button', { class: 'small', onclick: () => { githubAccount.set(null); render(); changed() } }, t('Disconnect'))))
      return
    }
    const key = h('input', { type: 'password', placeholder: t('GitHub token'), 'aria-label': t('GitHub token'), autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement
    const msg = h('p', { class: 'err small', role: 'status' })
    const go = h('button', { onclick: async () => {
      msg.textContent = ''
      go.setAttribute('disabled', '')
      try { githubAccount.set(await connect(key.value)); render(); changed() }
      catch (e) { msg.textContent = say(e) }
      finally { go.removeAttribute('disabled') }
    } }, t('Connect'))
    key.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click() })
    box.replaceChildren(
      h('p', { class: 'muted small' }, t('Keep a project in step with a GitHub repository. Make a fine-grained token for just the repositories you’ll use, with read and write access to their contents, then paste it here.'), ' ',
        h('a', { href: NEW_TOKEN, target: '_blank', rel: 'noopener' }, t('Make a token at github.com'))),
      h('div', { class: 'row' }, key, go),
      msg,
      h('p', { class: 'muted small' }, t('The token stays in this browser. With a project list, it travels with the list, encrypted, so anyone with your sync link could use it.')))
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
    const a = githubAccount.get(), l = linkOf(r), st = engine?.state
    const now = JSON.stringify([!!a, l, st?.role, st?.holder, st?.error, st?.busy])
    if (now === shown) return // nothing to redraw (and a token being typed stays)
    shown = now
    if (!l) {
      box.replaceChildren(...[
        h('p', { class: 'muted small' }, t('Not linked to a GitHub repository.')),
        a ? null : githubAccountBox(render),
        writable && a ? h('div', { class: 'row' }, h('button', { onclick: () => linkDialog(r, me, render) }, t('Link a repository…'))) : null,
      ].filter((x): x is HTMLElement => !!x))
      return
    }
    const where = h('div', {}, h('b', {}, l.repo), h('span', { class: 'muted' }, ` · ${l.branch}${l.dir ? ` · ${l.dir}/` : ''}`))
    const role = !a ? t('Connect your GitHub account to sync it from this browser.')
      : !writable ? t('Read-only link: this browser doesn’t sync it.')
      : st?.role === 'syncing' ? t('This browser keeps it in step: it brings commits in every minute, and sends changes once the project has been quiet for two minutes.')
      : st?.role === 'standby' ? t('{{name}} keeps it in step now (another browser, or the TeXLoom app). This browser takes over if they stop.', { name: st.holder ?? '?' })
      : t('Waiting to sync (offline, or catching up).')
    const blocked = l.blocked ? h('div', { class: 'risk' },
      h('span', {}, h('b', {}, '⚠ '), t('GitHub has changes that clash with the project’s in {{files}}. Nothing was brought in. Make one side match the other, or keep the project’s version where they clash.', { files: (l.clash ?? []).join(', ') || '?' })),
      writable && a ? h('span', { class: 'actions' }, h('button', { onclick: async (e: Event) => {
        const b = e.currentTarget as HTMLButtonElement
        if (!confirm(t('Bring in GitHub’s changes, keeping the project’s version where the two clash? The project’s version then goes to GitHub.'))) return
        b.disabled = true; note(t('Syncing…'))
        try { await pull(r, a, me(), true); await push(r, a, me()); note(t('Done.')) } catch (err) { note(say(err), true) } finally { b.disabled = false; render() }
      } }, t('Keep the project’s version'))) : null) : null
    box.replaceChildren(...[
      where,
      h('p', { class: 'muted small' }, role),
      l.head ? h('p', { class: 'muted small' }, t('Last synced {{when}} by {{name}}, at commit {{commit}}.', { when: ago(l.at), name: l.by || '?', commit: l.head.slice(0, 7) })) : null,
      blocked,
      st?.error ? h('p', { class: 'small err' }, st.error) : null,
      msg,
      writable ? h('div', { class: 'row' },
        a && st?.role !== 'standby' ? h('button', { class: 'small', disabled: st?.busy, onclick: async () => {
          note(t('Syncing…'))
          try { await engine?.now(); note(engine?.state.error ?? (engine?.state.role === 'syncing' && !linkOf(r)?.blocked ? t('Done.') : '')) } catch (err) { note(say(err), true) }
          render()
        } }, t('Sync now')) : null,
        h('button', { class: 'small', onclick: () => {
          if (!confirm(t('Stop keeping this project in step with {{repo}}? Both stay as they are.', { repo: l.repo }))) return
          setLink(r, null); r.meta.delete('githubLease'); render()
        } }, t('Unlink'))) : null,
      a ? null : githubAccountBox(render),
    ].filter((x): x is HTMLElement => !!x))
  }
  listeners.add(render)
  render()
  return box
}

/** Choose the repository, branch and folder; combine the two; start syncing. */
function linkDialog(r: Replica, me: () => string, done: () => void) {
  const account = githubAccount.get()
  if (!account) return
  const a = account
  const repo = h('input', { placeholder: 'owner/repository', 'aria-label': t('Repository'), spellcheck: 'false' }) as HTMLInputElement
  const branch = h('input', { placeholder: 'main', 'aria-label': t('Branch'), spellcheck: 'false' }) as HTMLInputElement
  const dir = h('input', { placeholder: t('(the whole repository)'), 'aria-label': t('Folder in the repository'), spellcheck: 'false' }) as HTMLInputElement
  const many = [...r.files.keys(), ...r.bin.keys()].length > 1
  const prefer = (v: 'repo' | 'project', label: string) => h('label', { class: 'radio' }, h('input', { type: 'radio', name: 'gh-prefer', value: v, checked: (v === 'project') === many }), label)
  const msg = h('p', { class: 'small', role: 'status' })
  const tell = (text: string, err = false) => { msg.className = err ? 'small err' : 'small muted'; msg.textContent = text }
  repo.addEventListener('change', async () => {
    const name = repoName(repo.value)
    if (!name || branch.value) return
    try { branch.value = (await repoInfo(a, name)).defaultBranch } catch (e) { tell(say(e), true) }
  })
  const go = h('button', { class: 'primary', onclick: () => run() }, t('Link'))
  const dlg = dialog('settings zlinkdlg',
    h('h3', {}, t('Link a GitHub repository')),
    h('label', { class: 'field' }, t('Repository'), repo),
    h('label', { class: 'field' }, t('Branch'), branch),
    h('label', { class: 'field' }, t('Folder in the repository'), dir),
    h('p', { class: 'muted small' }, t('Files only in the repository come into the project, and files only in the project go to the repository. Where both have a file and they differ, keep:')),
    prefer('repo', t('the repository’s version')),
    prefer('project', t('the project’s version (it goes to GitHub as a commit)')),
    h('p', { class: 'muted small' }, t('Everyone in the project sees the link. It syncs from the browser (or the TeXLoom app) of whoever has connected GitHub, one at a time.')),
    h('p', { class: 'muted small' }, t('If you also keep this repository in a folder with the TeXLoom app, turn on “Keep this folder’s git repository in step” there: the app and browsers then take turns, so changes never come in twice.')),
    msg,
    h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go))
  async function run() {
    const name = repoName(repo.value)
    if (!name) { tell(t('Enter the repository as owner/name, or its github.com address.'), true); return }
    const folder = dir.value.trim().replace(/^\/+|\/+$/g, '')
    if (folder && folder.split('/').some((s) => !s || s === '.' || s === '..')) { tell(t('That isn’t a folder name.'), true); return }
    go.setAttribute('disabled', ''); tell(t('Fetching…'))
    try {
      const info = await repoInfo(a, name)
      if (!info.canPush) throw new GitHubError(t('The token can read {{repo}} but not write to it.', { repo: name }))
      const which = (dlg.querySelector('input[name=gh-prefer]:checked') as HTMLInputElement | null)?.value === 'project' ? 'project' : 'repo'
      const n = await linkRepo(r, a, me(), name, branch.value.trim() || info.defaultBranch, folder, which)
      tell(n ? tn(n, 'Linked; {{n}} file came in from GitHub.', 'Linked; {{n}} files came in from GitHub.') : t('Linked.'))
      dlg.close(); done()
      engine?.now()
    } catch (e) { tell(say(e), true) } finally { go.removeAttribute('disabled') }
  }
}
