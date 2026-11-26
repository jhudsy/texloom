// The start page: creating and opening projects, and the list of them (search, tags, sorting,
// pages, selecting several).
import { Keys } from './crypto'
import { Replica } from './replica'
import { projects, projectCode, whenOpened, whoIn, type Recent } from './projects'
import { bulkRemoveDialog, removeDialog, type RemoveScope } from './remove'
import { prefs, languagePicker } from './settings'
import { askCode, chooseNewProject, chooseServer, codeBox, SAFARI_NOTE } from './storagenotice'
import { $, dialog, h, askText } from './dom'
import { brandIcon, icon } from './icons'
import { t, tn, locale } from './i18n'
import { listDialog } from './listdialog'
import { savedSecret, tagsProblem, syncNow as syncList } from './listsync'
import { copyDialog } from './copyproject'
import { tags, type Tag } from './tags'
import { manageTags, pickTags } from './tagdialogs'
import { newProjectInApp, openInApp, sourceNote } from './openinapp'
import { zoteroAccountBox, zoteroAccountStatus } from './zoteroui'
import { integrationCard } from './integrationcard'
import { serviceName, kindOf } from './forges'
import { githubAccountBox, gitAccountsStatus } from './githubui'
import { dropboxAccountBox, dropboxAccountStatus } from './dropboxui'
import { fragmentFor, fragmentOfLink, serverName, serverOr } from './servers'
import { savedFolder } from './foldersync'
import { chooseTemplate, type NewProject } from './templates'
import { quiet } from './quiet'
import { inAppKey, APP, UNTITLED, brand, refreshProjects, onRefresh } from './appinfo'
import { getString, readJson, setString } from './store'

/** Logos of the Dropbox folder or git repository this browser keeps a project in step with (what it has approved). */
function keptIn(project: string): HTMLElement[] {
  const saved = (key: string) => readJson<any>(`texloom.${key}.${project}`, null)
  const out: HTMLElement[] = []
  const dbx = saved('dropboxApproved')
  if (dbx) out.push(h('span', { class: 'kept', title: t('Kept in step with the Dropbox folder {{folder}}', { folder: dbx.path || dbx.folder || '' }) }, brandIcon('dropbox')))
  const git = saved('githubApproved')
  if (git) out.push(h('span', { class: 'kept', title: t('Kept in step with {{repo}} on {{service}}', { repo: git.repo ?? '', service: serviceName(git) }) }, brandIcon(kindOf(git))))
  return out
}
const keptByApp = (project: string) => !!getString(inAppKey(project))

/** What the start page's list is narrowed to. */
const finding = { text: '', tags: new Set<string>(), page: 0 }
const PAGE_SIZES = [25, 50, 100, 0] // 0: everything on one page
const PAGE_KEY = 'texloom.projectPageSize'
let pageSize = (() => { const n = Number(getString(PAGE_KEY) ?? 25); return PAGE_SIZES.includes(n) ? n : 25 })()
const pageCount = (total: number) => (pageSize ? Math.max(1, Math.ceil(total / pageSize)) : 1)
/** Projects ticked on the start page. Kept across pages and searches. */
const selected = new Set<string>()
let showSelection = () => {}
/** The projects the list shows (all pages), and the tick box beside the search that selects them all. */
let matchingNow: Recent[] = []
let selectAllBox: HTMLInputElement | null = null
const foldSearch = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()

// ---- start page ---------------------------------------------------------------
export function landing() {
  document.title = APP
  const app = $('#app')
  app.className = 'landing'
  const linkIn = h('input', { id: 'openlink', placeholder: t('Paste a project link…'), autocomplete: 'off', spellcheck: 'false',
    oninput: () => { openBtn.disabled = !linkIn.value.trim(); linkErr.textContent = '' } }) as HTMLInputElement
  const linkErr = h('span', { class: 'err small', role: 'status' })
  const openBtn = h('button', { type: 'submit', disabled: true, title: t('Open the project this link belongs to') }, t('Open')) as HTMLButtonElement
  const open = (e: Event) => { // a link to another server's project opens here, synced with that server
    e.preventDefault()
    const f = fragmentOfLink(linkIn.value.trim())
    if (f) location.hash = f
    else linkErr.textContent = t('That isn’t a project link. Ask whoever shared it to copy it again with Share.')
  }
  const list = h('div', {}, projectList())
  const finder = h('label', { class: 'finder', title: t('Search your projects by name, document title or tag') }, icon('search'),
    h('input', { type: 'search', value: finding.text, placeholder: t('Search by name or tag…'), 'aria-label': t('Search your projects'), oninput: (e: any) => { finding.text = e.target.value; finding.page = 0; list.replaceChildren(projectList()) } }))
  selectAllBox = h('input', { type: 'checkbox', class: 'selectall', onchange: (e: any) => {
    for (const r of matchingNow) { if (e.target.checked) selected.add(r.project); else selected.delete(r.project) }
    refreshProjects()
    selectAllBox?.focus()
  } }) as HTMLInputElement
  const tagline = h('p', { class: 'sub', hidden: !!projects.list().length }, t('Local-first collaborative LaTeX. Your files live on your devices; the server only relays encrypted changes.'))
  const finderRow = h('div', { class: 'findrow', hidden: !projects.list().length }, selectAllBox, finder, sortPicker())
  const syncNote = h('span', { class: 'syncnote' })
  const showSync = () => {
    const synced = !!savedSecret()
    syncNote.replaceChildren(
      h('span', { class: 'muted small', title: synced
        ? t('This list is synced between your devices, encrypted. The server can’t read it.')
        : t('This list is kept only in this browser: clearing this site’s data deletes it.{{safari}} A project stored on the server can always be reopened from its link, so keep your links safe, or sync this list to have it on all your devices.', { safari: SAFARI_NOTE }) },
        synced ? t('Synced between your devices') : t('Only in this browser')),
      h('button', { class: 'small', onclick: () => listDialog(() => { showSync(); refreshProjects() }) }, synced ? t('Manage sync…') : t('Sync between your devices…')))
  }
  showSync()
  app.append(
    h('div', { class: 'masthead' },
      h('div', {}, h('h1', {}, brand()), tagline),
      h('button', { class: 'icon gear', title: t('Settings'), 'aria-label': t('Settings'), onclick: startSettings }, icon('gear'))),
    h('div', { class: 'startrow' },
      h('button', { class: 'primary big', title: t('Start a new project from a template, an import or a folder'), onclick: () => createProject() }, icon('plus'), t('Create project')),
      h('form', { class: 'openlink', onsubmit: open },
        h('label', { for: 'openlink' }, t('Have a link to a project?')),
        h('div', { class: 'joined' }, linkIn, openBtn),
        linkErr)),
    h('section', { class: 'yours' },
      h('div', { class: 'listhead' }, h('h2', {}, t('Your projects')), syncNote),
      finderRow,
      list),
    sourceNote(),
  )
  onRefresh(() => {
    finderRow.hidden = !projects.list().length
    tagline.hidden = !!projects.list().length
    list.replaceChildren(projectList())
  })
}

/** Make a project: here, from a template or an import, or from a folder through the desktop app. */
async function createProject(choice?: NewProject) {
  const c = choice ?? await chooseTemplate()
  if (!c) return
  const server = await chooseServer(projects.list().map((r) => r.server))
  if (!server) return
  if (c.from === 'folder') return newProjectInApp(server)
  const where = await chooseNewProject(server)
  if (!where) return
  const k = Keys.generate()
  if (where === 'relay') setString(`texloom.newRelay.${k.project}`, '1')
  setString(`texloom.newTemplate.${k.project}`, c.from)
  if (c.name) setString(`texloom.newName.${k.project}`, c.name)
  location.hash = fragmentFor(k.fragment(), server)
}

/** The start page's own settings: your name and the language (set once, so not on the page). */
function startSettings() {
  const nameIn = h('input', { value: prefs.name, placeholder: t('Your name (shown to collaborators)'), oninput: (e: any) => (prefs.name = e.target.value) })
  const dlg = dialog('settings',
    h('h3', {}, t('Settings')),
    h('label', { class: 'field' }, t('Name'), nameIn),
    languagePicker(),
    h('h4', {}, t('Creation code')),
    codeBox(),
    h('h4', {}, t('Integrations')),
    integrationCard('Zotero', zoteroAccountStatus, zoteroAccountBox()),
    integrationCard(t('Git repositories'), gitAccountsStatus, githubAccountBox()),
    integrationCard('Dropbox', dropboxAccountStatus, dropboxAccountBox()),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => { dlg.close(); refreshProjects() } }, t('Done'))))
}

/** Every word is somewhere in the project's names or tags, and it has every chosen tag. */
function found(r: Recent, label: string, own: Tag[]): boolean {
  if ([...finding.tags].some((id) => !own.some((x) => x.id === id))) return false
  const text = foldSearch([label, r.name, r.nick, r.docTitle, projectCode(r.project), ...own.map((x) => x.name)].filter(Boolean).join(' '))
  return foldSearch(finding.text).split(/\s+/).filter(Boolean).every((word) => text.includes(word))
}

function toggleFilter(id: string) {
  if (!finding.tags.delete(id)) finding.tags.add(id)
  finding.page = 0
  refreshProjects()
  document.querySelector<HTMLElement>(`.tagbar [data-tag="${CSS.escape(id)}"]`)?.focus()
}

function tagBar(): HTMLElement | null {
  const all = tags.all()
  for (const id of finding.tags) if (!all.some((x) => x.id === id)) finding.tags.delete(id)
  const stuck = tagsProblem() === 'needs-code'
  if (!all.length && !stuck) return null
  return h('div', {},
    stuck ? h('p', { class: 'muted small' }, t('Your tags are saved only in this browser until you enter a creation code here.'), ' ',
      h('button', { class: 'small', onclick: async () => { if (await askCode()) syncList().catch((e) => quiet('the project list syncs again later (offline, say)', e)).finally(() => refreshProjects()) } }, t('Enter code'))) : null,
    all.length ? h('div', { class: 'tagbar', role: 'group', 'aria-label': t('Filter by tag') },
      h('span', { class: 'muted small' }, t('Filter by tag:')),
      ...all.map((tag) => h('button', { class: `utag tc-${tag.colour}${finding.tags.has(tag.id) ? ' on' : ''}`, 'data-tag': tag.id, 'aria-pressed': String(finding.tags.has(tag.id)), onclick: () => toggleFilter(tag.id) }, tag.name)),
      finding.tags.size ? h('button', { class: 'addtag', onclick: () => { finding.tags.clear(); finding.page = 0; refreshProjects() } }, t('Clear filter')) : null,
      h('button', { class: 'addtag', onclick: () => manageTags(() => refreshProjects()) }, t('Manage tags…'))) : null)
}

const projectLabel = (r: { nick?: string; name: string }) => r.nick || (r.name && r.name !== UNTITLED ? r.name : t('Untitled project'))
const byName = (a: Recent, b: Recent) => projectLabel(a).localeCompare(projectLabel(b), locale, { numeric: true, sensitivity: 'base' })
const SORTS: Record<string, { label: () => string; order: (a: Recent, b: Recent) => number }> = {
  opened: { label: () => t('Last opened'), order: (a, b) => b.at - a.at },
  changed: { label: () => t('Last changed'), order: (a, b) => (b.modified ?? b.at) - (a.modified ?? a.at) },
  newest: { label: () => t('Newest first'), order: (a, b) => (b.created ?? b.at) - (a.created ?? a.at) },
  oldest: { label: () => t('Oldest first'), order: (a, b) => (a.created ?? a.at) - (b.created ?? b.at) },
  az: { label: () => t('Name (A to Z)'), order: byName },
  za: { label: () => t('Name (Z to A)'), order: (a, b) => byName(b, a) },
}
const SORT_KEY = 'texloom.projectSort'
let sorting = (() => { const s = getString(SORT_KEY); return s && SORTS[s] ? s : 'opened' })()

function sortPicker(): HTMLElement {
  const select = h('select', { class: 'sorter', 'aria-label': t('Sort projects'), onchange: (e: any) => {
    sorting = e.target.value
    setString(SORT_KEY, sorting)
    finding.page = 0
    refreshProjects()
  } }, ...Object.entries(SORTS).map(([id, s]) => h('option', { value: id, ...(id === sorting ? { selected: true } : {}) }, s.label())))
  return h('label', { class: 'sortby' }, h('span', { class: 'muted small' }, t('Sort by')), select)
}

async function removeProject(r: Recent, scope: RemoveScope) {
  let keys: Keys | null = null
  try { keys = Keys.fromFragment('#' + r.fragment) } catch (e) { quiet('a damaged saved link: removed from this browser only', e) }
  if (scope !== 'browser' && keys) {
    const rep = new Replica(keys, serverOr(r.server), 'live')
    await rep.open()
    try { await (scope === 'everyone' ? rep.deleteForEveryone() : rep.removeFromServer()) } finally { await rep.close() }
  }
  await projects.forget(r.project)
  selected.delete(r.project)
}

/** May this browser remove the server's copy, or delete the project for everyone? Not once it's deleted. */
const canOwn = (r: Recent) => { if (r.deleted) return false; try { return !!Keys.fromFragment('#' + r.fragment)?.owner } catch { return false } }
const hasServerCopy = (r: Recent) => !(r.atRisk || r.storage === 'relay' || r.deleted)

/** What to do with the selected projects; shown only while some are. The tick box that selects
 *  everything found sits beside the search box (selectAllBox). */
function selectionBar(): HTMLElement {
  const count = h('span', { class: 'muted small' })
  const chosen = () => projects.list().filter((r) => selected.has(r.project))
  const bar = h('div', { class: 'selectbar' }, count,
    h('span', { class: 'bulk' },
      h('button', { class: 'small', onclick: () => { const n = selected.size; pickTags([...selected], tn(n, 'Tags for {{n}} project', 'Tags for {{n}} projects'), () => refreshProjects()) } }, t('Tags…')),
      h('button', { class: 'small', onclick: () => bulkRemoveDialog(
        chosen().map((r) => ({ id: r.project, name: projectLabel(r), code: projectCode(r.project), owner: canOwn(r), onServer: hasServerCopy(r) })),
        async (item, fromServer) => { const r = projects.list().find((x) => x.project === item.id); if (r) await removeProject(r, fromServer ? 'server' : 'browser') },
        () => refreshProjects()) }, t('Remove…')),
      h('button', { class: 'small', onclick: () => { selected.clear(); refreshProjects() } }, t('Clear selection'))))
  showSelection = () => {
    const inView = matchingNow.filter((r) => selected.has(r.project)).length
    if (selectAllBox) {
      selectAllBox.checked = matchingNow.length > 0 && inView === matchingNow.length
      selectAllBox.indeterminate = inView > 0 && inView < matchingNow.length
      selectAllBox.setAttribute('aria-label', tn(matchingNow.length, 'Select the {{n}} project found', 'Select all {{n}} projects found'))
      selectAllBox.title = selectAllBox.getAttribute('aria-label')!
    }
    const hidden = selected.size - inView
    count.textContent = hidden
      ? tn(selected.size, '{{n}} selected ({{hidden}} not shown)', '{{n}} selected ({{hidden}} not shown)', { hidden })
      : tn(selected.size, '{{n}} selected', '{{n}} selected')
    bar.hidden = !selected.size
    $('#app').classList.toggle('selecting', selected.size > 0)
  }
  showSelection()
  return bar
}

function goToPage(page: number, then: string) {
  finding.page = page
  refreshProjects()
  document.querySelector('.finder')?.scrollIntoView({ block: 'start' })
  document.querySelector<HTMLElement>(then)?.focus()
}

function pageSizePicker(): HTMLElement {
  return h('select', { class: 'pagesize', 'aria-label': t('Projects per page'), onchange: (e: any) => {
    pageSize = Number(e.target.value)
    setString(PAGE_KEY, String(pageSize))
    finding.page = 0
    refreshProjects()
    document.querySelector<HTMLElement>('.pagesize')?.focus()
  } }, ...PAGE_SIZES.map((n) => h('option', { value: n, ...(n === pageSize ? { selected: true } : {}) }, n ? t('{{n}} per page', { n }) : t('All on one page'))))
}

/** Below the list: pages, and how many to a page (once there are more than fit on one). */
function pager(total: number, all: number): HTMLElement | null {
  const paged = pageCount(total) > 1
  if (!paged && all <= PAGE_SIZES[0]) return null
  const last = pageCount(total) - 1
  const from = finding.page * pageSize + 1
  return h('nav', { class: 'pager', 'aria-label': t('Pages') },
    paged ? h('button', { class: 'prev', disabled: finding.page === 0, onclick: () => goToPage(finding.page - 1, '.pager .prev') }, t('Previous')) : null,
    paged ? h('span', { class: 'muted small' }, t('{{from}} to {{to}} of {{total}}', { from, to: Math.min(from + pageSize - 1, total), total })) : null,
    paged ? h('button', { class: 'next', disabled: finding.page === last, onclick: () => goToPage(finding.page + 1, '.pager .next') }, t('Next')) : null,
    pageSizePicker())
}

/** No projects yet: the ways to start one. */
function emptyState(): HTMLElement {
  const card = (title: string, info: string, run: () => unknown) =>
    h('button', { class: 'radio choice', onclick: run }, h('span', {}, h('b', {}, title), h('small', { class: 'muted' }, info)))
  return h('div', { class: 'empty' },
    h('p', {}, t('No projects here yet. Start one:')),
    h('div', { class: 'tgrid' },
      card(t('From a template…'), t('An article, a paper with references, slides, a thesis or a letter.'), () => createProject()),
      card(t('Import…'), t('From a .zip (e.g. Overleaf, GitHub) or an arXiv paper.'), () => createProject({ from: 'import', name: '' })),
      card(t('From a git repository…'), t('One folder of a repository on GitHub, GitLab, Gitea or Forgejo, or all of it, kept in step with the project.'), () => createProject({ from: 'git', name: '' })),
      card(t('From a folder on this computer…'), t('A folder you already have, kept in sync with the project by the TeXLoom app.'), () => newProjectInApp())),
    h('p', { class: 'muted small' }, t('Someone shared a project with you? Paste its link above.')))
}

function projectList(): HTMLElement {
  const list = projects.list()
  if (!list.length) { matchingNow = []; return emptyState() }
  for (const id of selected) if (!list.some((r) => r.project === id)) selected.delete(id)
  const bar = tagBar()
  const tagsOf = tags.reader()
  const dupes = new Set(list.map(projectLabel).filter((n, i, all) => all.indexOf(n) !== i))
  const me = prefs.name.trim()
  const matching = list.filter((r) => found(r, projectLabel(r), tagsOf(r.project))).sort(SORTS[sorting].order)
  matchingNow = matching
  finding.page = Math.min(finding.page, pageCount(matching.length) - 1)
  const page = pageSize ? matching.slice(finding.page * pageSize, (finding.page + 1) * pageSize) : matching
  const rows = page.length ? h('ul', { class: 'recent' }, ...page.map((r) => {
    const label = projectLabel(r)
    const code = projectCode(r.project)
    const { owner, shared: others } = whoIn(r.createdBy, r.people, me)
    const meta: string[] = []
    if (owner) meta.push(t('Owner: {{name}}', { name: owner }))
    if (others.length) meta.push(t('Shared with: {{names}}', { names: `${others.slice(0, 4).join(', ')}${others.length > 4 ? ` +${others.length - 4}` : ''}` }))
    if (r.files) meta.push(tn(r.files, '{{n}} file', '{{n}} files'))
    if (r.created && (sorting === 'newest' || sorting === 'oldest')) meta.push(t('added {{when}}', { when: whenOpened(r.created) }))
    if (r.modified) meta.push(t('changed {{when}}', { when: whenOpened(r.modified) }))
    if (!r.modified || whenOpened(r.modified) !== whenOpened(r.at)) meta.push(t('opened {{when}}', { when: whenOpened(r.at) }))
    const inApp = keptByApp(r.project)
    return h('li', { class: dupes.has(label) ? 'dupe' : '' },
      h('input', { type: 'checkbox', class: 'pick', 'aria-label': t('Select {{name}}', { name: label }), checked: selected.has(r.project), onchange: (e: any) => {
        if (e.target.checked) selected.add(r.project); else selected.delete(r.project)
        showSelection()
      } }),
      h('div', { class: 'top' },
        h('a', { href: `#p=${r.project}`, class: 'pname', title: r.nick && r.name ? t('{{name}} · others see “{{shared}}” · project code {{code}}', { name: label, shared: r.name, code }) : t('{{name}} · project code {{code}}', { name: label, code }) }, label),
        dupes.has(label) ? h('code', { class: 'pcode', title: t('Project code: derived from this project’s unique ID. Two projects with the same name always have different codes.') }, code) : null,
        r.writable ? null : h('span', { class: 'tag ro' }, t('read-only')),
        r.server ? h('span', { class: 'tag away', title: t('Kept on another server, {{server}}: this page syncs it with that server.', { server: r.server }) }, serverName(r.server)) : null,
        ...keptIn(r.project),
        r.deleted ? h('span', { class: 'tag warn', title: t('The owner deleted this project for everyone. This browser keeps a read-only copy.') }, t('deleted by the owner'))
          : !hasServerCopy(r) ? h('span', { class: 'tag warn', title: t('The server keeps no copy: this browser (and collaborators’ devices) hold the only copies. Clearing browser data deletes this one.') }, t('not on server')) : null,
        h('span', { class: 'spacer' }),
        r.deleted ? null : h('button', { class: `icon to-app${inApp ? ' on' : ''}`,
          title: inApp ? t('In the TeXLoom app: the app on this computer keeps this project in a folder. Click to show it there.') : t('Sync to a folder…: keep this project in a folder on this computer, using the TeXLoom app'),
          'aria-label': inApp ? t('{{name}} is in the TeXLoom app', { name: label }) : t('Sync {{name}} to a folder…', { name: label }),
          onclick: () => openInApp(`${serverOr(r.server)}/#${r.fragment}`) }, icon('folder')),
        h('button', { class: 'icon', title: t('Private label (only you see it)'), 'aria-label': t('Label {{name}}', { name: label }), onclick: async () => {
          const v = await askText(t('Private label for “{{name}}” ({{code}}).\nOnly you see it, in this browser. Leave empty to use the shared name.', { name: r.name || t('this project'), code }), r.nick ?? '')
          if (v === null) return
          projects.update(r.project, { nick: v.trim() })
          refreshProjects()
        } }, icon('pencil')),
        h('button', { class: 'icon', title: t('Make a copy'), 'aria-label': t('Make a copy of {{name}}…', { name: label }), onclick: () => {
          try {
            const keys = Keys.fromFragment('#' + r.fragment)
            if (keys) copyDialog({ name: label, by: prefs.name, server: serverOr(r.server), keys })
          } catch (e) { alert(e) }
        } }, icon('copy')),
        h('button', { class: 'icon forget', title: t('Remove…'), 'aria-label': t('Remove {{name}}…', { name: label }), onclick: async () => {
          const folder = await savedFolder(r.project).catch((e) => quiet('no saved folder known: the dialog doesn’t offer it', e))
          removeDialog({ name: label, code, owner: canOwn(r), onServer: hasServerCopy(r), folder: folder?.name ?? null },
            async (scope) => { await removeProject(r, scope); refreshProjects() })
        } }, icon('close'))),
      r.docTitle && r.docTitle !== label && r.docTitle !== r.name ? h('div', { class: 'dtitle' }, r.docTitle) : null,
      h('div', { class: 'details muted' },
        meta.length ? h('span', {}, meta.join(' · ')) : null,
        ...tagsOf(r.project).map((tag) => h('span', { class: 'tagchip' },
          h('button', { class: `utag tc-${tag.colour}${finding.tags.has(tag.id) ? ' on' : ''}`, title: t('Show only projects tagged “{{name}}”', { name: tag.name }), onclick: () => toggleFilter(tag.id) }, tag.name),
          h('button', { class: `utag x tc-${tag.colour}${finding.tags.has(tag.id) ? ' on' : ''}`, title: t('Remove the tag “{{tag}}” from {{name}}', { tag: tag.name, name: label }),
            'aria-label': t('Remove the tag “{{tag}}” from {{name}}', { tag: tag.name, name: label }), onclick: () => { tags.set([r.project], tag.id, false); refreshProjects() } }, '×'))),
        h('button', { class: 'addtag', 'aria-label': t('Tags for {{name}}…', { name: label }), onclick: () => pickTags([r.project], t('Tags for “{{name}}”', { name: label }), () => refreshProjects()) }, t('+ Tag'))))
  })) : h('p', { class: 'muted' }, t('No projects match your search.'))
  return h('div', {}, bar, selectionBar(), rows, pager(matching.length, list.length))
}
