// The keyboard shortcuts, in one list: opened with ? (outside a text field) or from Settings.
// TeXLoom's own, and the editor's (CodeMirror's) that are worth knowing.
import { dialog, h } from './dom'
import { t } from './i18n'

const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

const NAMES: Record<string, [mac: string, other: string]> = {
  Mod: ['⌘', 'Ctrl'], Ctrl: ['⌃', 'Ctrl'], Alt: ['⌥', 'Alt'], Shift: ['⇧', 'Shift'],
  ArrowLeft: ['←', '←'], ArrowRight: ['→', '→'], ArrowUp: ['↑', '↑'], ArrowDown: ['↓', '↓'],
  Enter: ['↩', 'Enter'], Escape: ['Esc', 'Esc'], Space: ['Space', 'Space'],
}

/** 'Mod-Alt-m' as ⌘⌥M on a Mac and Ctrl+Alt+M elsewhere. */
export function keys(spec: string): string {
  const parts = spec.split('-').map((p) => NAMES[p]?.[MAC ? 0 : 1] ?? (p.length === 1 ? p.toUpperCase() : p))
  return parts.join(MAC ? '' : '+')
}

/** A shortcut: its keys (one spec, or a Mac one and another), or words for a mouse action. */
type Row = [what: string, how: string | { mac: string; other: string } | { mouse: string }]

function groups(): [string, Row[]][] {
  return [
    [t('Anywhere'), [
      [t('Compile'), 'Mod-s'],
      [t('Search the whole project'), 'Mod-Shift-f'],
      [t('Show the cursor’s place in the PDF'), 'Mod-Alt-ArrowRight'],
      [t('Go to the source of the PDF (where you last clicked it, or the middle of the view)'), 'Mod-Alt-ArrowLeft'],
      [t('This list'), '?'],
    ]],
    [t('In the editor'), [
      [t('Comment on the selected text'), 'Mod-Alt-m'],
      [t('Suggest mode on or off'), 'Mod-Alt-e'],
      [t('Insert a symbol'), 'Mod-Alt-s'],
      [t('Bold, italic (\\textbf, \\textit around the selection)'), `${keys('Mod-b')}  ${keys('Mod-i')}`],
      [t('Find and replace in this file'), 'Mod-f'],
      [t('Go to a line'), 'Mod-Alt-g'],
      [t('Comment lines out or back in (%)'), 'Mod-/'],
      [t('Undo, redo'), { mac: `${keys('Mod-z')}  ${keys('Mod-Shift-z')}`, other: `${keys('Mod-z')}  ${keys('Mod-y')}` }],
      [t('Select the next match of the selection'), 'Mod-d'],
      [t('Move the line up or down'), `${keys('Alt-ArrowUp')}  ${keys('Alt-ArrowDown')}`],
      [t('Copy the line up or down'), `${keys('Shift-Alt-ArrowUp')}  ${keys('Shift-Alt-ArrowDown')}`],
      [t('Delete the line'), 'Mod-Shift-k'],
      [t('Indent more, less'), `${keys('Mod-]')}  ${keys('Mod-[')}`],
      [t('Suggest completions'), { mac: keys('Alt-i'), other: keys('Ctrl-Space') }],
    ]],
    [t('Writing a comment'), [
      [t('Save it'), 'Mod-Enter'],
      [t('Close it while it’s empty'), 'Escape'],
    ]],
    [t('In the PDF'), [
      [t('Previous, next page (once you’ve clicked the PDF)'), `${keys('ArrowLeft')}  ${keys('ArrowRight')}`],
      [t('First, last page'), 'Home  End'],
      [t('Fit the width or a whole page'), { mouse: t('Click the zoom percentage') }],
      [t('Go to the source of a spot'), { mouse: t('Double-click') }],
      [t('Zoom'), { mouse: t('{{key}} + scroll, or pinch', { key: keys('Mod') }) }],
      [t('Comment on the selected text'), 'c'],
      [t('Copy selected text, open in a separate window, download'), { mouse: t('Right-click') }],
    ]],
    [t('In the assistant'), [
      [t('Send'), 'Enter'],
      [t('New line'), 'Shift-Enter'],
    ]],
  ]
}

function shown(how: Row[1]): HTMLElement {
  if (typeof how === 'object' && 'mouse' in how) return h('span', { class: 'muted' }, how.mouse)
  const text = typeof how === 'string' ? (how.includes(' ') ? how : keys(how)) : MAC ? how.mac : how.other
  return h('span', {}, ...text.split('  ').map((k) => h('kbd', {}, k)))
}

let open: HTMLDialogElement | null = null

export function showShortcuts() {
  if (open) return
  const dlg = dialog('shortcuts',
    h('h3', {}, t('Keyboard shortcuts')),
    ...groups().flatMap(([title, rows]) => [
      h('h5', {}, title),
      h('table', { class: 'keys' }, ...rows.map(([what, how]) => h('tr', {}, h('td', {}, what), h('td', {}, shown(how))))),
    ]),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Done'))))
  open = dlg
  dlg.addEventListener('close', () => { open = null })
}

/** ? anywhere but in a text field opens the list. */
export function listenForShortcutsKey() {
  document.addEventListener('keydown', (e) => {
    if (e.key !== '?' || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return
    const el = e.target as HTMLElement | null
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.closest?.('.cm-editor'))) return
    e.preventDefault()
    showShortcuts()
  })
}
