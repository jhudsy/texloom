// Tiny DOM helpers shared by the UI modules. Children are added as text nodes or
// elements, never parsed as HTML, so user-controlled strings can't inject markup.
import { t } from './i18n'

type Kid = Node | string | null | undefined | false
export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs?: Record<string, any>, ...kids: Kid[]): HTMLElementTagNameMap[K]
export function h(tag: string, attrs?: Record<string, any>, ...kids: Kid[]): HTMLElement
export function h(tag: string, attrs: Record<string, any> = {}, ...kids: Kid[]): HTMLElement {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v)
    else if (k === 'checked' || k === 'value') (el as any)[k] = v
    else if (v !== false && v != null) el.setAttribute(k, v === true ? '' : String(v))
  }
  for (const kid of kids) if (kid !== null && kid !== undefined && kid !== false) el.append(kid)
  return el
}

export const $ = <T extends HTMLElement = HTMLElement>(sel: string, root: ParentNode = document) => root.querySelector(sel) as T

export function dialog(cls: string, ...kids: Kid[]): HTMLDialogElement {
  const dlg = h('dialog', { class: `dlg ${cls}` }, ...kids)
  document.body.append(dlg)
  dlg.addEventListener('close', () => dlg.remove())
  dlg.showModal()
  return dlg
}

/** A styled prompt(): resolves to what was typed, or null if cancelled. */
export function askText(label: string, value = '', maxLength?: number): Promise<string | null> {
  return new Promise((resolve) => {
    let answer: string | null = null
    const input = h('input', { value, 'aria-label': label, class: 'ask-input', ...(maxLength ? { maxlength: maxLength } : {}) }) as HTMLInputElement
    const ok = () => { answer = input.value; dlg.close() }
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ok() } })
    const dlg = dialog('settings',
      h('p', { class: 'ask-label' }, label), input,
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), h('button', { class: 'primary', onclick: ok }, t('OK'))))
    dlg.addEventListener('close', () => resolve(answer))
    input.focus()
    input.setSelectionRange(value.length, value.length)
  })
}

/** Offer bytes to the user as a file download. */
export function saveFile(name: string, data: Uint8Array | string, type = 'application/octet-stream') {
  const url = URL.createObjectURL(new Blob([data as BlobPart], { type }))
  const a = h('a', { href: url, download: name })
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** Make `handle` resize `target` vertically by dragging (height stored under `key`). */
export function verticalResizer(handle: HTMLElement, target: HTMLElement, key: string, min = 60, def = 180) {
  const get = () => { try { return Number(localStorage.getItem(key)) || def } catch { return def } }
  const set = (px: number) => {
    const max = Math.max(min, (target.parentElement?.clientHeight ?? 600) - 120)
    const v = Math.round(Math.min(max, Math.max(min, px)))
    target.style.height = `${v}px`
    try { localStorage.setItem(key, String(v)) } catch {}
  }
  target.style.height = `${get()}px`
  handle.setAttribute('role', 'separator')
  handle.setAttribute('aria-orientation', 'horizontal')
  handle.tabIndex = 0
  handle.title = t('Drag to resize · double-click to reset')
  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return
    e.preventDefault()
    handle.setPointerCapture(e.pointerId)
    document.body.classList.add('resizing-v')
    const startY = e.clientY, startH = target.getBoundingClientRect().height
    const move = (ev: PointerEvent) => set(startH - (ev.clientY - startY)) // dragging up grows the panel below
    const up = () => {
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', up)
      handle.removeEventListener('pointercancel', up)
      document.body.classList.remove('resizing-v')
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
    handle.addEventListener('pointercancel', up)
  })
  handle.addEventListener('dblclick', () => set(def))
  handle.addEventListener('keydown', (e) => {
    const d = e.key === 'ArrowUp' ? 1 : e.key === 'ArrowDown' ? -1 : 0
    if (!d) return
    e.preventDefault()
    set(target.getBoundingClientRect().height + d * (e.shiftKey ? 60 : 20))
  })
}
