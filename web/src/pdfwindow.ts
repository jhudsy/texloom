// The compiled PDF in a window of its own, for example on another screen. The editor stays in
// charge: it sends each new PDF to that window inside the browser (nothing goes through the
// server), and the window sends clicks back. Only the project's id is in the window's address,
// after the #: never its keys. A channel rather than window.opener, so that the two find each
// other again when either is reloaded.
import { $, h } from './dom'
import { t } from './i18n'
import { PdfView } from './pdfview'

type Point = { page: number; x: number; y: number }
type Message =
  | { t: 'pdf'; data: Uint8Array; name: string } // editor -> window: show this
  | ({ t: 'mark'; w: number; h: number } & Point) // editor -> window: highlight this box
  | { t: 'ready' } // editor: just opened
  | { t: 'closed' } // editor: going away
  | { t: 'ask' } // window: send me the PDF
  | { t: 'gone' } // window: going away
  | ({ t: 'pick' } & Point) // window: go to the source of this point

const HASH = '#pdf-window='
const channel = (project: string) => new BroadcastChannel(`texloom-pdf-${project}`)

/** The project this window shows the PDF of, if that's what it is for. */
export function pdfWindowProject(): string | null {
  if (!location.hash.startsWith(HASH)) return null
  const project = location.hash.slice(HASH.length)
  return /^[\w-]+$/.test(project) ? project : null
}

/** The editor's end. `onPick`: the window asked for the source of a point. */
export function pdfWindowLink(project: string, name: () => string, onPick: (at: Point) => void) {
  const ch = channel(project)
  let latest: Uint8Array | null = null
  let connected = false // PDFs can be large: copy them only when a window wants them
  const say = (m: Message) => ch.postMessage(m)
  const send = () => { if (latest && connected) say({ t: 'pdf', data: latest, name: name() }) }
  ch.onmessage = (e: MessageEvent<Message>) => {
    const m = e.data
    if (m.t === 'ask') { connected = true; send() }
    else if (m.t === 'gone') connected = false
    else if (m.t === 'pick') onPick(m)
  }
  say({ t: 'ready' })
  window.addEventListener('pagehide', () => say({ t: 'closed' }))
  return {
    /** Open the window, or bring it forward. False if the browser refused. */
    open(): boolean {
      const win = window.open(`/${HASH}${project}`, `texloom-pdf-${project}`, 'popup=yes,width=820,height=1000')
      win?.focus()
      return !!win
    },
    pdf(data: Uint8Array) { latest = data; send() },
    mark(page: number, x: number, y: number, w: number, boxH: number) { if (connected) say({ t: 'mark', page, x, y, w, h: boxH }) },
  }
}

/** The window's end: this whole page. */
export function openPdfWindow(project: string) {
  document.title = t('PDF')
  const app = $('#app')
  app.className = 'pdfwindow'
  const view = new PdfView()
  const title = h('b', { class: 'name' })
  const status = h('span', { class: 'muted', role: 'status' }, t('Waiting for a PDF from the editor…'))
  app.append(h('div', { class: 'pdfbar' }, title, status, h('span', { class: 'spacer' }), view.controls), view.el)
  const ch = channel(project)
  const say = (m: Message) => ch.postMessage(m)
  view.onPick = (page, x, y) => {
    say({ t: 'pick', page, x, y })
    try { window.opener?.focus() } catch {} // browsers may not allow it: the editor still jumps
  }
  ch.onmessage = (e: MessageEvent<Message>) => {
    const m = e.data
    if (m.t === 'pdf') {
      const name = m.name && m.name !== 'Untitled project' ? m.name : t('Untitled project')
      title.textContent = name
      document.title = `${name} — ${t('PDF')}`
      status.textContent = ''
      view.show(m.data).catch((err) => { status.textContent = t('PDF error: {{error}}', { error: err }) })
    } else if (m.t === 'mark') view.highlight(m.page, m.x, m.y, m.w, m.h)
    else if (m.t === 'ready') say({ t: 'ask' })
    else if (m.t === 'closed') status.textContent = t('The editor has closed, so this PDF is no longer updating.')
  }
  window.addEventListener('pagehide', () => say({ t: 'gone' }))
  say({ t: 'ask' })
}
