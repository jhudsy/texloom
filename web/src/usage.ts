// How much the server keeps: this project, and (for whoever holds its creation code) all that the
// code stores against its allowance. Shown in Settings ▸ Sync & storage.

import { h } from './dom'
import { t, locale } from './i18n'
import type { Replica, StoreInfo } from './replica'

const KB = 1024, MB = KB * 1024, GB = MB * 1024

/** A size for people: KB, MB or GB, with the language's decimal mark (and its unit names: Mo in French). */
export function bytes(n: number): string {
  const num = (x: number, digits: number) => x.toLocaleString(locale, { maximumFractionDigits: digits })
  if (n >= GB) return t('{{n}} GB', { n: num(n / GB, 1) })
  if (n >= MB) return t('{{n}} MB', { n: num(n / MB, n >= 100 * MB ? 0 : 1) })
  return t('{{n}} KB', { n: num(Math.max(n, n > 0 ? KB : 0) / KB, 0) })
}

/** The lines to show for these figures (empty when the server keeps nothing of the project). */
export function usageLines(info: StoreInfo | null, storage: 'server' | 'relay'): HTMLElement[] {
  if (!info || storage === 'relay' || !info.registered) return []
  const out: HTMLElement[] = [h('p', { class: 'small' }, info.projectMax != null
    ? t('This project takes {{used}} on the server (up to {{max}} per project).', { used: bytes(info.used), max: bytes(info.projectMax) })
    : t('This project takes {{used}} on the server.', { used: bytes(info.used) }))]
  if (info.allowance != null && info.ownerUsed != null) {
    const left = Math.max(0, info.allowance - info.ownerUsed)
    out.push(h('div', { class: 'usage' },
      h('meter', { min: '0', max: String(info.allowance), value: String(Math.min(info.ownerUsed, info.allowance)),
        high: String(info.allowance * 0.9), optimum: '0', 'aria-label': t('Storage used') }),
      h('p', { class: 'small' }, t('Your creation code: {{used}} of {{total}} used, {{left}} left (all its projects and project lists).',
        { used: bytes(info.ownerUsed), total: bytes(info.allowance), left: bytes(left) }))))
  }
  return out
}

/** A box showing the figures, asking the server for fresh ones once shown. */
export function usageBox(r: Replica): HTMLElement {
  const box = h('div', { class: 'usage-box' })
  const draw = () => box.replaceChildren(...usageLines(r.storeInfo, r.storage))
  draw()
  r.onUsage = () => { if (box.isConnected) draw() }
  r.askUsage()
  return box
}
