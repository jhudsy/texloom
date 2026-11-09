// The windows for choosing a project's tags and for looking after the tags themselves.
import { dialog, h } from './dom'
import { t, tn } from './i18n'
import { foldTag } from './listcore'
import { cleanTagName, MAX_TAG_NAME, TAG_COLOURS, tags, type Tag, type TagColour } from './tags'

const COLOUR_NAMES: Record<TagColour, () => string> = {
  red: () => t('Red'), orange: () => t('Orange'), yellow: () => t('Yellow'), green: () => t('Green'), teal: () => t('Teal'),
  blue: () => t('Blue'), indigo: () => t('Indigo'), purple: () => t('Purple'), pink: () => t('Pink'), grey: () => t('Grey'),
}

/** Assign or unassign tags on one project or several, and create new ones. */
export function pickTags(projects: string[], title: string, changed: () => void) {
  const typed = () => cleanTagName(input.value)
  const commit = () => {
    if (!typed()) return
    const tag = tags.find(typed()) ?? tags.create(typed())
    if (tag) tags.set(projects, tag.id, true)
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
    const want = foldTag(typed()), of = tags.reader()
    const has = (id: string) => projects.filter((p) => of(p).some((x) => x.id === id)).length
    const all = tags.all(), shown = all.filter((x) => foldTag(x.name).includes(want))
    items.replaceChildren(...shown.map((tag) => {
      const n = has(tag.id)
      const box = h('input', { type: 'checkbox', checked: n === projects.length, onchange: (e: any) => { tags.set(projects, tag.id, e.target.checked); changed() } })
      box.indeterminate = n > 0 && n < projects.length // on some of them
      return h('li', {}, h('label', {}, box, h('span', { class: `tagdot tc-${tag.colour}` }), h('span', {}, tag.name)))
    }))
    if (!all.length) items.replaceChildren(h('li', { class: 'muted small' }, t('No tags yet. Type a name to create one.')))
    create.hidden = !typed() || !!tags.find(typed())
    create.textContent = t('Create “{{name}}”', { name: typed() })
  }
  const dlg = dialog('settings',
    h('h3', {}, title),
    h('p', { class: 'muted small' }, t('Only you see your tags. They aren’t shared with collaborators.'), projects.length > 1 ? ' ' + t('A dash means only some of these projects have that tag.') : ''),
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
  let picking: string | null = null // the tag whose colours are showing

  /** Show or hide a tag's colours, keeping the keyboard on its dot. */
  function pick(id: string | null, focusOn: string) {
    picking = id
    render()
    items.querySelector<HTMLElement>(`[data-swatch="${CSS.escape(focusOn)}"]`)?.focus()
  }

  function colours(tag: Tag): HTMLElement {
    return h('div', { class: 'tagcolours', role: 'group', 'aria-label': t('Colour') }, ...TAG_COLOURS.map((colour) => h('button', {
      class: `tagdot tc-${colour}${colour === tag.colour ? ' on' : ''}`, title: COLOUR_NAMES[colour](), 'aria-label': COLOUR_NAMES[colour](), 'aria-pressed': String(colour === tag.colour),
      onclick: () => { tags.setColour(tag.id, colour); changed(); pick(null, tag.id) },
    })))
  }

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
      const label = t('Change the colour of “{{name}}”', { name: tag.name })
      return h('li', { class: 'managed' },
        h('button', { class: `tagdot tc-${tag.colour}`, 'data-swatch': tag.id, title: label, 'aria-label': label, 'aria-expanded': String(picking === tag.id), onclick: () => pick(picking === tag.id ? null : tag.id, tag.id) }),
        name,
        h('span', { class: 'muted small' }, tn(n, '{{n}} project', '{{n}} projects')),
        h('button', { 'aria-label': t('Delete the tag “{{name}}”', { name: tag.name }), onclick: () => remove(tag) }, t('Delete')),
        picking === tag.id ? colours(tag) : null)
    }))
    if (!all.length) items.replaceChildren(h('li', { class: 'muted small' }, t('No tags yet.')))
  }

  const dlg = dialog('settings',
    h('h3', {}, t('Manage tags')),
    h('p', { class: 'muted small' }, t('Rename a tag by editing its name. The change applies to every project that has it.'), ' ', t('Click a dot to change a tag’s colour.')),
    items,
    h('div', { class: 'row' }, addIn, h('button', { onclick: add }, t('Add'))),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))),
  )
  render()
}
