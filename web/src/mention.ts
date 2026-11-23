// @name mentions in comments and replies (issue #3). A mention is just text: `@` and a collaborator's
// name, found by matching against the names the project knows (its personal links and everyone who
// has commented), so desktop apps and agents write and read them the same way, with nothing stored
// beside the text. The server can't read projects, so a mention is seen when the project is opened.
import { h } from './dom'
import type { Replica } from './replica'
import type { Item } from './review'

const WORDISH = /[\p{L}\p{N}_]/u

/** The project's collaborators by name: its personal links (as granted) and everyone who has written
 *  a comment or reply, without `me` and without duplicates (ignoring case), longest first. */
export function collaborators(r: Replica, items: Item[], me = ''): string[] {
  const names = new Map<string, string>()
  const add = (n: unknown) => {
    const name = typeof n === 'string' ? n.trim().slice(0, 64) : ''
    if (name && name.toLowerCase() !== me.trim().toLowerCase() && !names.has(name.toLowerCase())) names.set(name.toLowerCase(), name)
  }
  r.members.forEach((m) => add(m?.name))
  for (const it of items) { add(it.by); for (const rp of it.replies) add(rp.by) }
  return [...names.values()].sort((a, b) => b.length - a.length || a.localeCompare(b))
}

/** Whether `text` mentions `name`: `@name`, any case, not followed by a letter or digit. */
export function mentions(text: string, name: string): boolean {
  const n = name.trim()
  if (!n) return false
  const hay = text.toLowerCase(), needle = `@${n.toLowerCase()}`
  for (let i = hay.indexOf(needle); i >= 0; i = hay.indexOf(needle, i + 1)) {
    const after = text[i + needle.length]
    if (after === undefined || !WORDISH.test(after)) return true
  }
  return false
}

/** An item whose text or replies mention `me`. */
export function mentionsMe(it: Item, me: string): boolean {
  return !!me.trim() && (mentions(it.text, me) || it.replies.some((rp) => mentions(rp.text, me)))
}

/** Typing `@` in `area` offers the names that start with what follows it; ↑/↓ choose, Enter or Tab
 *  put one in, Escape closes. Its keys are handled before the box's own (a reply sends on Enter). */
export function mentionPicker(area: HTMLTextAreaElement, names: () => string[]): void {
  let menu: HTMLElement | null = null
  let choices: string[] = []
  let at = 0, chosen = 0
  const close = () => { menu?.remove(); menu = null }
  const query = (): { from: number; q: string } | null => {
    const before = area.value.slice(0, area.selectionStart ?? 0)
    const m = /(^|[^\p{L}\p{N}_])@([\p{L}\p{N}_ .'’-]{0,40})$/u.exec(before)
    return m ? { from: before.length - m[2].length - 1, q: m[2] } : null
  }
  const put = (name: string) => {
    const end = area.selectionStart ?? at
    area.setRangeText(`@${name} `, at, end, 'end')
    area.dispatchEvent(new Event('input', { bubbles: true }))
    close()
    area.focus()
  }
  const show = () => {
    const found = query()
    const q = found?.q.toLowerCase() ?? ''
    choices = found ? names().filter((n) => n.toLowerCase().startsWith(q) && n.toLowerCase() !== q).slice(0, 8) : []
    if (!found || !choices.length) { close(); return }
    at = found.from
    chosen = Math.min(chosen, choices.length - 1)
    const box = area.getBoundingClientRect()
    menu?.remove()
    menu = h('div', { class: 'mention-menu', role: 'listbox', style: `left:${Math.round(box.left)}px; top:${Math.round(box.bottom + 2)}px` },
      ...choices.map((n, i) => h('div', { class: `mention-choice${i === chosen ? ' on' : ''}`, role: 'option', 'aria-selected': String(i === chosen),
        onmousedown: (e: MouseEvent) => { e.preventDefault(); put(n) } }, `@${n}`)))
    document.body.append(menu)
  }
  area.addEventListener('input', () => { chosen = 0; show() })
  area.addEventListener('blur', close)
  area.addEventListener('keydown', (e) => {
    if (!menu || e.isComposing) return
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { chosen = (chosen + (e.key === 'ArrowDown' ? 1 : choices.length - 1)) % choices.length; show() }
    else if (e.key === 'Enter' || e.key === 'Tab') put(choices[chosen])
    else if (e.key === 'Escape') close()
    else return
    e.preventDefault()
    e.stopImmediatePropagation()
  }, true)
}
