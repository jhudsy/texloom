// Resizable, collapsible three-pane layout: files | editor | PDF/assistant.
// Widths are remembered per browser. The editor takes whatever space is left.
import { t } from './i18n'

const KEY = 'texloom.layout'
const MIN_LEFT = 140, MIN_RIGHT = 260, MIN_EDITOR = 280
const NARROW = 900 // below this width one thing is on screen at a time. The only place this number is
const narrowWindow = matchMedia(`(max-width: ${NARROW - 0.02}px)`)
const markWindow = () => document.documentElement.classList.toggle('narrow', narrowWindow.matches) // what style.css keys on
markWindow()

interface State { left: number; right: number; leftHidden: boolean; rightHidden: boolean }

function load(): State {
  const def: State = { left: 220, right: Math.round(Math.max(360, (innerWidth - 220) * 0.45)), leftHidden: false, rightHidden: false }
  try { return { ...def, ...JSON.parse(localStorage.getItem(KEY) || '{}') } } catch { return def }
}

export type Pane = 'left' | 'right'

export interface Layout {
  isOpen(pane: Pane): boolean
  toggle(pane: Pane): void
  reveal(pane: Pane): void
  /** Small windows: back to the editor. Elsewhere it's always on screen. */
  showEditor(): void
  /** Small windows: the file list is a drawer over the editor. */
  closeDrawer(): void
  /** One thing on screen at a time. */
  readonly narrow: boolean
  onChange: () => void
}

/** Which panes are open in a window of one kind, and how that looks. */
interface Mode {
  isOpen(pane: Pane): boolean
  set(pane: Pane, open: boolean): void
  render(): void
}

export function setupLayout(main: HTMLElement, splitL: HTMLElement, splitR: HTMLElement): Layout {
  const s = load()
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(s)) } catch {} }
  const classes = (left: boolean, right: boolean, editor: boolean, drawer: boolean) => {
    main.classList.toggle('no-left', !left)
    main.classList.toggle('no-right', !right)
    main.classList.toggle('no-editor', !editor)
    main.classList.toggle('drawer-open', drawer)
  }

  const wide: Mode = {
    isOpen: (pane) => !(pane === 'left' ? s.leftHidden : s.rightHidden),
    set(pane, open) { if (pane === 'left') s.leftHidden = !open; else s.rightHidden = !open; save() },
    render() {
      const total = main.clientWidth
      // keep stored widths valid for the current window size
      s.left = Math.max(MIN_LEFT, Math.min(s.left, total * 0.4))
      const leftW = s.leftHidden ? 0 : s.left
      s.right = Math.max(MIN_RIGHT, Math.min(s.right, total - leftW - MIN_EDITOR))
      const rightW = s.rightHidden ? 0 : s.right
      classes(!s.leftHidden, !s.rightHidden, true, false)
      main.style.gridTemplateColumns = `${leftW}px ${s.leftHidden ? 0 : 5}px minmax(0, 1fr) ${s.rightHidden ? 0 : 5}px ${rightW}px`
    },
  }

  const open = { left: false, right: false } // small windows: the drawer, and the panel in place of the editor
  const narrow: Mode = {
    isOpen: (pane) => open[pane],
    set(pane, value) { open[pane] = value; if (value && pane === 'right') open.left = false },
    render() {
      classes(open.left, open.right, !open.right, open.left)
      main.style.gridTemplateColumns = open.right ? '0 0 0 minmax(0, 1fr)' : '0 minmax(0, 1fr) 0 0' // the file list is out of the grid here (a drawer)
    },
  }

  const mode = () => (narrowWindow.matches ? narrow : wide)
  const apply = () => { mode().render(); layout.onChange() }
  const set = (pane: Pane, value: boolean) => { if (mode().isOpen(pane) !== value) { mode().set(pane, value); apply() } }

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
  narrowWindow.addEventListener('change', () => { markWindow(); apply() })
  main.addEventListener('pointerdown', (e) => { // a tap beside the drawer closes it
    if (!(e.target as HTMLElement).closest('aside')) layout.closeDrawer()
  })

  const layout: Layout = {
    isOpen: (pane) => mode().isOpen(pane),
    toggle: (pane) => set(pane, !mode().isOpen(pane)),
    reveal: (pane) => set(pane, true),
    showEditor() { if (narrowWindow.matches) { open.left = open.right = false; apply() } },
    closeDrawer() { if (narrowWindow.matches) set('left', false) },
    get narrow() { return narrowWindow.matches },
    onChange: () => {},
  }
  requestAnimationFrame(apply)
  return layout
}
