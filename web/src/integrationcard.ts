// An integration (Zotero, git repositories, Dropbox…) in Settings, as a card: its name, a one-line
// status beside it, and the full section underneath, folded away unless the project uses it or it
// needs attention. The section itself is the integration's own (zoteroui.ts, githubui.ts…).
import { h } from './dom'

export type Tone = 'ok' | 'warn' | 'err' | 'off'
export interface Status { text: string; tone: Tone; open?: boolean } // open: shown unfolded when Settings opens

/** A card for one integration. `status` is asked again every second while the card is on screen. */
export function integrationCard(name: string, status: () => Status, body: HTMLElement): HTMLElement {
  const dot = h('span', { class: 'idot', 'aria-hidden': 'true' })
  const line = h('span', { class: 'istatus' })
  const first = status()
  const card = h('details', { class: 'icard', ...(first.open ? { open: true } : {}) },
    h('summary', {}, h('span', { class: 'iname' }, name), h('span', { class: 'iright' }, dot, line)),
    h('div', { class: 'ibody' }, body)) as HTMLDetailsElement
  const show = (s: Status) => { line.textContent = s.text; dot.className = `idot ${s.tone}`; card.dataset.tone = s.tone }
  show(first)
  let placed = false // (the card is made before it's on the page: stop only once it has been and has gone)
  const timer = setInterval(() => {
    if (!card.isConnected) { if (placed) clearInterval(timer); return }
    placed = true
    show(status())
  }, 1000)
  return card
}
