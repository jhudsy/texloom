// How to connect an MCP tool that's already on this computer. It runs under the person's own
// account with that tool, as a collaborator: nothing here goes through the assistant.
import { dialog, h } from '../dom'
import { t } from '../i18n'

type Access = 'suggest' | 'edit' | 'read'
const INSTALL = 'uv tool install git+https://github.com/jhudsy/texloom'

function codeBlock(text: () => string): { el: HTMLElement; update(): void } {
  const pre = h('pre', {})
  const copy = h('button', { class: 'small', onclick: (e: any) => { navigator.clipboard.writeText(text()); e.target.textContent = t('Copied') } }, t('Copy'))
  return { el: h('div', { class: 'cmds' }, pre, copy), update() { pre.textContent = text(); copy.textContent = t('Copy') } }
}

/** `link(true)` is the edit link, `link(false)` the read-only one. */
export function connectToolDialog(writable: boolean, link: (write: boolean) => string) {
  let access: Access = writable ? 'suggest' : 'read'
  const args = (name: string) => ['mcp', ...(access === 'suggest' ? ['--suggest-only'] : []), '--name', name, link(access !== 'read')]
  const quoted = (a: string) => (/^[\w.-]+$/.test(a) ? a : `"${a}"`) // double quotes work in every common shell, and the link has nothing they'd expand
  const claude = codeBlock(() => `claude mcp add texloom -- texloom ${args('Claude Code').map(quoted).join(' ')}`)
  const other = codeBlock(() => JSON.stringify({ mcpServers: { texloom: { command: 'texloom', args: args('AI agent') } } }, null, 2))
  const update = () => { claude.update(); other.update() }
  const choice = (value: Access, label: string) => h('label', { class: 'check' },
    h('input', { type: 'radio', name: 'connect-access', value, checked: access === value, onchange: () => { access = value; update() } }), label)
  const install = codeBlock(() => INSTALL)
  install.update(); update()
  const dlg = dialog('settings',
    h('h3', {}, t('Connect an AI tool via MCP')),
    h('p', { class: 'muted small' }, t('Tools that support MCP, such as Claude Code, Claude Desktop or Cursor, can work on this project like a collaborator. They run on your computer under your own account with that tool, so no API key is needed here.')),
    h('h4', {}, t('Install the TeXLoom command-line helper once')),
    install.el,
    h('p', { class: 'muted small' }, h('a', { href: 'https://docs.astral.sh/uv/getting-started/installation/', target: '_blank', rel: 'noopener' }, t('How to install uv'))),
    writable ? h('h4', {}, t('Choose what the tool may do')) : null,
    writable ? h('div', { class: 'connect-access' },
      choice('suggest', t('Suggest changes for people to accept')), choice('edit', t('Edit files directly')), choice('read', t('Read the project only'))) : null,
    h('h4', {}, t('Add it to your tool')),
    h('p', { class: 'small' }, t('Claude Code: run this in a terminal')), claude.el,
    h('p', { class: 'small' }, t('Other tools: add this to their MCP settings')), other.el,
    h('p', { class: 'muted small' }, t('If your tool can’t find the texloom command, give its full path instead. If this project is also in a folder on this computer, you can use the folder’s path instead of the link.')),
    h('p', { class: 'risk small' }, t('These lines contain this project’s link, and your tool saves it in its settings. Anyone who can read those settings can open the project.')),
    h('p', { class: 'muted small' }, t('The tool reads text other people wrote in this project, which could include instructions aimed at it. On shared projects, suggesting is the safer choice.')),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
}
