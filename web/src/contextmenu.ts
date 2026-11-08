// A small right-click menu. Returns a function that closes it.
export type MenuItem = [label: string, run: () => void]

/** Menu items, leaving out the ones whose condition was false: `cond && [label, run]`. */
export const menuItems = (...items: (MenuItem | false | null | undefined)[]) => items.filter(Boolean) as MenuItem[]

export function openContextMenu(x: number, y: number, items: MenuItem[], onClose?: () => void): () => void {
  const m = document.createElement('div')
  m.className = 'ctxmenu'
  m.setAttribute('role', 'menu')
  let open = true
  const close = () => {
    if (!open) return
    open = false
    document.removeEventListener('pointerdown', away, true)
    document.removeEventListener('keydown', away, true)
    removeEventListener('blur', away)
    m.remove()
    onClose?.()
  }
  const away = (e: Event) => {
    if (e instanceof KeyboardEvent ? e.key === 'Escape' : !m.contains(e.target as Node)) close()
  }
  for (const [label, run] of items) {
    const b = document.createElement('button')
    b.setAttribute('role', 'menuitem')
    b.textContent = label
    b.onclick = () => { close(); run() }
    m.append(b)
  }
  document.body.append(m)
  const w = m.offsetWidth, h = m.offsetHeight
  Object.assign(m.style, { left: `${Math.max(4, Math.min(x, innerWidth - w - 4))}px`, top: `${Math.max(4, Math.min(y, innerHeight - h - 4))}px` })
  ;(m.firstElementChild as HTMLElement | null)?.focus()
  setTimeout(() => {
    if (!open) return
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('keydown', away, true)
    addEventListener('blur', away)
  })
  return close
}
