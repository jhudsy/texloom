// Resizable, collapsible three-pane layout: files | editor | PDF/assistant.
// Widths are remembered per browser. The editor takes whatever space is left.
import { t } from './i18n'

const KEY = 'texloom.layout'
const MIN_LEFT = 140, MIN_RIGHT = 260, MIN_EDITOR = 280
const NARROW = 900 // below this width the old single-column-ish CSS layout applies

interface State { left: number; right: number; leftHidden: boolean; rightHidden: boolean }

function load(): State {
  const def: State = { left: 220, right: Math.round(Math.max(360, (innerWidth - 220) * 0.45)), leftHidden: false, rightHidden: false }
  try { return { ...def, ...JSON.parse(localStorage.getItem(KEY) || '{}') } } catch { return def }
}

export interface Layout {
  toggleLeft(): void
  toggleRight(): void
  showRight(): void
  /** Small windows: switch back from the PDF panel to the editor. */
  showEditor(): void
  /** Small windows: the file list is a drawer over the editor. */
  closeDrawer(): void
  readonly narrow: boolean
  readonly leftHidden: boolean
  readonly rightHidden: boolean
  onChange: () => void
}

export function setupLayout(main: HTMLElement, splitL: HTMLElement, splitR: HTMLElement): Layout {
  const s = load()
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(s)) } catch {} }

  let narrowRight = false // small windows: showing the PDF/assistant panel instead of the editor
  let drawerOpen = false // small windows: the file list is open over them
  const apply = () => {
    if (innerWidth < NARROW) { // small windows: the editor OR the PDF/assistant panel, switched with ▥
      const right = narrowRight
      main.classList.toggle('no-left', !drawerOpen)
      main.classList.toggle('drawer-open', drawerOpen)
      main.classList.toggle('no-right', !right)
      main.classList.toggle('no-editor', right)
      main.style.gridTemplateColumns = right ? '0 0 0 minmax(0, 1fr)' : '0 minmax(0, 1fr) 0 0' // the file list is out of the grid here (a drawer)
      layout.onChange()
      return
    }
    const total = main.clientWidth
    // keep stored widths valid for the current window size
    s.left = Math.max(MIN_LEFT, Math.min(s.left, total * 0.4))
    const leftW = s.leftHidden ? 0 : s.left
    s.right = Math.max(MIN_RIGHT, Math.min(s.right, total - leftW - MIN_EDITOR))
    const rightW = s.rightHidden ? 0 : s.right
    main.classList.remove('drawer-open')
    main.classList.toggle('no-left', s.leftHidden)
    main.classList.toggle('no-right', s.rightHidden)
    main.classList.remove('no-editor')
    main.style.gridTemplateColumns = `${leftW}px ${s.leftHidden ? 0 : 5}px minmax(0, 1fr) ${s.rightHidden ? 0 : 5}px ${rightW}px`
    layout.onChange()
  }

  const drag = (split: HTMLElement, side: 'left' | 'right') => {
    split.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return
      e.preventDefault()
      split.setPointerCapture(e.pointerId)
      document.body.classList.add('resizing')
      const rect = main.getBoundingClientRect()
      const move = (ev: PointerEvent) => {
        if (side === 'left') s.left = ev.clientX - rect.left
        else s.right = rect.right - ev.clientX
        apply()
      }
      const up = () => {
        split.removeEventListener('pointermove', move)
        split.removeEventListener('pointerup', up)
        split.removeEventListener('pointercancel', up)
        document.body.classList.remove('resizing')
        save()
      }
      split.addEventListener('pointermove', move)
      split.addEventListener('pointerup', up)
      split.addEventListener('pointercancel', up)
    })
    // double-click resets that pane to its default width
    split.addEventListener('dblclick', () => {
      if (side === 'left') s.left = 220
      else s.right = Math.round(Math.max(360, (main.clientWidth - (s.leftHidden ? 0 : s.left)) * 0.45))
      apply(); save()
    })
    // keyboard: focus the divider and use the arrow keys
    split.tabIndex = 0
    split.addEventListener('keydown', (e) => {
      const step = e.shiftKey ? 50 : 16
      const dir = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0
      if (!dir) return
      e.preventDefault()
      if (side === 'left') s.left += dir * step
      else s.right -= dir * step
      apply(); save()
    })
  }
  drag(splitL, 'left')
  drag(splitR, 'right')
  splitL.setAttribute('role', 'separator'); splitL.setAttribute('aria-label', t('Resize file list'))
  splitR.setAttribute('role', 'separator'); splitR.setAttribute('aria-label', t('Resize PDF and assistant panel'))
  splitL.title = splitR.title = t('Drag to resize · double-click to reset')
  addEventListener('resize', apply)
  main.addEventListener('pointerdown', (e) => { // a tap beside the drawer closes it
    if (drawerOpen && innerWidth < NARROW && !(e.target as HTMLElement).closest('aside')) layout.closeDrawer()
  })

  const layout: Layout = {
    toggleLeft() {
      if (innerWidth < NARROW) { drawerOpen = !drawerOpen; apply(); return }
      s.leftHidden = !s.leftHidden; apply(); save()
    },
    toggleRight() {
      if (innerWidth < NARROW) { narrowRight = !narrowRight; apply(); return }
      s.rightHidden = !s.rightHidden; apply(); save()
    },
    showRight() {
      if (innerWidth < NARROW) { if (!narrowRight || drawerOpen) { narrowRight = true; drawerOpen = false; apply() } return }
      if (s.rightHidden) { s.rightHidden = false; apply(); save() }
    },
    showEditor() { if (innerWidth < NARROW && (narrowRight || drawerOpen)) { narrowRight = false; drawerOpen = false; apply() } },
    closeDrawer() { if (drawerOpen) { drawerOpen = false; apply() } },
    get narrow() { return innerWidth < NARROW },
    get leftHidden() { return innerWidth < NARROW ? !drawerOpen : s.leftHidden },
    get rightHidden() { return innerWidth < NARROW ? !narrowRight : s.rightHidden },
    onChange: () => {},
  }
  requestAnimationFrame(apply)
  return layout
}
