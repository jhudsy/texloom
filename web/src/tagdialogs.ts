// The windows for choosing a project's tags and for looking after the tags themselves.
import { dialog, h } from './dom'
import { t, tn } from './i18n'
import { foldTag } from './listcore'
import { cleanTagName, MAX_TAG_NAME, tags, type Tag } from './tags'

/** Assign or unassign tags on a project, and create new ones. */
export function pickTags(project: string, label: string, changed: () => void) {
  const typed = () => cleanTagName(input.value)
  const commit = () => {
    if (!typed()) return
    const tag = tags.find(typed()) ?? tags.create(typed())
    if (tag) tags.set(project, tag.id, true)
    input.value = ''
    render(); changed()
  }
  const input = h('input', {
    class: 'tagfind', placeholder: t('Find or create a tag…'), 'aria-label': t('Find or create a tag…'), maxlength: MAX_TAG_NAME, oninput: () => render(),
    onkeydown: (e: KeyboardEvent) => { if (e.key === 'Enter') { e.preventDefault(); commit() } },
  })
  const items = h('ul', { class: 'taglist' })
  const create = h('button', { hidden: true, onclick: commit })
  function render() {
    const want = foldTag(typed()), mine = new Set(tags.of(project).map((x) => x.id))
    const all = tags.all(), shown = all.filter((x) => foldTag(x.name).includes(want))
    items.replaceChildren(...shown.map((tag) => h('li', {}, h('label', {},
      h('input', { type: 'checkbox', checked: mine.has(tag.id), onchange: (e: any) => { tags.set(project, tag.id, e.target.checked); changed() } }),
      h('span', {}, tag.name)))))
    if (!all.length) items.replaceChildren(h('li', { class: 'muted small' }, t('No tags yet. Type a name to create one.')))
    create.hidden = !typed() || !!tags.find(typed())
    create.textContent = t('Create “{{name}}”', { name: typed() })
  }
  const dlg = dialog('settings',
    h('h3', {}, t('Tags for “{{name}}”', { name: label })),
    h('p', { class: 'muted small' }, t('Only you see your tags. They aren’t shared with collaborators.')),
    input, create, items,
    h('div', { class: 'row end' },
      h('button', { onclick: () => manageTags(() => { render(); changed() }) }, t('Manage tags…')),
      h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
  render()
}

/** Add, rename and delete tags. */
export function manageTags(changed: () => void) {
  const refresh = () => { render(); changed() }
  const add = () => { if (tags.create(addIn.value)) { addIn.value = ''; refresh() } }
  const addIn = h('input', {
    placeholder: t('New tag'), 'aria-label': t('New tag'), maxlength: MAX_TAG_NAME,
    onkeydown: (e: KeyboardEvent) => { if (e.key === 'Enter') { e.preventDefault(); add() } },
  })
  const items = h('ul', { class: 'taglist' })

  function rename(tag: Tag, input: HTMLInputElement) {
    const name = cleanTagName(input.value), other = tags.find(name)
    const merging = other && other.id !== tag.id
    if (!name || name === tag.name || (merging && !confirm(t('A tag called “{{name}}” already exists. Merge the two?', { name: other.name })))) {
      input.value = tag.name
      return
    }
    tags.rename(tag.id, name)
    refresh()
  }

  function remove(tag: Tag) {
    const n = tags.uses(tag.id)
    if (n && !confirm(tn(n, 'Delete the tag “{{name}}”? It will be taken off {{n}} project. The project itself isn’t changed.', 'Delete the tag “{{name}}”? It will be taken off {{n}} projects. The projects themselves aren’t changed.', { name: tag.name }))) return
    tags.remove(tag.id)
    refresh()
  }

  function render() {
    const all = tags.all()
    items.replaceChildren(...all.map((tag) => {
      const n = tags.uses(tag.id)
      const name: HTMLInputElement = h('input', { value: tag.name, 'aria-label': t('Name of the tag “{{name}}”', { name: tag.name }), maxlength: MAX_TAG_NAME, onchange: () => rename(tag, name) })
      return h('li', { class: 'managed' }, name,
        h('span', { class: 'muted small' }, tn(n, '{{n}} project', '{{n}} projects')),
        h('button', { 'aria-label': t('Delete the tag “{{name}}”', { name: tag.name }), onclick: () => remove(tag) }, t('Delete')))
    }))
    if (!all.length) items.replaceChildren(h('li', { class: 'muted small' }, t('No tags yet.')))
  }

  const dlg = dialog('settings',
    h('h3', {}, t('Manage tags')),
    h('p', { class: 'muted small' }, t('Rename a tag by editing its name. The change applies to every project that has it.')),
    items,
    h('div', { class: 'row' }, addIn, h('button', { onclick: add }, t('Add'))),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
  render()
}
